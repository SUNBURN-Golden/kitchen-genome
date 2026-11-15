from tests_balance_fake import healthy_balance
"""DeepSeek review stage: approved AI findings publish with provenance + rollback log; rejected ones close; cap respected."""
import json
import os
import subprocess
import sys
from pathlib import Path
import pytest
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'daemon'))
sys.path.insert(0, str(ROOT / 'tests'))
from collect_world import SEED
from kg_ai import review as rv
from kg_ai.budget import Budget
from kg_ai.deepseek import Client
from kg_collect import cycle
from kg_collect.deepseek import normalize_finding
from kg_daemon import db
from kg_daemon.claims import queue_review
from kg_daemon.runner import CostCapExceeded
from test_deepseek_client import meter, response

TEXT = ('서울 한남동의 달빛정원은 2026-10-01 문을 열었다. 한도윤 셰프가 총괄 셰프를 맡는다. '
        '드라마 속 레스토랑 파밀리아는 폐업 직전에 놓여 있다. 한도윤 셰프가 총괄 셰프를 맡는다.')


@pytest.fixture
def env(tmp_path):
    conn = db.connect(str(tmp_path / 'state.db')); db.migrate(conn)
    cycle.init_db(conn, SEED)
    raw = tmp_path / 'raw'; (raw / 'h').mkdir(parents=True)
    (raw / 'h' / 't.txt').write_text(TEXT, encoding='utf-8')
    conn.execute("""INSERT INTO fetch_record(fetch_id,url,canonical_url,host,method,outcome,text_ref,fetched_at,fetcher_version,extractor_version)
                    VALUES ('f','https://press.test/a','https://press.test/a','press.test','html','ok','h/t.txt',?,'fixture','rules-new')""", (db.utcnow(),))
    conn.commit()
    seed = {'kitchens': [{'id': 'k1', 'name': '모수', 'en': 'Mosu'}],
            'restaurants': [{'id': 'r1', 'name': '달빛정원', 'chef': '한도윤', 'kitchen': 'k1'}, {'id': 'r2', 'name': '다른집', 'chef': '김다른'}]}
    sp = tmp_path / 'seed.json'; sp.write_text(json.dumps(seed, ensure_ascii=False), encoding='utf-8')
    return conn, sp, tmp_path / 'log.jsonl', str(raw)


def add(conn, xid, label, quote, **kw):
    start = TEXT.find(quote) if quote in TEXT else 0
    conn.execute("""INSERT INTO extraction(extraction_id,fetch_id,canonical_url,host,label,epistemic,chef_text,restaurant_text,org_text,role_text,date_text,
                    span_start,span_end,quote,extractor,extractor_version,published_at,created_at,asserted_by,pending_only)
                    VALUES (?,?,?,?,?,'inference',?,?,?,?,?,?,?,?,'deepseek:web','web-extract-1','2026-10-02',?,'ai_inference',1)""",
                 (xid, 'f', 'https://press.test/a', 'press.test', label, kw.get('chef'), kw.get('rest'), kw.get('org'), kw.get('role'), kw.get('date'),
                  start, start + len(quote), quote, db.utcnow()))
    queue_review(conn, 'ai_inference', 'extraction', xid, {'asserted_by': 'ai_inference'}, dedupe_key='ai:extraction:' + xid)
    conn.commit()


def client_for(conn, decisions, calls):
    def transport(payload, key):
        body = json.loads(payload); calls.append(body)
        data = json.loads(body['messages'][2]['content'])
        assert body['temperature'] == 0 and body['thinking'] == {'type': 'disabled'} and body['model'] == 'deepseek-chat'
        return response({'reviews': [{'id': f['id'], 'decision': decisions[f['id']], 'reason': 'ok' if decisions[f['id']] == 'approve' else 'fiction'}
                                     for f in data['findings']]})
    return Client(Budget(meter(conn)), key='dummy', transport=transport, balance_transport=healthy_balance)


