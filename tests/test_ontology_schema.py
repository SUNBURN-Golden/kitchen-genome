import os
import sqlite3
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'daemon'))

from kg_daemon import db as kgdb                                   # noqa: E402
from kg_daemon import ontology as onto                             # noqa: E402
from kg_daemon.claims import (add_evidence, create_claim, link_evidence, open_conflict, set_claim_status)  # noqa: E402
from kg_daemon.registry import load_registry, sync_registry       # noqa: E402


@pytest.fixture()
def conn():
    c = kgdb.connect(':memory:')
    kgdb.migrate(c)
    sync_registry(c, load_registry())
    return c


def mk(conn):
    conn.execute("INSERT INTO chef(chef_id,display_name,created_at) VALUES ('c1','가',?),('c2','나',?)", ('t', 't'))
    conn.execute("INSERT INTO restaurant(restaurant_id,name,created_at) VALUES ('r1','R1','t'),('r2','R2','t')")
    conn.execute("INSERT INTO branch(branch_id,restaurant_id,created_at) VALUES ('b1','r1','t'),('b2','r2','t')")


def ev(conn, url='https://press.example.com/x', source='press_articles', day='2026-10-20'):
    return add_evidence(conn, source_id=source, source_url=url, collected_at=day, quote='짧은 인용')


def emp(conn, chef, branch, status='source_claim', **kw):
    cl = create_claim(conn, 'employment', status, 'test')
    if status != 'unverified':
        link_evidence(conn, cl, ev(conn))
    return onto.add_employment(conn, chef_id=chef, branch_id=branch, claim_id=cl, **kw)


def test_migrations_apply_once_and_are_checksum_pinned(tmp_path):
    c = kgdb.connect(':memory:')
    assert kgdb.migrate(c) == ['0001_core_ontology.sql', '0002_daemon_runtime.sql', '0003_collection_bot.sql', '0004_profile_facts.sql', '0005_candidate_fact_types.sql', '0006_deepseek_collection.sql', '0007_aux_robots_counter.sql', '0008_deepseek_efficiency.sql', '0009_extraction_verbatim_dates.sql', '0010_fetch_priority.sql', '0011_collect_discovery_scope.sql', '0012_web_access_guard.sql']
    assert kgdb.migrate(c) == []
    d = tmp_path / 'm'
    d.mkdir()
    (d / '0001_x.sql').write_text('CREATE TABLE IF NOT EXISTS t(a);')
    c2 = kgdb.connect(':memory:')
    kgdb.migrate(c2, str(d))
    (d / '0001_x.sql').write_text('CREATE TABLE IF NOT EXISTS t(a, b);')
    with pytest.raises(RuntimeError):
        kgdb.migrate(c2, str(d))


def test_no_row_in_fact_tables_can_be_deleted(conn):
    mk(conn)
    emp(conn, 'c1', 'b1', valid_to_kind='open')
    for sql in ("DELETE FROM employment", "DELETE FROM chef", "DELETE FROM claim", "DELETE FROM evidence", "DELETE FROM branch", "DELETE FROM source"):
        with pytest.raises(sqlite3.DatabaseError, match='append-only'):
            conn.execute(sql)


def test_evidence_is_immutable_and_quote_is_short(conn):
    e = ev(conn)
    with pytest.raises(sqlite3.DatabaseError, match='immutable'):
        conn.execute("UPDATE evidence SET quote='x' WHERE evidence_id=?", (e,))
    with pytest.raises(ValueError):
        add_evidence(conn, source_id='press_articles', source_url='https://a.example.com', collected_at='2026-10-20', quote='가' * 201)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO evidence(evidence_id,source_id,source_url,quote,collected_at,created_at) VALUES ('x','press_articles','u',?,'d','d')", ('가' * 201,))


