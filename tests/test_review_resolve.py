"""Deterministic review_queue backlog resolver: closes only clear-cut items, never approves a public claim."""
import json
import os
import sqlite3
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'daemon'))

from kg_daemon import db as kgdb                                   # noqa: E402
from kg_daemon.claims import queue_review                          # noqa: E402
from kg_daemon.db import stable_id                                 # noqa: E402
from kg_daemon.review_resolve import noise_reason, open_counts, resolve_review_backlog  # noqa: E402
from kg_daemon.seed_migration import migrate_seed                  # noqa: E402

FIX = os.path.join(ROOT, 'tests', 'fixtures')
NOW = '2026-11-27T00:00:00Z'
ARTICLE = 'https://press.test/news/articleView.html?idxno=1'
LISTING = 'https://press.test/news/articleList.html?sc_area=K&sc_word=x'


@pytest.fixture()
def conn():
    c = kgdb.connect(':memory:')
    kgdb.migrate(c)
    migrate_seed(c, json.load(open(os.path.join(FIX, 'seed_minimal.json'), encoding='utf-8')), '2026-10-27T00:00:00Z')
    c.execute('PRAGMA foreign_keys = OFF')   # fixture rows reference fetches/claims that are irrelevant here
    return c


def career(conn, aid, reason, quote=None, url=ARTICLE, created=NOW):
    conn.execute("""INSERT INTO career_assertion(assertion_id,fetch_id,canonical_url,publisher_group,origin_group,independent_eligible,
                    content_hash,quote,extractor_version,language,state) VALUES (?,?,?,?,?,0,'h',?,'career-span-1','ko','review')""",
                 (aid, 'fch_' + aid, url, 'p', 'p', quote))
    return queue_review(conn, reason, 'career_assertion', aid, {'url': url}, dedupe_key=aid + ':' + reason, now=created)


def state(conn, rid):
    row = conn.execute('SELECT state, payload_json FROM review_queue WHERE review_id=?', (rid,)).fetchone()
    return row['state'], json.loads(row['payload_json']).get('deterministic_resolution', {}).get('rule')


def test_career_validation_failures_without_quote_are_dismissed(conn):
    rid = career(conn, 'a1', 'invalid_task_json')
    rid2 = career(conn, 'a2', 'offset_ambiguous')
    out = resolve_review_backlog(conn, NOW)
    assert out['career_no_quote'] == 2
    assert state(conn, rid) == ('dismissed', 'career_no_quote') and state(conn, rid2)[0] == 'dismissed'


def test_unbound_duplicates_merge_into_article_copy_and_listing_snippets_close(conn):
    q = '홍길동 셰프는 2019년 어딘가에서 일했다.'
    listing = career(conn, 'b1', 'unbound_career', q, LISTING, '2026-11-01T00:00:00Z')
    article = career(conn, 'b2', 'unbound_career', q + ' ', ARTICLE, '2026-11-02T00:00:00Z')   # same wording
    lonely_listing = career(conn, 'b3', 'unbound_career', '다른 문장의 셰프 이야기.', LISTING)
    out = resolve_review_backlog(conn, NOW)
    assert state(conn, article) == ('open', None)                     # the article copy stays for a human
    assert state(conn, listing) == ('dismissed', 'career_duplicate_quote')
    merged = json.loads(conn.execute('SELECT payload_json FROM review_queue WHERE review_id=?', (article,)).fetchone()[0])['merged_duplicates']
    assert [m['review_id'] for m in merged] == [listing]
    assert state(conn, lonely_listing) == ('dismissed', 'career_listing_snippet')
    assert out['career_duplicate_quote'] == 1 and out['career_listing_snippet'] == 1


def test_unbound_covered_only_when_chef_already_holds_every_named_venue(conn):
    covered = career(conn, 'c1', 'unbound_career', '키친B를 이끄는 가상일 셰프.')
    unknown_venue = career(conn, 'c2', 'unbound_career', '가상일 셰프는 키친B와 키친A를 거쳐 새 식당을 열었다.')
    past = career(conn, 'c3', 'unbound_career', '가상일 셰프(전 키친B 총괄 셰프)는 독립했다.')
    other_person = career(conn, 'c4', 'unbound_career', '키친B에서 가상일 셰프와 함께 일한 김철수 셰프.')
    no_venue = career(conn, 'c5', 'unbound_career', '가상일 셰프는 요리를 사랑한다.')
    resolve_review_backlog(conn, NOW)
    assert state(conn, covered) == ('resolved', 'career_covered')
    for rid in (unknown_venue, past, other_person, no_venue):
        assert state(conn, rid) == ('open', None)
    # Nothing about the genome changed: no new claim, employment or published assertion.
    assert conn.execute("SELECT COUNT(*) FROM career_assertion WHERE state<>'review'").fetchone()[0] == 0


