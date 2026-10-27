import json
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'daemon'))

from kg_daemon import db as kgdb                       # noqa: E402
from kg_daemon import ontology as onto                 # noqa: E402
from kg_daemon.seed_migration import migrate_seed      # noqa: E402
from kg_daemon.stages import run_quality_checks        # noqa: E402

FIX = os.path.join(ROOT, 'tests', 'fixtures', 'seed_minimal.json')
NOW = '2026-10-27T00:00:00Z'


@pytest.fixture()
def seed():
    return json.load(open(FIX, encoding='utf-8'))


@pytest.fixture()
def conn(seed):
    c = kgdb.connect(':memory:')
    kgdb.migrate(c)
    migrate_seed(c, seed, NOW)
    return c


def chef_id(conn, name):
    return conn.execute('SELECT chef_id FROM chef WHERE display_name=?', (name,)).fetchone()['chef_id']


def branch_of(conn, name):
    return conn.execute('SELECT b.branch_id FROM restaurant r JOIN branch b ON b.restaurant_id=r.restaurant_id WHERE r.name=?', (name,)).fetchone()['branch_id']


def test_idempotent_second_run_changes_nothing(seed):
    c = kgdb.connect(':memory:')
    kgdb.migrate(c)
    migrate_seed(c, seed, NOW)
    tables = ['chef', 'restaurant', 'branch', 'employment', 'claim', 'evidence', 'claim_evidence', 'restaurant_status_event', 'mentorship_assertion', 'alias', 'review_queue']
    before = {t: c.execute(f'SELECT COUNT(*) FROM {t}').fetchone()[0] for t in tables}
    migrate_seed(c, seed, NOW)
    assert {t: c.execute(f'SELECT COUNT(*) FROM {t}').fetchone()[0] for t in tables} == before


def test_established_and_kitchen_with_same_name_merge_and_flag_review(conn):
    assert conn.execute("SELECT COUNT(*) FROM restaurant WHERE name='키친A'").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM review_queue WHERE reason='entity_merge_by_name'").fetchone()[0] >= 1


def test_legacy_ids_are_kept_as_aliases(conn):
    row = conn.execute("SELECT entity_id FROM alias WHERE entity_type='restaurant' AND kind='legacy_id' AND alias='r1'").fetchone()
    assert row and conn.execute('SELECT name FROM restaurant WHERE restaurant_id=?', (row['entity_id'],)).fetchone()['name'] == '가상식당1'


def test_homonym_safe_chef_ids_and_same_chef_across_restaurants(conn):
    b = chef_id(conn, '테스트셰프B')
    assert conn.execute('SELECT COUNT(*) FROM employment WHERE chef_id=?', (b,)).fetchone()[0] >= 3      # origin@ka + r1 + r3 own
    assert conn.execute("SELECT disambiguator FROM chef WHERE chef_id=?", (b,)).fetchone()[0] == 'ka'


def test_origin_kitchen_is_past_employment_and_own_restaurant_is_open(conn):
    b = chef_id(conn, '테스트셰프B')
    ka = branch_of(conn, '키친A')
    rows = conn.execute('SELECT * FROM employment WHERE chef_id=? AND branch_id=?', (b, ka)).fetchall()
    # one employment per lineage assertion (r1 and r3 both name this chef): separate claims are kept, never merged away
    assert len(rows) == 2 and {r['valid_to_kind'] for r in rows} == {'unknown'} and {r['role_text'] for r in rows} >= {'수셰프'}
    cur = onto.current_employment(conn, b, '2026-10-27')
    assert [e['branch_id'] for e in cur] == [branch_of(conn, '가상식당1')]          # r3 is closed; origin kitchen not current


def test_legacy_verified_is_downgraded_to_source_claim_until_reverified(conn):
    assert conn.execute("SELECT COUNT(*) FROM claim WHERE status='verified'").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM claim WHERE status='source_claim'").fetchone()[0] > 0
    assert conn.execute("SELECT COUNT(*) FROM claim WHERE status='unverified'").fetchone()[0] > 0