def test_approve_publishes_with_badge_and_reject_closes(env):
    conn, sp, log, raw = env
    add(conn, 'x_open', 'opening', '서울 한남동의 달빛정원은 2026-10-01 문을 열었다', rest='달빛정원', date='2026-10-01')
    add(conn, 'x_fic', 'closure', '드라마 속 레스토랑 파밀리아는 폐업 직전에 놓여 있다', rest='파밀리아')
    add(conn, 'x_rep', 'employment_claim', '한도윤 셰프가 총괄 셰프를 맡는다', chef='한도윤', role='총괄 셰프')
    add(conn, 'x_bad', 'menu', '이 문장은 원문에 없다', rest='달빛정원')
    calls = []
    s = rv.review(conn, str(sp), str(log), raw, client=client_for(conn, {'x_open': 'approve', 'x_fic': 'reject', 'x_rep': 'approve'}, calls))
    assert (s['approved'], s['rejected'], s['prechecked_rejects'], s['remaining'], s['stopped']) == (2, 2, 1, 0, None)
    assert len(calls) == 1                                    # one batched request per source document
    sent = json.loads(calls[0]['messages'][2]['content'])['findings']
    rep = next(f for f in sent if f['id'] == 'x_rep')
    assert len(rep['occurrences']) == 2                       # repeated quote: reviewer sees every occurrence
    assert next(f for f in sent if f['id'] == 'x_open')['site_record']['name'] == '달빛정원'
    seed = json.loads(sp.read_text(encoding='utf-8'))
    pub = seed['restaurants'][0]['ai_reviewed']
    assert {p['extraction_id'] for p in pub} == {'x_open', 'x_rep'}
    assert all(p['badge'] == 'AI 검토' and p['src'] == 'https://press.test/a' and p['quote'] in TEXT for p in pub)
    assert 'ai_reviewed' not in seed['restaurants'][1] and seed['restaurants'][0]['chef'] == '한도윤'
    states = dict(conn.execute("SELECT subject_id, state FROM review_queue WHERE reason='ai_inference'").fetchall())
    assert states == {'x_open': 'resolved', 'x_rep': 'resolved', 'x_fic': 'dismissed', 'x_bad': 'dismissed'}
    lines = [json.loads(l) for l in log.read_text(encoding='utf-8').splitlines()]
    assert {l['extraction_id']: l['action'] for l in lines} == {'x_open': 'approve', 'x_rep': 'approve', 'x_fic': 'reject', 'x_bad': 'reject'}
    assert next(l for l in lines if l['extraction_id'] == 'x_bad')['reason'].startswith('quote_not_in_source')
    # cost is metered against the shared AI day ledger
    assert conn.execute("SELECT COUNT(*) FROM ai_request WHERE task='ai-review-2' AND state='done'").fetchone()[0] == 1
    # rerun: nothing open, nothing re-sent, no duplicate entries
    s2 = rv.review(conn, str(sp), str(log), raw, client=client_for(conn, {}, calls))
    assert s2['remaining'] == 0 and len(calls) == 1
    # rollback removes exactly the published entry and logs it
    out = subprocess.run([sys.executable, str(ROOT / 'scripts/ai_review_rollback.py'), '--extraction-id', 'x_rep', '--seed', str(sp), '--log', str(log)],
                         capture_output=True, text=True, check=True)
    assert json.loads(out.stdout) == {'removed': 1}
    seed = json.loads(sp.read_text(encoding='utf-8'))
    assert [p['extraction_id'] for p in seed['restaurants'][0]['ai_reviewed']] == ['x_open']
    assert json.loads(log.read_text(encoding='utf-8').splitlines()[-1])['action'] == 'rollback'


def test_unlinked_approval_and_cap_stop_leaves_rest_open(env):
    conn, sp, log, raw = env
    add(conn, 'x1', 'opening', '서울 한남동의 달빛정원은 2026-10-01 문을 열었다', chef=None, rest=None)
    s = rv.review(conn, str(sp), str(log), raw, client=client_for(conn, {'x1': 'approve'}, []), daily_cap=0)
    assert s['stopped'] == 'cost_cap' and s['remaining'] == 1 and s['approved'] == 0
    s = rv.review(conn, str(sp), str(log), raw, client=client_for(conn, {'x1': 'approve'}, []), daily_cap=20)
    assert s['approved'] == 1 and s['remaining'] == 0
    seed = json.loads(sp.read_text(encoding='utf-8'))
    # no restaurant/chef text: matched by nothing -> published to the unlinked list, still badged and sourced
    assert seed['ai_reviewed_unlinked'][0]['badge'] == 'AI 검토'