def test_resolver_never_creates_claims_or_employment(conn):
    claims = conn.execute('SELECT COUNT(*) FROM claim').fetchone()[0]
    emps = conn.execute('SELECT COUNT(*) FROM employment').fetchone()[0]
    career(conn, 'd1', 'unbound_career', '키친B를 이끄는 가상일 셰프.')
    resolve_review_backlog(conn, NOW)
    assert conn.execute('SELECT COUNT(*) FROM claim').fetchone()[0] == claims
    assert conn.execute('SELECT COUNT(*) FROM employment').fetchone()[0] == emps


def target(conn, name, kind, status='candidate', payload=None):
    tid = stable_id('tgt', kind, name)
    conn.execute("INSERT INTO target(target_id,kind,name_ko,origin,discovered_at,status) VALUES (?,?,?,'discovered',?,?)", (tid, kind, name, NOW, status))
    rid = queue_review(conn, 'candidate_target', 'target', tid, payload or {'name': name}, dedupe_key='cand:' + tid, now=NOW)
    return tid, rid


def test_candidate_noise_rejected_known_chef_resolved_real_names_left_open(conn):
    t1, show = target(conn, '흑백요리사2', 'restaurant')
    t2, generic = target(conn, '유명 레스토랑', 'restaurant')
    t3, person = target(conn, '최연소', 'chef')
    t4, real = target(conn, '홍길동', 'chef')
    t5, venue = target(conn, '서라벌', 'restaurant')
    t6, known = target(conn, '가상이', 'chef')
    chef = conn.execute('SELECT chef_id FROM chef LIMIT 1').fetchone()[0]
    conn.execute('INSERT INTO target_entity(target_id, chef_id) VALUES (?,?)', (t6, chef))
    t7, held = target(conn, '마스터', 'chef', payload={'chatgpt_review': {'decision': 'ambiguous', 'applied': {'left_open': True}}})
    t8, already = target(conn, '서울', 'chef', status='rejected')
    resolve_review_backlog(conn, NOW)
    for rid, tid in ((show, t1), (generic, t2), (person, t3)):
        assert state(conn, rid) == ('dismissed', 'candidate_rejected')
        assert conn.execute('SELECT status FROM target WHERE target_id=?', (tid,)).fetchone()[0] == 'rejected'
    assert state(conn, real) == ('open', None) and state(conn, venue) == ('open', None)
    assert state(conn, known) == ('resolved', 'candidate_known_chef')
    assert conn.execute('SELECT status FROM target WHERE target_id=?', (t6,)).fetchone()[0] == 'candidate'
    assert state(conn, held) == ('open', None)                         # human hold is respected
    assert state(conn, already) == ('dismissed', 'candidate_rejected')


def test_noise_reason_is_narrow():
    assert noise_reason('restaurant', '흑백요리사') and noise_reason('restaurant', '아시아 50 베스트 레스토랑')
    assert noise_reason('restaurant', '캐나다') and noise_reason('chef', '부부')
    for name in ('서라벌', '스타일', '미국식당', '레스토랑 오네뜨'):
        assert noise_reason('restaurant', name) is None
    assert noise_reason('chef', '마스터셰프김') is None


def conflict(conn, cid, claim_a, claim_b, subject='emp_x', created=NOW, review=True):
    conn.execute("""INSERT INTO conflict(conflict_id,subject_type,subject_id,field,claim_a,claim_b,opened_at)
                    VALUES (?,'employment',?,'employment.current_vs_past',?,?,?)""", (cid, subject, claim_a, claim_b, created))
    rid = queue_review(conn, 'conflict', 'conflict', cid, {'quote': 'q ' + cid}, dedupe_key='conflict:' + cid, now=created) if review else None
    qid = queue_review(conn, 'quality:open_conflict', 'quality', cid, {'kind': 'open_conflict'}, dedupe_key=f'quality:open_conflict:{cid}:2026-11', now=created)
    return rid, qid