def test_employment_open_ended_and_constraints(conn):
    mk(conn)
    emp(conn, 'c1', 'b1', valid_from='2020', valid_to_kind='open')        # open-ended allowed
    with pytest.raises(sqlite3.IntegrityError):                            # 'known' end needs a date
        conn.execute("INSERT INTO employment(employment_id,chef_id,branch_id,valid_to_kind,claim_id,created_at) VALUES ('x','c1','b1','known',(SELECT claim_id FROM claim LIMIT 1),'t')")
    with pytest.raises(sqlite3.IntegrityError):                            # needs a branch or an employer label
        cl = create_claim(conn, 'employment', 'unverified', 't')
        conn.execute("INSERT INTO employment(employment_id,chef_id,valid_to_kind,claim_id,created_at) VALUES ('y','c1','open',?,'t')", (cl,))
    with pytest.raises(sqlite3.IntegrityError):                            # end before start
        cl = create_claim(conn, 'employment', 'unverified', 't')
        conn.execute("INSERT INTO employment(employment_id,chef_id,branch_id,valid_from,valid_to,valid_to_kind,claim_id,created_at) VALUES ('z','c1','b1','2022','2020','known',?,'t')", (cl,))
    with pytest.raises(ValueError):                                        # free text is never coerced into a date
        emp(conn, 'c2', 'b1', valid_from='2024년 봄')


def test_move_closes_prior_and_adds_pending_new_never_deletes(conn):
    mk(conn)
    old = emp(conn, 'c1', 'b1', valid_from='2020', valid_to_kind='open')
    evd = dict(source_id='instagram_public', source_url='https://www.instagram.com/p/FAKE', collected_at='2026-10-26')
    r = onto.move_chef(conn, chef_id='c1', effective_date='2026-10-10', evidence=evd, to_branch_id='b2')
    assert r['closed_employment'] == old
    rows = {x['employment_id']: x for x in conn.execute('SELECT * FROM employment')}
    assert len(rows) == 2 and rows[old]['state'] == 'closed' and rows[old]['valid_to'] == '2026-10-10'
    new = rows[r['new_employment']]
    st = conn.execute('SELECT status FROM claim WHERE claim_id=?', (new['claim_id'],)).fetchone()['status']
    assert st == 'unverified' and new['valid_to_kind'] == 'open'
    assert conn.execute("SELECT COUNT(*) FROM review_queue WHERE reason='pending_verification'").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM correction_history WHERE table_name='employment' AND row_id=?", (old,)).fetchone()[0] >= 2   # audit trail


def test_unverified_move_is_not_shown_as_fact_but_is_visible_as_pending(conn):
    mk(conn)
    emp(conn, 'c1', 'b1', valid_from='2020', valid_to_kind='open')
    onto.move_chef(conn, chef_id='c1', effective_date='2026-10-10', status='unverified',
                   evidence=dict(source_id='instagram_public', source_url='https://www.instagram.com/p/FAKE', collected_at='2026-10-26'), to_branch_id='b2')
    public = onto.current_employment(conn, 'c1', '2026-10-27')                       # default min_status=source_claim
    assert [e['branch_id'] for e in public] == ['b1'] and public[0]['end_pending'] is True
    assert len(onto.pending_moves(conn, 'c1')) == 1
    reviewer = onto.current_employment(conn, 'c1', '2026-10-27', min_status='unverified')
    assert [e['branch_id'] for e in reviewer] == ['b2']
    # and history is intact: before the move date the old job applies, in both views
    assert [e['branch_id'] for e in onto.current_employment(conn, 'c1', '2026-09-01', min_status='unverified')] == ['b1']


def test_ambiguous_move_is_not_guessed(conn):
    mk(conn)
    emp(conn, 'c1', 'b1', valid_to_kind='open')
    emp(conn, 'c1', 'b2', valid_to_kind='open')
    with pytest.raises(onto.AmbiguousMove):
        onto.move_chef(conn, chef_id='c1', effective_date='2026-10-10', to_employer_label='somewhere',
                       evidence=dict(source_id='press_articles', source_url='https://press.example.com/m', collected_at='2026-10-26'))
    assert conn.execute("SELECT COUNT(*) FROM employment WHERE state='closed'").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM review_queue WHERE reason='ambiguous_move'").fetchone()[0] == 1