def test_review_batch_splitting_preserves_schema_rejection_brake(env):
    conn, sp, log, raw = env
    add(conn, 'x_open', 'opening', '서울 한남동의 달빛정원은 2026-10-01 문을 열었다', rest='달빛정원', date='2026-10-01')
    add(conn, 'x_rep', 'employment_claim', '한도윤 셰프가 총괄 셰프를 맡는다', chef='한도윤', role='총괄 셰프')
    calls, qcalls = [], []
    client = Client(Budget(meter(conn)), key='dummy',
                    transport=lambda *a: calls.append(1) or response({'reviews': 'invalid'}),
                    balance_transport=healthy_balance, fallback_key='fixture-backup',
                    fallback_transport=lambda *a: qcalls.append(1) or response())
    stats = rv.review(conn, str(sp), str(log), raw, client=client)
    assert len(calls) == 6 and qcalls == []
    assert client.stopped == stats['stopped'] == 'schema_reject_limit'
    assert client.status == 'paused' and stats['remaining'] == 1 and stats['published'] == 0


def test_daily_cap_is_clamped_to_20(env):
    conn, sp, log, raw = env
    add(conn, 'x1', 'opening', '서울 한남동의 달빛정원은 2026-10-01 문을 열었다', rest='달빛정원')
    s = rv.review(conn, str(sp), str(log), raw, client=client_for(conn, {'x1': 'approve'}, []), daily_cap=500)
    run = conn.execute('SELECT cost_cap_units FROM daemon_run WHERE run_id=?', (s['run_id'],)).fetchone()[0]
    assert run == 20


def test_daily_cap_override_env_bypasses_clamp(env, monkeypatch):
    conn, sp, log, raw = env
    add(conn, 'x1', 'opening', '서울 한남동의 달빛정원은 2026-10-01 문을 열었다', rest='달빛정원')
    monkeypatch.setenv('KG_AI_DAILY_COST_CAP_OVERRIDE', '500')
    s = rv.review(conn, str(sp), str(log), raw, client=client_for(conn, {'x1': 'approve'}, []), daily_cap=500)
    run = conn.execute('SELECT cost_cap_units FROM daemon_run WHERE run_id=?', (s['run_id'],)).fetchone()[0]
    assert run == 500 and s['approved'] == 1 and s['stopped'] is None
    # Budget must honor the override too, or reservations would still stop at 20.
    b = Budget(meter(conn, cap=500), daily_cap=500)
    assert b.daily_cap == 500
    assert b.reserve('over20', 'task', 'deepseek-chat', 50_000) is True   # 25 units: impossible under the clamp
    monkeypatch.setenv('KG_AI_DAILY_COST_CAP_OVERRIDE', '')
    assert Budget(meter(conn), daily_cap=500).daily_cap == 20
    monkeypatch.delenv('KG_AI_DAILY_COST_CAP_OVERRIDE')
    b2 = Budget(meter(conn, cap=500), daily_cap=500)
    assert b2.daily_cap == 20
    with pytest.raises(CostCapExceeded):
        b2.reserve('over20b', 'task', 'deepseek-chat', 50_000)
    monkeypatch.setenv('KG_AI_DAILY_COST_CAP_OVERRIDE', '-5')
    assert Budget(meter(conn), daily_cap=500).daily_cap == 0


def test_repeated_identical_quote_no_longer_aborts_extraction():
    text = '달빛정원 소식. 다른 문장. 달빛정원 소식.'
    out = normalize_finding({'label': 'closure', 'quote': '달빛정원 소식', 'span_start': 99, 'span_end': 100}, text)
    assert (out['span_start'], out['span_end']) == (0, 7) and out['ambiguity'] == 'quote_repeats=2'


def test_pages_render_ai_badge(tmp_path):
    src = (ROOT / 'scripts/build_pages.py').read_text(encoding='utf-8')
    assert 'AI 검토' in src and 'aisec(r)' in src and 'aisec(k)' in src
    assert 'AI-reviewed' in (ROOT / 'scripts/build_pages_en.py').read_text(encoding='utf-8')
