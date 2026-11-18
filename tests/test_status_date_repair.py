"""Collector dates and legacy timeline repairs must be safe for as-of readers."""
import json
import os
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), 'daemon'))

from kg_collect import apply, cycle
from kg_daemon import db, ontology, stages
from kg_daemon.claims import add_evidence, create_claim, link_evidence
from collect_world import SEED

NOW = '2026-11-18T00:00:00Z'


@pytest.fixture
def conn():
    c = db.connect(':memory:')
    db.migrate(c)
    cycle.init_db(c, SEED)
    c.execute("""INSERT INTO fetch_record
        (fetch_id,url,canonical_url,host,method,outcome,fetched_at,fetcher_version,extractor_version)
        VALUES ('f','https://press.test/a','https://press.test/a','press.test','html','ok',?,'test','test')""", (NOW,))
    yield c
    c.close()


def extraction(c, date_text, date_iso=None, published_at='2026-10-02', extraction_id='x'):
    target = c.execute("SELECT target_id FROM target WHERE name_ko='달빛정원' AND kind='restaurant'").fetchone()[0]
    c.execute("""INSERT INTO extraction
        (extraction_id,fetch_id,canonical_url,host,label,epistemic,date_text,date_iso,
         span_start,span_end,quote,extractor,extractor_version,resolved_restaurant_target,published_at,created_at)
        VALUES (?,'f','https://press.test/a','press.test','opening','claim',?,?,0,4,'문을 열었다','rule:opening','test',?,?,?)""",
              (extraction_id, date_text, date_iso, target, published_at, NOW))


@pytest.mark.parametrize('text,iso,published,expected', [
    ('10월 7일', None, '2026-10-02', ('2026-10-07', 'day')),
    ('10월 7일', None, '2026.10.02', ('2026-10-07', 'day')),
    ('October 7', None, '2026-10-02', ('2026-10-07', 'day')),
    ('October 7, 2026', None, None, ('2026-10-07', 'day')),
    ('지난해 5월', None, '2026-10-02', ('2025-05', 'month')),
    ('2025', None, None, ('2025', 'year')),
    ('2025-03', None, None, ('2025-03', 'month')),
    ('10월 7일', '2025-10-07', '2026-10-02', ('2025-10-07', 'day')),
    ('미정', None, '2026-10-02', (None, None)),
    ('10월 7일', None, None, (None, None)),
    ('October 7', None, None, (None, None)),
    ('2월 30일', None, '2026-10-02', (None, None)),
    ('10월 32일', None, '2026-10-02', (None, None)),
    ('2026-13-01', None, '2026-10-02', (None, None)),
    ('미정', '2026-02-30', '2026-10-02', (None, None)),
])
def test_apply_status_normalizes_dates(conn, text, iso, published, expected):
    extraction(conn, text, iso, published)
    assert apply.apply_status(conn, now=NOW)['events'] == 1
    row = conn.execute("SELECT effective_date,effective_precision FROM restaurant_status_event WHERE kind='open'").fetchone()
    assert tuple(row) == expected


def test_apply_status_deduplicates_normalized_date(conn):
    extraction(conn, '2026-10-07')
    assert apply.apply_status(conn, now=NOW)['events'] == 1
    extraction(conn, '10월 7일', extraction_id='y')
    result = apply.apply_status(conn, now=NOW)
    assert result['events'] == 0 and result['skipped'] == 1
    assert conn.execute("SELECT COUNT(*) FROM restaurant_status_event WHERE kind='open'").fetchone()[0] == 1


def legacy_event(c, text, published='2026-10-02'):
    branch = c.execute("SELECT b.branch_id FROM branch b JOIN restaurant r USING(restaurant_id) WHERE r.name='달빛정원'").fetchone()[0]
    claim = create_claim(c, 'status_event', 'source_claim', 'test', now=NOW)
    evidence = add_evidence(c, source_id='press_articles', source_url='https://press.test/a',
                            collected_at=NOW, published_at=published, quote='문을 열었다')
    link_evidence(c, claim, evidence)
    c.execute("""INSERT INTO restaurant_status_event
        (event_id,branch_id,kind,effective_date,effective_precision,claim_id,created_at)
        VALUES ('bad',?,'open',?,'month',?,?)""", (branch, text, claim, NOW))
    return branch, claim