def test_unknown_end_is_never_current_and_former_employer_not_listed_as_current(conn):
    mk(conn)
    emp(conn, 'c1', 'b1', valid_to_kind='unknown')                    # alumnus of b1
    emp(conn, 'c1', 'b2', valid_to_kind='open')
    assert [e['branch_id'] for e in onto.current_employment(conn, 'c1', '2026-10-27')] == ['b2']


def status_event(conn, branch, kind, date, status='source_claim'):
    cl = create_claim(conn, 'status_event', status, 't')
    link_evidence(conn, cl, ev(conn, url=f'https://press.example.com/{branch}{kind}{date}'))
    p = onto.parse_iso_date(date) if date else None
    conn.execute('INSERT INTO restaurant_status_event(event_id,branch_id,kind,effective_date,effective_precision,claim_id,created_at) VALUES (?,?,?,?,?,?,?)',
                 (f'e{conn.execute("SELECT COUNT(*) FROM restaurant_status_event").fetchone()[0]}', branch, kind, p[0] if p else None, p[1] if p else None, cl, 't'))


def test_branch_status_as_of_dates(conn):
    mk(conn)
    status_event(conn, 'b1', 'open', '2020-03')
    status_event(conn, 'b1', 'pause', '2022-01-10')
    status_event(conn, 'b1', 'reopen', '2022-06-01')
    status_event(conn, 'b1', 'close', '2024-09-01')
    st = lambda d: onto.branch_state_asof(conn, 'b1', d)['state']       # noqa: E731
    assert st('2019-01-01') == 'unknown'
    assert st('2021-01-01') == 'open'
    assert st('2022-02-01') == 'paused'
    assert st('2023-01-01') == 'open'
    assert st('2025-01-01') == 'closed'


def test_coarse_dates_that_straddle_are_uncertain_not_guessed(conn):
    mk(conn)
    status_event(conn, 'b1', 'open', '2020-01-01')
    status_event(conn, 'b1', 'close', '2024')                           # year only
    assert onto.branch_state_asof(conn, 'b1', '2024-06-15')['state'] == 'uncertain'
    assert onto.branch_state_asof(conn, 'b1', '2025-01-01')['state'] == 'closed'
    assert onto.branch_state_asof(conn, 'b1', '2023-12-31')['state'] == 'open'


def test_closed_branch_employment_is_not_current(conn):
    mk(conn)
    emp(conn, 'c1', 'b1', valid_to_kind='open')
    status_event(conn, 'b1', 'close', '2026-07-19')
    assert onto.current_employment(conn, 'c1', '2026-10-27') == []
    assert [e['branch_id'] for e in onto.current_employment(conn, 'c1', '2026-06-01')] == ['b1']


def test_overlap_is_derived_coemployment_and_not_mentorship(conn):
    mk(conn)
    emp(conn, 'c1', 'b1', valid_from='2018', valid_to='2021', valid_to_kind='known')
    emp(conn, 'c2', 'b1', valid_from='2020', valid_to_kind='open')
    emp(conn, 'c2', 'b2', valid_from='2010', valid_to_kind='unknown')
    ov = conn.execute('SELECT * FROM v_overlap').fetchall()
    assert len(ov) == 1 and ov[0]['overlap_from'] == '2020' and ov[0]['overlap_to'] == '2021'
    assert conn.execute('SELECT COUNT(*) FROM mentorship_assertion').fetchone()[0] == 0
    assert conn.execute("SELECT type FROM sqlite_master WHERE name='v_overlap'").fetchone()[0] == 'view'   # cannot be written to


def test_overlap_excludes_unknown_start(conn):
    mk(conn)
    emp(conn, 'c1', 'b1', valid_to_kind='open')                       # no valid_from
    emp(conn, 'c2', 'b1', valid_from='2020', valid_to_kind='open')
    assert conn.execute('SELECT COUNT(*) FROM v_overlap').fetchone()[0] == 0


