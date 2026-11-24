import json
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'daemon'))

from kg_collect.extract import clean_name, new_chef_names          # noqa: E402
from kg_collect.review import reject_reason                        # noqa: E402
from kg_daemon import db as kgdb                                   # noqa: E402
from kg_daemon.chef_resolve import (                               # noqa: E402
    classify_chef_name, match_chef, resolve_open_unknown_chefs,
)
from kg_daemon.claims import queue_review                          # noqa: E402
from kg_daemon.seed_migration import migrate_seed                  # noqa: E402
from kg_daemon.stages import entity_resolution                     # noqa: E402
from kg_daemon.runner import StageContext                          # noqa: E402

FIX = os.path.join(ROOT, 'tests', 'fixtures')


@pytest.fixture()
def conn():
    c = kgdb.connect(':memory:')
    kgdb.migrate(c)
    migrate_seed(c, json.load(open(os.path.join(FIX, 'seed_minimal.json'), encoding='utf-8')), '2026-10-27T00:00:00Z')
    return c


def chef_id(conn, name, origin=None):
    if origin is None:
        return conn.execute('SELECT chef_id FROM chef WHERE display_name=?', (name,)).fetchone()[0]
    return conn.execute('SELECT chef_id FROM chef WHERE display_name=? AND disambiguator=?', (name, origin)).fetchone()[0]


def test_classify_strips_role_parenthetical_and_splits_coordinated_names():
    assert classify_chef_name('김범주 (파티시에)').status == 'single'
    assert classify_chef_name('김범주 (파티시에)').names == ('김범주',)
    classified = classify_chef_name('박지현·윤송이 (파티시에)')
    assert classified.status == 'multiple' and classified.names == ('박지현', '윤송이')
    assert classify_chef_name('테스트셰프E (+부가 메모)').names == ('테스트셰프E',)
    for raw in ('서울', '한국인', '가상 출신 셰프 (이름 미기재)', '', None):
        assert classify_chef_name(raw).status == 'reject'


def test_extract_and_review_refuse_place_and_nationality_as_chef_names():
    assert clean_name('서울') is None and clean_name('한국인') is None
    assert reject_reason('chef', '서울') == 'generic_name'
    assert reject_reason('chef', '한국인') == 'generic_name'
    assert reject_reason('chef', '여경옥') is None
    sentence = '모수 서울의 안성재 셰프는 이날 수상 결과와 상관없이 말했다.'
    surfaces = [m['surface'] for m in new_chef_names(sentence, [])]
    assert '서울' not in surfaces
    assert '안성재' in surfaces
    assert all(m['surface'] != '한국인' for m in new_chef_names('이번 발표에서 한국인 셰프 가운데 가장 높은 순위는', []))


def test_match_existing_chef_by_cleaned_name_and_english_alias(conn):
    eid = chef_id(conn, '테스트셰프E')
    assert match_chef(conn, '테스트셰프E (+부가 메모)', 'kb').status == 'matched'
    assert match_chef(conn, '테스트셰프E (+부가 메모)', 'kb').chef_id == eid
    cid = chef_id(conn, '테스트셰프C')
    conn.execute("UPDATE chef SET display_name_en='Test Chef C' WHERE chef_id=?", (cid,))
    conn.commit()
    result = match_chef(conn, 'Test Chef C', 'ka')
    assert result.status == 'matched' and result.chef_id == cid


def test_match_never_guesses_unknown_homonym_or_multiple(conn):
    assert match_chef(conn, '없는셰프', 'ka').status == 'unknown'
    assert match_chef(conn, '박지현·윤송이 (파티시에)').status == 'multiple'
    # Two people share a display name; a different origin must not pick one.
    conn.execute("INSERT INTO chef(chef_id,display_name,disambiguator,created_at) VALUES ('chf_dup','테스트셰프B','other','2026-10-27T00:00:00Z')")
    conn.commit()
    assert match_chef(conn, '테스트셰프B', 'missing').status == 'homonym'
    assert match_chef(conn, '테스트셰프B').status == 'homonym'
    assert match_chef(conn, '테스트셰프B', 'ka').status == 'matched'