def test_mentorship_only_where_source_text_asserts_it(conn):
    rows = conn.execute('SELECT * FROM mentorship_assertion').fetchall()
    assert len(rows) == 1
    assert chef_id(conn, '테스트셰프C') == rows[0]['mentee_chef_id'] and '가나다' in rows[0]['mentor_label']


def test_closed_restaurant_gets_status_event_and_no_current_employment(conn):
    r3 = branch_of(conn, '가상식당3')
    assert onto.branch_state_asof(conn, r3, '2026-10-27')['state'] == 'closed'
    assert onto.branch_state_asof(conn, r3, '2026-07-18')['state'] != 'closed'


def test_unparseable_open_dates_are_not_invented(conn):
    r3 = branch_of(conn, '가상식당3')
    evs = conn.execute("SELECT effective_date, effective_precision FROM restaurant_status_event WHERE branch_id=? AND kind='open'", (r3,)).fetchall()
    assert all(e['effective_date'] is None and e['effective_precision'] is None for e in evs)       # '미확인' stays undated
    r1 = branch_of(conn, '가상식당1')
    e = conn.execute("SELECT effective_date, effective_precision FROM restaurant_status_event WHERE branch_id=? AND kind='open'", (r1,)).fetchone()
    assert (e['effective_date'], e['effective_precision']) == ('2024-05', 'month')


def test_unnamed_chef_gets_no_employment_but_a_review_item(conn):
    r5 = branch_of(conn, '가상식당5')
    assert conn.execute('SELECT COUNT(*) FROM employment WHERE branch_id=?', (r5,)).fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM review_queue WHERE reason='unnamed_chef'").fetchone()[0] == 1


def test_multi_chef_cell_creates_one_employment_each(conn):
    r4 = branch_of(conn, '가상식당4')
    names = {conn.execute('SELECT display_name FROM chef WHERE chef_id=?', (e['chef_id'],)).fetchone()[0] for e in conn.execute('SELECT chef_id FROM employment WHERE branch_id=?', (r4,))}
    assert names == {'가상일', '가상이'}


def test_former_name_kept_as_alias(conn):
    assert conn.execute("SELECT COUNT(*) FROM alias WHERE alias='옛이름4'").fetchone()[0] == 1


def test_career_entry_refines_existing_employment_instead_of_duplicating(conn):
    d = chef_id(conn, '테스트셰프D')
    r6 = branch_of(conn, '가상호텔A')
    rows = conn.execute('SELECT * FROM employment WHERE chef_id=? AND branch_id=?', (d, r6)).fetchall()
    assert len(rows) == 1 and rows[0]['valid_from'] == '2018'


def test_conflict_lineage_status_becomes_review_not_silent_choice(conn):
    r8 = branch_of(conn, '가상식당8')
    assert conn.execute('SELECT COUNT(*) FROM employment WHERE branch_id=?', (r8,)).fetchone()[0] >= 1
    assert conn.execute("SELECT COUNT(*) FROM review_queue WHERE reason='legacy_conflict'").fetchone()[0] == 1


def test_quality_invariants_hold_after_migration(conn):
    assert run_quality_checks(conn, '2026-10-27')['violations'] == []


def test_dry_run_on_memory_and_input_is_not_mutated(seed):
    snapshot = json.dumps(seed, sort_keys=True)
    c = kgdb.connect(':memory:')
    kgdb.migrate(c)
    migrate_seed(c, seed, NOW)
    assert json.dumps(seed, sort_keys=True) == snapshot


@pytest.mark.skipif(not os.path.exists(os.path.join(ROOT, 'data', 'seed.json')), reason='no real seed in this checkout')
def test_real_seed_migrates_cleanly_in_memory_without_touching_the_file():
    p = os.path.join(ROOT, 'data', 'seed.json')
    raw = open(p, 'rb').read()
    c = kgdb.connect(':memory:')
    kgdb.migrate(c)
    stats = migrate_seed(c, json.loads(raw), NOW)
    assert stats['restaurants'] > 0 and stats['employments'] > 0
    assert run_quality_checks(c, '2026-10-27')['violations'] == []
    assert open(p, 'rb').read() == raw