def test_mentorship_needs_a_source_assertion_not_ai(conn):
    mk(conn)
    ai = create_claim(conn, 'mentorship', 'ai_inference', 'ai')
    with pytest.raises(sqlite3.IntegrityError, match='source'):
        conn.execute("INSERT INTO mentorship_assertion(mentorship_id,mentee_chef_id,mentor_chef_id,claim_id,created_at) VALUES ('m','c1','c2',?,'t')", (ai,))
    ok = create_claim(conn, 'mentorship', 'source_claim', 'src')
    link_evidence(conn, ok, ev(conn))
    conn.execute("INSERT INTO mentorship_assertion(mentorship_id,mentee_chef_id,mentor_chef_id,claim_id,created_at) VALUES ('m','c1','c2',?,'t')", (ok,))


def test_claim_promotion_rules(conn):
    cl = create_claim(conn, 'employment', 'source_claim', 't')
    with pytest.raises(ValueError):                                   # no evidence yet
        set_claim_status(conn, cl, 'verified', 'daemon:verify')
    link_evidence(conn, cl, ev(conn, url='https://press.example.com/1'))
    with pytest.raises(ValueError):                                   # one press host is not enough
        set_claim_status(conn, cl, 'verified', 'daemon:verify')
    link_evidence(conn, cl, ev(conn, url='https://other.example.org/2'))
    set_claim_status(conn, cl, 'verified', 'daemon:verify', reason='two independent hosts')
    h = conn.execute('SELECT * FROM claim_status_history WHERE claim_id=?', (cl,)).fetchall()
    assert [(x['from_status'], x['to_status']) for x in h] == [('source_claim', 'verified')] and h[0]['changed_by'] == 'daemon:verify'


def test_social_lead_never_counts_toward_verified_and_ai_needs_human(conn):
    cl = create_claim(conn, 'employment', 'unverified', 't')
    link_evidence(conn, cl, ev(conn, url='https://www.instagram.com/p/a', source='instagram_public'))
    link_evidence(conn, cl, ev(conn, url='https://www.instagram.com/p/b', source='instagram_public'))
    with pytest.raises(ValueError):
        set_claim_status(conn, cl, 'verified', 'daemon:verify')
    ai = create_claim(conn, 'employment', 'ai_inference', 'ai')
    link_evidence(conn, ai, ev(conn, url='https://official.example.com/o', source='official_sites'))
    with pytest.raises(ValueError, match='human'):
        set_claim_status(conn, ai, 'verified', 'daemon:verify')
    set_claim_status(conn, ai, 'verified', 'human:reviewer')


def test_conflicts_keep_both_claims(conn):
    a = create_claim(conn, 'status_event', 'source_claim', 't')
    b = create_claim(conn, 'status_event', 'source_claim', 't')
    cid = open_conflict(conn, 'branch', 'b1', 'close.effective_date', a, b)
    assert open_conflict(conn, 'branch', 'b1', 'close.effective_date', b, a) == cid     # idempotent, order-free
    assert conn.execute("SELECT COUNT(*) FROM claim WHERE claim_id IN (?,?)", (a, b)).fetchone()[0] == 2
    with pytest.raises(sqlite3.DatabaseError):
        conn.execute('DELETE FROM conflict')


def test_parse_iso_date():
    assert onto.parse_iso_date('2024') == ('2024', 'year')
    assert onto.parse_iso_date('2024-05') == ('2024-05', 'month')
    assert onto.parse_iso_date('2024-02-30') is None
    assert onto.parse_iso_date('2024-13') is None
    assert onto.parse_iso_date('미확인') is None and onto.parse_iso_date('2024 (가오픈)') is None


def test_alumni_of_lists_openings_only_when_source_attributes_them(conn):
    mk(conn)
    emp(conn, 'c1', 'b1', role_text='수셰프', valid_to_kind='unknown')
    cl = create_claim(conn, 'status_event', 'source_claim', 't')
    link_evidence(conn, cl, ev(conn))
    conn.execute("INSERT INTO restaurant_status_event(event_id,branch_id,kind,effective_date,effective_precision,by_chef_id,claim_id,created_at) VALUES ('o','b2','open','2024-05','month','c1',?,'t')", (cl,))
    out = onto.alumni_of(conn, 'r1')
    assert [a['chef_id'] for a in out] == ['c1'] and out[0]['opened'][0]['restaurant'] == 'R2'
    assert onto.alumni_of(conn, 'r2') == []