def _guide_extraction(conn, fetch_id, host, url, chef_text, target_id, mapped_chef, now='2026-10-27T00:00:00Z'):
    conn.execute("""INSERT INTO fetch_record(fetch_id,url,canonical_url,host,method,outcome,fetcher_version,extractor_version,fetched_at)
                    VALUES (?,?,?,?, 'html','ok','t','t',?)""", (fetch_id, url, url, host, now))
    conn.execute("""INSERT INTO target(target_id,kind,name_ko,name_en,origin,discovered_at,status,attrs_json)
                    VALUES (?, 'chef', ?, NULL, 'seed', ?, 'active', '{}')""", (target_id, chef_text, now))
    conn.execute('INSERT INTO target_entity(target_id, chef_id) VALUES (?,?)', (target_id, mapped_chef))
    conn.execute("""INSERT INTO extraction(extraction_id,fetch_id,canonical_url,host,label,epistemic,chef_text,
                    span_start,span_end,quote,extractor,extractor_version,resolved_chef_target,created_at)
                    VALUES (?,?,?,?, 'employment_claim','fact',?, 0, 4, '인용', 'rule:test','t', ?, ?)""",
                 ('ex_' + fetch_id, fetch_id, url, host, chef_text, target_id, now))


def test_match_uses_stored_wikipedia_and_michelin_spellings(conn):
    cid = chef_id(conn, '테스트셰프A')
    conn.execute("UPDATE chef SET display_name_en=NULL WHERE chef_id=?", (cid,))
    _guide_extraction(conn, 'f_wiki', 'ko.wikipedia.org', 'https://ko.wikipedia.org/wiki/테스트셰프A',
                      'Test Chef A', 'tgt_wiki_a', cid)
    _guide_extraction(conn, 'f_mich', 'guide.michelin.com', 'https://guide.michelin.com/kr/ko/restaurant/x',
                      'Mingoo Test', 'tgt_mich_a', cid)
    conn.commit()
    wiki = match_chef(conn, 'Test Chef A')
    assert wiki.status == 'matched' and wiki.chef_id == cid and wiki.source == 'wikipedia'
    mich = match_chef(conn, 'Mingoo Test')
    assert mich.status == 'matched' and mich.chef_id == cid and mich.source == 'michelin'


def _queue_unknown(conn, cid, name, origin=None, hold=False, now='2026-10-27T00:00:00Z'):
    payload = {'chef_name': name, 'origin_kitchen': origin, 'target_kitchen_id': origin}
    ev = {'source_url': 'https://press.example.com/x', 'collected_at': now, 'quote': '인용'}
    conn.execute("""INSERT INTO candidate_fact(candidate_id,idempotency_key,source_id,fact_type,payload_json,evidence_json,
                    extractor,asserted_by,resolution,created_at)
                    VALUES (?,?, 'press_articles', 'employment', ?, ?, 'rule:test', 'source', 'needs_review', ?)""",
                 (cid, 'idem:' + cid, json.dumps(payload, ensure_ascii=False), json.dumps(ev, ensure_ascii=False), now))
    body = {'fact_type': 'employment', 'payload': payload}
    if hold:
        body['chatgpt_review'] = {
            'decision': 'ambiguous',
            'applied': {'left_open': True, 'awaiting_chef_confirmation': True},
        }
    queue_review(conn, 'unknown_chef', 'candidate_fact', cid, body, dedupe_key=f'unknown_chef:{cid}', now=now)
    return cid