def test_duplicate_conflicts_collapse_to_one_item_and_conflict_rows_stay_open(conn):
    r1, q1 = conflict(conn, 'cfl_1', 'clm_a1', 'clm_b', created='2026-11-01T00:00:00Z')
    r2, q2 = conflict(conn, 'cfl_2', 'clm_a2', 'clm_b', created='2026-11-02T00:00:00Z')
    r3, q3 = conflict(conn, 'cfl_3', 'clm_a3', 'clm_other', created='2026-11-03T00:00:00Z')
    _, qb1 = conflict(conn, 'cfl_4', 'clm_c1', 'clm_d', subject='brn_y', review=False)
    _, qb2 = conflict(conn, 'cfl_5', 'clm_c2', 'clm_d', subject='brn_y', review=False, created='2026-11-28T00:00:00Z')
    resolve_review_backlog(conn, NOW)
    assert state(conn, r1) == ('open', None) and state(conn, r3) == ('open', None)
    assert state(conn, r2) == ('dismissed', 'conflict_duplicate')
    merged = json.loads(conn.execute('SELECT payload_json FROM review_queue WHERE review_id=?', (r1,)).fetchone()[0])['merged_duplicates']
    assert merged[0]['conflict_id'] == 'cfl_2' and merged[0]['quote'] == 'q cfl_2'
    for q in (q1, q2, q3):
        assert state(conn, q)[0] == 'dismissed'                         # covered by the conflict review item
    assert state(conn, qb1) == ('open', None) and state(conn, qb2) == ('dismissed', 'quality_conflict_dup')
    assert conn.execute("SELECT COUNT(*) FROM conflict WHERE state='open'").fetchone()[0] == 5


def test_heavy_guard_items_close_only_for_terminal_frontier(conn, tmp_path):
    heavy = tmp_path / 'heavy_queue.db'
    h = sqlite3.connect(heavy)
    h.execute('CREATE TABLE frontier(url TEXT PRIMARY KEY, host TEXT, canonical_url TEXT, state TEXT, status INTEGER, error TEXT)')
    rows = {'https://a.test/1': 'failed', 'https://a.test/2': 'done', 'https://a.test/3': 'queued'}
    rids = {}
    for url, st in rows.items():
        h.execute('INSERT INTO frontier VALUES (?,?,?,?,500,?)', (url, 'a.test', url, st, 'attempts_exhausted'))
        rids[st] = queue_review(conn, 'guard_attempts_exhausted', 'heavy_frontier', stable_id('url', url), {'stage': 'heavy_fetch'},
                                dedupe_key=stable_id('guard-heavy', url), now=NOW)
    h.commit(); h.close()
    resolve_review_backlog(conn, NOW, heavy_db=str(heavy))
    assert state(conn, rids['failed']) == ('dismissed', 'heavy_fetch_terminal')
    assert state(conn, rids['done']) == ('resolved', 'heavy_fetch_terminal')
    assert state(conn, rids['queued']) == ('open', None)


def test_idempotent_and_dismissals_survive_requeue(conn):
    rid = career(conn, 'e1', 'quote_missing')
    first = resolve_review_backlog(conn, NOW)
    assert first and resolve_review_backlog(conn, NOW) == {}
    # The collector re-queues with INSERT OR IGNORE, so a closed item does not come back.
    queue_review(conn, 'quote_missing', 'career_assertion', 'e1', {}, dedupe_key='e1:quote_missing')
    assert state(conn, rid)[0] == 'dismissed'
    assert sum(open_counts(conn).values()) == conn.execute("SELECT COUNT(*) FROM review_queue WHERE state='open'").fetchone()[0]


def test_quality_check_stage_runs_the_resolver(conn):
    from kg_daemon.runner import StageContext
    from kg_daemon.stages import quality_check
    rid = career(conn, 'f1', 'invalid_task_json')
    conn.commit()
    ctx = StageContext.__new__(StageContext)
    ctx.conn, ctx.env, ctx.config = conn, {'today': '2026-11-27'}, {}
    try:
        res = quality_check(ctx)
    except Exception as exc:   # a seed invariant violation is out of scope; the resolver must still have run first
        res = None
        assert 'NonRetryable' in type(exc).__name__
    assert state(conn, rid) == ('dismissed', 'career_no_quote')
    if res is not None:
        assert res.output['review_backlog_closed']['career_no_quote'] == 1