def recompute(c):
    return stages.service_output_recompute(SimpleNamespace(conn=c, env={'today': '2026-11-18'}, run_id=None))


@pytest.mark.parametrize('text,published,expected', [
    ('10월 7일', '2026-10-02', ('2026-10-07', 'day')),
    ('October 7', '2026-10-02', ('2026-10-07', 'day')),
    ('October 7, 2026', None, ('2026-10-07', 'day')),
    ('10월 7일', None, (None, None)),
    ('미정', '2026-10-02', (None, None)),
    ('2026-02-30', '2026-10-02', (None, None)),
])
def test_recompute_repairs_existing_event_and_records_once(conn, text, published, expected):
    branch, _ = legacy_event(conn, text, published)
    recompute(conn)
    assert tuple(conn.execute("SELECT effective_date,effective_precision FROM restaurant_status_event WHERE event_id='bad'").fetchone()) == expected
    output = json.loads(conn.execute("SELECT payload_json FROM service_output WHERE output_key='branch_status' AND subject_id=?", (branch,)).fetchone()[0])
    assert output['state'] == ('open' if expected[0] else 'unknown')
    assert conn.execute("SELECT COUNT(*) FROM service_output WHERE output_key='branch_status'").fetchone()[0] == conn.execute('SELECT COUNT(*) FROM branch').fetchone()[0]
    audit = conn.execute("SELECT * FROM correction_history WHERE reason='malformed_date'").fetchall()
    assert len(audit) == 1
    assert (audit[0]['table_name'], audit[0]['row_id'], audit[0]['old_value'], audit[0]['new_value']) == ('restaurant_status_event', 'bad', text, expected[0])
    recompute(conn)
    assert conn.execute("SELECT COUNT(*) FROM correction_history WHERE reason='malformed_date'").fetchone()[0] == 1


def test_repair_does_not_choose_between_conflicting_publication_years(conn):
    _, claim = legacy_event(conn, '10월 7일')
    link_evidence(conn, claim, add_evidence(conn, source_id='press_articles', source_url='https://other.test/a',
                                          collected_at=NOW, published_at='2025-10-02'))
    recompute(conn)
    assert conn.execute("SELECT effective_date FROM restaurant_status_event WHERE event_id='bad'").fetchone()[0] is None


@pytest.mark.parametrize('field', ['valid_from', 'valid_to'])
def test_recompute_clears_malformed_employment_date(conn, field):
    claim = create_claim(conn, 'employment', 'source_claim', 'test', now=NOW)
    chef = conn.execute('SELECT chef_id FROM chef LIMIT 1').fetchone()[0]
    employment = ontology.add_employment(conn, chef_id=chef, claim_id=claim, employer_label='test', valid_to_kind='open', now=NOW)
    end = ", valid_to_kind='known'" if field == 'valid_to' else ''
    conn.execute(f"UPDATE employment SET {field}='알 수 없음', {field}_precision='day'{end} WHERE employment_id=?", (employment,))
    recompute(conn)
    row = conn.execute('SELECT * FROM employment WHERE employment_id=?', (employment,)).fetchone()
    assert row[field] is None and row[f'{field}_precision'] is None
    assert row['valid_to_kind'] == ('unknown' if field == 'valid_to' else 'open')
    assert conn.execute("SELECT COUNT(*) FROM correction_history WHERE reason='malformed_date' AND row_id=? AND field=?", (employment, field)).fetchone()[0] == 1
    recompute(conn)
    assert conn.execute("SELECT COUNT(*) FROM correction_history WHERE reason='malformed_date' AND row_id=?", (employment,)).fetchone()[0] == 1


def test_recompute_propagates_unrelated_errors(conn, monkeypatch):
    def fail(*args):
        raise RuntimeError('unrelated reader failure')
    monkeypatch.setattr(stages, 'current_employment', fail)
    with pytest.raises(RuntimeError, match='unrelated reader failure'):
        recompute(conn)