def test_queue_resolver_dismisses_non_person_resolves_unique_match_and_respects_holds(conn):
    conn.execute("""INSERT INTO target(target_id,kind,name_ko,name_en,origin,discovered_at,status,attrs_json)
                    VALUES ('tgt_seoul', 'chef', '서울', NULL, 'discovered', '2026-10-27T00:00:00Z', 'candidate', '{}')""")
    _queue_unknown(conn, 'cnd_seoul', '서울')
    _queue_unknown(conn, 'cnd_kr', '한국인')
    _queue_unknown(conn, 'cnd_e', '테스트셰프E (+부가 메모)', 'kb')
    _queue_unknown(conn, 'cnd_hold', '테스트셰프E (+부가 메모)', 'kb', hold=True)
    _queue_unknown(conn, 'cnd_multi', '박지현·윤송이 (파티시에)', 'kb')
    _queue_unknown(conn, 'cnd_miss', '없는셰프', 'ka')
    conn.commit()
    stats = resolve_open_unknown_chefs(conn, now='2026-11-24T01:00:00Z')
    conn.commit()
    assert stats['dismissed'] == 2 and stats['resolved'] == 1 and stats['held'] == 1
    assert stats['left_open'] == 2 and stats['targets_rejected'] == 1
    assert conn.execute("SELECT state FROM review_queue WHERE subject_id='cnd_seoul'").fetchone()[0] == 'dismissed'
    assert conn.execute("SELECT resolution FROM candidate_fact WHERE candidate_id='cnd_seoul'").fetchone()[0] == 'rejected'
    assert conn.execute("SELECT status FROM target WHERE target_id='tgt_seoul'").fetchone()[0] == 'rejected'
    resolved = conn.execute("SELECT state, payload_json FROM review_queue WHERE subject_id='cnd_e'").fetchone()
    assert resolved['state'] == 'resolved'
    note = json.loads(resolved['payload_json'])['deterministic_resolution']
    assert note['decision'] == 'resolved' and note['chef_id'] == chef_id(conn, '테스트셰프E')
    assert conn.execute("SELECT resolved_chef_id, resolution FROM candidate_fact WHERE candidate_id='cnd_e'").fetchone()[0] == chef_id(conn, '테스트셰프E')
    held = json.loads(conn.execute("SELECT payload_json FROM review_queue WHERE subject_id='cnd_hold'").fetchone()[0])
    assert conn.execute("SELECT state FROM review_queue WHERE subject_id='cnd_hold'").fetchone()[0] == 'open'
    assert held['deterministic_resolution']['decision'] == 'held'
    assert conn.execute("SELECT resolution FROM candidate_fact WHERE candidate_id='cnd_hold'").fetchone()[0] == 'needs_review'
    open_ids = {r[0] for r in conn.execute("SELECT subject_id FROM review_queue WHERE reason='unknown_chef' AND state='open'")}
    assert open_ids == {'cnd_hold', 'cnd_multi', 'cnd_miss'}


def test_entity_resolution_rejects_non_person_and_resolves_cleaned_alias(conn):
    now = '2026-10-27T00:00:00Z'
    ev = json.dumps({'source_url': 'https://press.example.com/n', 'collected_at': now, 'quote': '인용'})
    rows = [
        ('cnd_new_e', '테스트셰프E (+부가 메모)', 'kb'),
        ('cnd_new_seoul', '서울', None),
        ('cnd_new_miss', '없는셰프', 'ka'),
    ]
    for cid, name, origin in rows:
        payload = json.dumps({'chef_name': name, 'origin_kitchen': origin, 'target_kitchen_id': 'r1'}, ensure_ascii=False)
        conn.execute("""INSERT INTO candidate_fact(candidate_id,idempotency_key,source_id,fact_type,payload_json,evidence_json,
                        extractor,asserted_by,resolution,created_at)
                        VALUES (?,?, 'press_articles', 'employment', ?, ?, 'rule:test', 'source', 'pending', ?)""",
                     (cid, 'idem:' + cid, payload, ev, now))
    conn.commit()
    result = entity_resolution(StageContext(conn=conn, run_id='run_test', stage='entity_resolution',
                                           config={}, cost=None, prev={}, cursor={}, env={}))
    conn.commit()
    assert result.output['rejected_non_person'] == 1
    assert conn.execute("SELECT resolution, resolved_chef_id FROM candidate_fact WHERE candidate_id='cnd_new_e'").fetchone()[0] == 'resolved'
    assert conn.execute("SELECT resolved_chef_id FROM candidate_fact WHERE candidate_id='cnd_new_e'").fetchone()[0] == chef_id(conn, '테스트셰프E')
    assert conn.execute("SELECT resolution FROM candidate_fact WHERE candidate_id='cnd_new_seoul'").fetchone()[0] == 'rejected'
    assert conn.execute("SELECT resolution FROM candidate_fact WHERE candidate_id='cnd_new_miss'").fetchone()[0] == 'needs_review'
    assert conn.execute("SELECT reason FROM review_queue WHERE subject_id='cnd_new_miss'").fetchone()[0] == 'unknown_chef'
