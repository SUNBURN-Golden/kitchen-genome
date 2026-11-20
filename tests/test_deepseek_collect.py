from tests_balance_fake import healthy_balance
"""Source-backed fixtures exercise planning -> inference -> review isolation and web limits."""
import json
import sys
from pathlib import Path
from types import SimpleNamespace
import pytest
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'daemon'))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tests'))
from collect_world import SEED
from kg_ai.budget import Budget
from kg_ai.deepseek import Client, DeepSeekError, QWEN_MODEL, TaskReject, reservation_tokens
from kg_collect import cycle, gaps, apply, discover
from kg_collect.deepseek import EXTRACT_INSTRUCTION, Adapters, validate_extraction
from kg_collect.rawstore import RawStore
from kg_collect.pipeline import process_doc
from kg_collect.targets import Gazetteer, load_seed_targets
from kg_collect.net import PoliteNet, Response
from kg_collect.limits import record_dispatch
from kg_daemon import db, stages, collect_bridge
from kg_daemon.runner import Daemon
from test_deepseek_client import envelope, meter, response


@pytest.fixture
def world(tmp_path):
    conn = db.connect(str(tmp_path/'state.db')); db.migrate(conn)
    cycle.init_db(conn, SEED)
    conn.execute("INSERT INTO fetch_record(fetch_id,url,canonical_url,host,method,outcome,fetched_at,fetcher_version,extractor_version) VALUES ('f','https://official.test/1','https://official.test/1','official.test','html','ok',?,'fixture','rules-new')", (db.utcnow(),))
    conn.commit()
    return conn


def doc(text, suffix='1'):
    return SimpleNamespace(text=text, url='https://official.test/'+suffix, canonical_url='https://official.test/'+suffix,
                           host='official.test', content_hash='hash'+suffix, published_at=None, items=[], fetch_id='f', ocr_conf=None)


def finding(text, **extra):
    return {'label':'closure','chef_text':None,'restaurant_text':'달빛정원','org_text':None,'role_text':None,'date_text':None,
            'quote':text,'span_start':0,'span_end':len(text),'ambiguity':None, **extra}


def adapter(conn, data):
    return Adapters(Client(Budget(meter(conn)), key='dummy', transport=lambda *a: response(data), balance_transport=healthy_balance))


def test_validate_extraction_keeps_valid_rows_and_reports_each_dropped_index():
    text = '달빛정원의 운영 소식입니다.'
    payload = {'findings': [
        finding(text, quote='not on the page'),
        finding(text),
        finding(text, date_text='2026-11-05'),
        None,
        finding('운영 소식', label='menu'),
        finding(text, label='unknown'),
    ]}
    dropped = []
    rows = validate_extraction(payload, text, dropped=dropped)
    assert [r['quote'] for r in rows] == [text, '운영 소식']
    assert all(text[r['span_start']:r['span_end']] == r['quote'] for r in rows)
    assert dropped == [
        '0:quote_not_exact,quote_len=15,in_text=0',
        '2:date_not_verbatim',
        '3:shape_invalid,finding=not_object',
        '5:label_invalid,label=unknown',
    ]
    assert validate_extraction(payload, text) == rows


def test_validate_extraction_all_invalid_preserves_primary_code_and_every_miss():
    text = '달빛정원의 운영 소식입니다.'
    with pytest.raises(TaskReject) as rejected:
        validate_extraction({'findings': [
            finding(text, quote='not on the page'),
            finding(text, date_text='2026-11-05'),
            None,
        ]}, text)
    assert rejected.value.code == 'quote_not_exact'
    assert rejected.value.detail == (
        '0:quote_not_exact,quote_len=15,in_text=0 '
        '1:date_not_verbatim 2:shape_invalid,finding=not_object'
    )


@pytest.mark.parametrize('payload,code,detail', [
    (None, 'shape_invalid', 'findings=missing'),
    ([], 'shape_invalid', 'findings=missing'),
    ({}, 'shape_invalid', 'findings=missing'),
    ({'findings': None}, 'shape_invalid', 'findings=missing'),
    ({'findings': {}}, 'shape_invalid', 'findings=missing'),
    ({'findings': [finding('달빛정원')] * 9}, 'too_many_findings', 'n=9'),
    ({'findings': [None] * 9}, 'too_many_findings', 'n=9'),
])
def test_validate_extraction_structural_rejections_are_unchanged(payload, code, detail):
    with pytest.raises(TaskReject) as rejected:
        validate_extraction(payload, '달빛정원')
    assert (rejected.value.code, rejected.value.detail) == (code, detail)


def test_validate_extraction_empty_findings_remain_valid():
    dropped = []
    assert validate_extraction({'findings': []}, '', dropped=dropped) == []
    assert dropped == []


def test_gap_planner_validates_ids_and_returns_search_jobs(world):
    c = world
    def transport(payload,key):
        data = json.loads(json.loads(payload)['messages'][1]['content']); g=data[0]
        assert len(data)<=20 and 'failed_queries' in g
        return response({'queries':[{'gap_id':g['gap_id'],'target_id':g['target_id'],'query':'달빛정원 공식 소개', 'reason':'missing biography'}]})
    a = Adapters(Client(Budget(meter(c)), key='dummy', transport=transport, balance_transport=healthy_balance))
    jobs = a.plan(c)
    assert len(jobs)==1 and jobs[0]['path']=='reverse_gap'
    job = jobs[0]; job['url']=discover.search_url(discover.load_seeds(),job['query'])
    assert discover.enqueue(c, **job)
    forged = adapter(c, {'queries':[{'gap_id':'unknown','target_id':'unknown','query':'https://bad.test', 'reason':'escape'}]})
    assert forged.plan(c)==[] and forged.client.stopped is None and forged.client.status=='enabled'


@pytest.mark.parametrize('low_balance', [False, True])
def test_qwen_extract_continues_after_both_plan_providers_fail(world, low_balance):
    from test_deepseek_balance import wallet
    text = '달빛정원의 운영에 대한 중요한 소식입니다.'
    dcalls, qcalls = [], []
    def primary(payload, key):
        dcalls.append(json.loads(payload))
        raise DeepSeekError('transport_unknown')
    def qwen(payload, key):
        body = json.loads(payload)
        qcalls.append(body)
        assert body['model'] == QWEN_MODEL and body['enable_thinking'] is False
        assert 'thinking' not in body and body['temperature'] == 0
        if len(qcalls) == 1:
            raise DeepSeekError('transport_unknown')
        return response({'findings': [finding(text)]})
    client = Client(Budget(meter(world)), key='dummy', transport=primary,
                    balance_transport=(lambda key: wallet(available=False)) if low_balance else healthy_balance,
                    fallback_key='fixture-backup', fallback_transport=qwen)
    a = Adapters(client)
    assert a.plan(world) == []
    assert client.stopped == 'transport_unknown' and client.status == 'enabled'
    result = process_doc(world, doc(text), Gazetteer(world), tier='official', model_adapter=a)
    assert result['new'] == 1 and a.stats['called'] >= 1 and a.stats['findings'] == 1
    assert len(dcalls) == (0 if low_balance else 1) and len(qcalls) == 2
    rows = list(world.execute('SELECT task,model,state FROM ai_request ORDER BY rowid'))
    assert [(r['task'], r['model'], r['state']) for r in rows] == (
        ([] if low_balance else [('gap-plan-2', 'deepseek-chat', 'unknown')]) +
        [('gap-plan-2', QWEN_MODEL, 'unknown'), ('web-extract-2', QWEN_MODEL, 'done')])
    row = world.execute("SELECT pending_only,model_meta_json FROM extraction WHERE extractor='deepseek:web'").fetchone()
    assert row['pending_only'] == 1 and json.loads(row['model_meta_json'])['provider'] == 'qwen'


def test_cycle_balance_epilogue_keeps_qwen_extract_and_recheck_enabled(world, tmp_path, monkeypatch):
    from test_deepseek_balance import wallet
    from kg_collect import recheck
    text = '달빛정원의 운영에 대한 중요한 소식입니다.'
    tasks, admitted = [], []
    def qwen(payload, key):
        body = json.loads(payload)
        tasks.append(body)
        if len(tasks) == 1:
            data = json.loads(body['messages'][1]['content'])
            g = data[0]
            return response({'queries': [{'gap_id': g['gap_id'], 'target_id': g['target_id'],
                                         'query': '달빛정원 공식 소개', 'reason': 'missing biography'}]})
        return response({'findings': [finding(text)]})
    client = Client(Budget(meter(world)), key='dummy',
                    transport=lambda *a: pytest.fail('low-balance primary completion'),
                    balance_transport=lambda key: wallet(available=False),
                    fallback_key='fixture-backup', fallback_transport=qwen)
    a = Adapters(client)
    bot = cycle.Bot(world, data_dir=str(tmp_path / 'data'), deepseek=True)
    bot.ai = a
    monkeypatch.setattr(recheck, 'admit', lambda *args, **kwargs: admitted.append(1) or 1)
    result = cycle.stage_plan(SimpleNamespace(env={'bot': bot}, cost=client.budget.meter))
    assert result.output['new_jobs']['deepseek_gap'] == 1
    assert result.output['new_jobs']['monthly_rechecks'] == 1 and admitted == [1]
    assert client.balance.check(client.key) == 'balance_low'
    assert client.primary_paused == 'balance_low' and client.stopped is None and client.status == 'enabled'
    assert process_doc(world, doc(text), Gazetteer(world), tier='official', model_adapter=a)['new'] == 1
    assert a.stats['called'] == 1 and a.stats['findings'] == 1 and len(tasks) == 2
    assert {r[0] for r in world.execute('SELECT model FROM ai_request')} == {QWEN_MODEL}


@pytest.mark.parametrize('lost_extract', [False, True])
def test_monthly_extract_uses_qwen_after_balance_pause(world, lost_extract):
    from test_deepseek_balance import wallet
    from kg_collect import recheck
    text = '달빛정원의 운영에 대한 중요한 소식입니다.'
    calls = []
    def qwen(*a):
        calls.append(1)
        if lost_extract and len(calls) == 1:
            raise DeepSeekError('transport_unknown')
        return response({'findings': [finding(text)]})
    client = Client(Budget(meter(world)), key='dummy',
                    transport=lambda *a: pytest.fail('low-balance recheck primary completion'),
                    balance_transport=lambda key: wallet(available=False),
                    fallback_key='fixture-backup',
                    fallback_transport=qwen)
    recheck.ensure_schedule(world, SEED)
    world.execute("UPDATE restaurant_recheck SET next_due_at=CASE restaurant_id WHEN 'r_dal' THEN '2026-01-01T00:00:00Z' ELSE '2099-01-01T00:00:00Z' END")
    world.commit()
    assert recheck.admit(world, discover.load_seeds()) == 1
    aid = world.execute('SELECT attempt_id FROM restaurant_recheck_attempt').fetchone()[0]
    a = Adapters(client)
    rows = a.extract(doc(text), [], Gazetteer(world), workload='recheck', attempt_id=aid)
    if lost_extract:
        assert rows == [] and a.defer_extract and client.status == 'enabled'
        # A failed inspection stays deferred, while a different document can still use Qwen.
        rows = a.extract(doc(text, '2'), [], Gazetteer(world))
        assert len(rows) == 1 and len(calls) == 2 and not a.defer_extract
        row = world.execute("SELECT model,state FROM ai_request ORDER BY rowid LIMIT 1").fetchone()
        assert tuple(row) == (QWEN_MODEL, 'unknown')
        return
    assert len(rows) == len(calls) == 1 and a.stats['called'] == 1 and a.stats['skipped_balance'] == 0
    assert client.primary_paused == 'balance_low' and client.stopped is None and client.status == 'enabled'
    row = world.execute('SELECT model,workload,state FROM ai_request').fetchone()
    assert tuple(row) == (QWEN_MODEL, 'recheck', 'done')


def _recheck_attempt(conn, budget):
    from kg_collect import recheck
    recheck.ensure_schedule(conn, SEED)
    conn.execute("UPDATE restaurant_recheck SET next_due_at=CASE restaurant_id WHEN 'r_dal' THEN '2026-01-01T00:00:00Z' ELSE '2099-01-01T00:00:00Z' END")
    conn.commit()
    assert recheck.admit(conn, discover.load_seeds()) == 1
    return conn.execute('SELECT attempt_id FROM restaurant_recheck_attempt').fetchone()[0]


def test_recheck_finding_contract_rejects_three_results(world):
    text = '달빛정원의 운영 소식입니다.'
    calls = []
    a = Adapters(Client(Budget(meter(world)), key='dummy',
                        transport=lambda *args: calls.append(1) or response({'findings': [finding(text) for _ in range(3)]}),
                        balance_transport=healthy_balance))
    aid = _recheck_attempt(world, a.client.budget)
    assert a.extract(doc(text), [], Gazetteer(world), workload='recheck', attempt_id=aid) == []
    # Three valid rows still breach the recheck ceiling: one billed retry, then the document fails.
    assert len(calls) == 2 and a.stats['called'] == 1 and a.stats['failed'] == 1
    assert a.stats['findings'] == 0 and a.stats['findings_dropped'] == 0
    assert a.client.stopped is None and a.client.status == 'enabled' and not a.defer_extract
    rows = world.execute("SELECT state,error,result_json FROM ai_request WHERE task='web-extract-2'").fetchall()
    assert len(rows) == 2 and all(r['state'] == 'failed' and r['error'] == 'too_many_findings' and r['result_json'] is None for r in rows)
    assert world.execute('SELECT COUNT(*) FROM extraction').fetchone()[0] == 0


@pytest.mark.parametrize('valid,invalid', [(2, 1), (2, 2), (1, 2)])
def test_recheck_partial_drops_keep_valid_rows(world, valid, invalid):
    text = '달빛정원의 운영 소식입니다.'
    good = [finding(text), finding('운영 소식', label='menu')][:valid]
    bad = [finding(text, quote='not on the page'), finding(text, date_text='2026-11-05')][:invalid]
    tokens = ['quote_not_exact,quote_len=15,in_text=0', 'date_not_verbatim'][:invalid]
    expected_dropped = ' '.join('%d:%s' % (valid + i, token) for i, token in enumerate(tokens))
    calls = []
    a = Adapters(Client(Budget(meter(world)), key='dummy',
                        transport=lambda *args: calls.append(1) or response({'findings': good + bad}),
                        balance_transport=healthy_balance))
    aid = _recheck_attempt(world, a.client.budget)
    rows = a.extract(doc(text), [], Gazetteer(world), workload='recheck', attempt_id=aid)
    # Dropping the invalid findings brings the >2-finding response under the ceiling: no retry.
    assert len(calls) == 1 and len(rows) == valid and a.stats['failed'] == 0
    assert [r['quote'] for r in rows] == [r['quote'] for r in good]
    assert all(r['pending_only'] for r in rows)
    assert a.stats['findings'] == valid and a.stats['findings_dropped'] == invalid
    assert a.client.stopped is None and a.client.status == 'enabled' and not a.defer_extract
    for r in rows:
        meta = json.loads(r['model_meta_json'])
        assert meta['workload'] == 'recheck' and meta['dropped'] == expected_dropped
        assert 'not on the page' not in meta['dropped'] and '2026-11-05' not in meta['dropped'] and text not in meta['dropped']
    request = world.execute("SELECT state,error,result_json FROM ai_request WHERE task='web-extract-2'").fetchone()
    assert request['state'] == 'done' and request['error'] is None
    assert json.loads(request['result_json'])['dropped'] == expected_dropped.split(' ')


def test_zero_rule_hard_extraction_unknown_date_and_review_only(world):
    c = world; text='달빛정원의 운영에 대한 중요한 소식입니다.'
    a = adapter(c, {'findings':[finding(text)]})
    before = {t:c.execute('SELECT COUNT(*) FROM '+t).fetchone()[0] for t in ('claim','employment','restaurant_status_event','field_history','service_output')}
    r = process_doc(c, doc(text), Gazetteer(c), tier='official', model_adapter=a)
    assert r['new']==1
    row = c.execute("SELECT * FROM extraction WHERE extractor='deepseek:web'").fetchone()
    assert row['pending_only']==1 and row['asserted_by']=='ai_inference' and row['epistemic']=='inference' and row['date_text'] is None
    assert c.execute("SELECT COUNT(*) FROM review_queue WHERE reason='ai_inference'").fetchone()[0]==1
    process_doc(c, doc(text), Gazetteer(c), tier='official', model_adapter=a)
    assert c.execute('SELECT COUNT(*) FROM extraction').fetchone()[0]==1
    assert c.execute("SELECT COUNT(*) FROM review_queue WHERE reason='ai_inference'").fetchone()[0]==1
    # Even deliberately forged fact fields must not fill a gap or materialize a source assertion.
    c.execute("UPDATE extraction SET epistemic='fact'"); c.commit()
    gaps.resolve_gaps(c); apply.apply_status(c); apply.apply_affiliation(c)
    assert {t:c.execute('SELECT COUNT(*) FROM '+t).fetchone()[0] for t in before}==before
    assert c.execute("SELECT COUNT(*) FROM gap WHERE resolved_by_extraction=?", (row['extraction_id'],)).fetchone()[0]==0


@pytest.mark.parametrize('extra,code', [
    ({'quote': 'fabricated'}, 'quote_not_exact'),
    ({'date_text': '2026-11-05'}, 'date_not_verbatim'),
    ({'label': 'shell_command'}, 'label_invalid'),
    ({'org_text': 'invented employer'}, 'name_absent'),
])
def test_invalid_model_fields_quotes_and_dates_are_rejected(world, extra, code):
    text = '달빛정원의 운영 소식입니다.'
    calls = []
    def transport(payload, key):
        calls.append(json.loads(payload))
        return response({'findings': [finding(text, **extra)]})
    a = Adapters(Client(Budget(meter(world)), key='dummy', transport=transport, balance_transport=healthy_balance))
    assert a.extract(doc(text), [], Gazetteer(world)) == []
    assert len(calls) == 2 and a.client.stopped is None and a.client.status == 'enabled'
    assert not a.defer_extract and a.stats['failed'] == 1 and a.stats['findings'] == 0
    assert code in calls[1]['messages'][-1]['content']
    assert 'Previous output was rejected' not in calls[0]['messages'][0]['content']
    rows = world.execute("SELECT error, result_json FROM ai_request WHERE state='failed'").fetchall()
    assert len(rows) == 2 and all(r['result_json'] is None and r['error'].startswith(code) for r in rows)
    blob = ' '.join(r['error'] for r in rows)
    assert text not in blob and 'fabricated' not in blob and '2026-11-05' not in blob and 'invented' not in blob
    assert world.execute('SELECT COUNT(*) FROM extraction').fetchone()[0] == 0


def test_quote_rejection_fails_one_document_and_next_document_is_extracted(world):
    text = '달빛정원의 운영 소식입니다.'
    calls, qcalls = [], []
    def transport(payload, key):
        calls.append(json.loads(payload))
        quote = 'fabricated' if len(calls) <= 2 else text
        return response({'findings': [finding(text, quote=quote)]})
    client = Client(Budget(meter(world)), key='dummy', transport=transport,
                    balance_transport=healthy_balance, fallback_key='fixture-backup',
                    fallback_transport=lambda *a: qcalls.append(1) or response())
    a, gaz = Adapters(client), Gazetteer(world)
    assert process_doc(world, doc(text), gaz, tier='official', model_adapter=a)['new'] == 0
    # Failed, not deferred: the billed rejection is stored, so a deferred document
    # would return every run only to hit task_previously_rejected and defer forever.
    assert len(calls) == 2 and not a.defer_extract and a.stats['failed'] == 1
    assert client.stopped is None and client.status == 'enabled'
    billed = client.budget.meter.used
    assert billed == pytest.approx(.12)
    # The same Client also refuses to replay the two already billed attempts.
    assert process_doc(world, doc(text), gaz, tier='official', model_adapter=a)['new'] == 0
    assert len(calls) == 2 and client.budget.meter.used == billed and not a.defer_extract
    assert process_doc(world, doc(text, '2'), gaz, tier='official', model_adapter=a)['new'] == 1
    assert len(calls) == 3 and qcalls == [] and not a.defer_extract
    assert a.stats['findings'] == 1 and client.status == 'enabled'
    rows = world.execute('SELECT state,error,result_json FROM ai_request ORDER BY rowid').fetchall()
    assert [r['state'] for r in rows] == ['failed', 'failed', 'done']
    assert all(r['error'].startswith('quote_not_exact') and r['result_json'] is None for r in rows[:2])


@pytest.mark.parametrize('provider', ['deepseek', 'qwen'])
def test_invalid_json_skips_one_document_and_extracts_next(world, provider):
    from test_deepseek_balance import wallet
    text = '달빛정원의 운영 소식입니다.'
    calls = []
    def transport(payload, key):
        calls.append(json.loads(payload))
        if len(calls) <= 2:
            return 200, json.dumps({'usage': {'prompt_tokens': 7000, 'completion_tokens': 633, 'total_tokens': 7633},
                                    'choices': [{'finish_reason': 'stop', 'message': {'content': 'bad json'}}]}).encode()
        return response({'findings': [finding(text)]})
    client = Client(Budget(meter(world)), key='dummy',
                    transport=transport if provider == 'deepseek' else lambda *a: pytest.fail('primary called'),
                    balance_transport=healthy_balance if provider == 'deepseek' else lambda key: wallet(available=False),
                    fallback_key='fixture-backup',
                    fallback_transport=transport if provider == 'qwen' else lambda *a: pytest.fail('backup called'))
    a, gaz = Adapters(client), Gazetteer(world)
    assert process_doc(world, doc(text), Gazetteer(world), tier='official', model_adapter=a)['new'] == 0
    assert len(calls) == 2 and client.stopped is None and client._schema_rejections == 1
    other = doc(text, '2')
    assert process_doc(world, other, Gazetteer(world), tier='official', model_adapter=a)['new'] == 1
    assert len(calls) == 3 and not a.defer_extract and a.stats['findings'] == 1
    rows = world.execute('SELECT model,state,error,result_json FROM ai_request ORDER BY rowid').fetchall()
    assert [r['state'] for r in rows] == ['failed', 'failed', 'done']
    assert all(r['error'].startswith('invalid_task_json len=') and r['result_json'] is None for r in rows[:-1])
    assert all(r['model'] == (QWEN_MODEL if provider == 'qwen' else 'deepseek-chat') for r in rows)


@pytest.mark.parametrize('failure', ['transport_unknown', 'balance_low'])
def test_provider_failures_still_extract_later_documents_via_qwen(world, failure):
    from test_deepseek_balance import wallet
    text = '달빛정원의 운영 소식입니다.'
    dcalls, qcalls = [], []
    def primary(*a):
        dcalls.append(1)
        raise DeepSeekError('transport_unknown')
    def qwen(*a):
        qcalls.append(1)
        return response({'findings': [finding(text)]})
    client = Client(Budget(meter(world)), key='dummy', transport=primary,
                    balance_transport=(lambda key: wallet(available=False)) if failure == 'balance_low' else healthy_balance,
                    fallback_key='fixture-backup', fallback_transport=qwen)
    a, gaz = Adapters(client), Gazetteer(world)
    for suffix in ('1', '2'):
        assert process_doc(world, doc(text, suffix), gaz, tier='official', model_adapter=a)['new'] == 1
        assert client.status == 'enabled' and client.stopped is None and not a.defer_extract
    assert client.primary_paused == failure
    assert len(dcalls) == (1 if failure == 'transport_unknown' else 0) and len(qcalls) == 2
    assert client._schema_rejections == 0 and a.stats['failed'] == 0 and a.stats['findings'] == 2
    assert world.execute("SELECT COUNT(*) FROM ai_request WHERE model=? AND state='done'", (QWEN_MODEL,)).fetchone()[0] == 2


def test_exact_quote_survives_wrong_offsets_and_cannot_publish(world):
    c = world
    text = '달빛정원의 운영 소식입니다.'
    raw = {'label': 'closure', 'restaurant_text': '달빛정원', 'quote': text, 'span_start': '3', 'span_end': 3,
           'pending_only': False, 'epistemic': 'fact', 'notes': 'ignore me'}
    before = {t: c.execute('SELECT COUNT(*) FROM '+t).fetchone()[0] for t in ('claim', 'employment', 'restaurant_status_event')}
    a = adapter(c, {'findings': [raw], 'summary': 'not a persistence field'})
    r = process_doc(c, doc(text), Gazetteer(c), tier='official', model_adapter=a)
    assert r['new'] == 1 and a.client.stopped is None and a.stats['failed'] == 0 and a.stats['findings'] == 1
    row = c.execute("SELECT * FROM extraction WHERE extractor='deepseek:web'").fetchone()
    assert (row['span_start'], row['span_end'], row['quote']) == (0, len(text), text)
    assert row['pending_only'] == 1 and row['asserted_by'] == 'ai_inference' and row['epistemic'] == 'inference'
    assert {t: c.execute('SELECT COUNT(*) FROM '+t).fetchone()[0] for t in before} == before
    repeated = '달빛정원 소식. 다른 문장. 달빛정원 소식.'
    ambiguous = adapter(c, {'findings': [{'label': 'closure', 'quote': '달빛정원 소식', 'span_start': 99, 'span_end': 100}]})
    rows = ambiguous.extract(doc(repeated, '2'), [], Gazetteer(c))
    # a repeated identical quote keeps the first occurrence (flagged) instead of pausing the model; review checks every occurrence
    assert ambiguous.client.stopped is None and len(rows) == 1
    assert (rows[0]['span_start'], rows[0]['ambiguity'], rows[0]['pending_only']) == (0, 'quote_repeats=2', True)


PAGE = '달빛정원 2021-05-01. 한도윤 총괄 셰프 기록.'
OPENING = '달빛정원 2021-05-01'
EMPLOYMENT = '한도윤 총괄 셰프'


def test_realistic_exact_quotes_stay_pending_and_wrong_unique_offsets_are_repaired(world):
    c = world
    before = {t: c.execute('SELECT COUNT(*) FROM ' + t).fetchone()[0] for t in ('claim', 'employment', 'restaurant_status_event')}
    payload = {'findings': [
        {'label': 'opening', 'restaurant_text': '달빛정원', 'date_text': '2021-05-01', 'quote': OPENING,
         'span_start': 1, 'span_end': 2, 'pending_only': False, 'epistemic': 'fact', 'notes': 'ignore'},
        {'label': 'employment_claim', 'chef_text': '한도윤', 'restaurant_text': '달빛정원', 'role_text': '총괄 셰프',
         'quote': EMPLOYMENT},
    ]}
    calls = []
    def transport(payload_bytes, key):
        calls.append(json.loads(payload_bytes))
        return response(payload)
    a = Adapters(Client(Budget(meter(c)), key='dummy', transport=transport, balance_transport=healthy_balance))
    result = process_doc(c, doc(PAGE), Gazetteer(c), tier='official', model_adapter=a)
    assert result['new'] >= 2 and a.client.stopped is None and a.stats['failed'] == 0 and len(calls) == 1
    sent = calls[0]['messages'][0]['content']
    assert 'at most 8' in sent and 'Do not convert' in sent and 'Do not paraphrase' in sent
    rows = c.execute("SELECT label,quote,span_start,span_end,date_text,pending_only,asserted_by,epistemic FROM extraction WHERE extractor='deepseek:web' ORDER BY span_start").fetchall()
    assert [(r['label'], r['quote'], r['span_start'], r['span_end'], r['date_text'], r['pending_only'], r['asserted_by'], r['epistemic']) for r in rows] == [
        ('opening', OPENING, 0, len(OPENING), '2021-05-01', 1, 'ai_inference', 'inference'),
        ('employment_claim', EMPLOYMENT, PAGE.index(EMPLOYMENT), PAGE.index(EMPLOYMENT) + len(EMPLOYMENT), None, 1, 'ai_inference', 'inference'),
    ]
    assert {t: c.execute('SELECT COUNT(*) FROM ' + t).fetchone()[0] for t in before} == before


def test_realistic_extraction_misses_are_categorized_without_storing_source_text(world):
    """A Korean page with a converted date and a near quote must not be repaired into evidence."""
    c = world
    text = '달빛정원은 2021년 5월 성수동에 문을 열었다. 한도윤 셰프는 달빛정원의 총괄 셰프로 있다.'
    near = '달빛정원은 2021년 5월에 오픈했다'
    payload = {'findings': [
        {'label': 'opening', 'restaurant_text': '달빛정원', 'date_text': '2021-05', 'quote': near},
        {'label': 'opening', 'restaurant_text': '달빛정원', 'date_text': '2021년 5월', 'quote': '달빛정원은 2021년 5월 성수동에 문을 열었다.'},
        {'label': 'employment_claim', 'chef_text': '없는사람', 'quote': '한도윤 셰프는 달빛정원의 총괄 셰프로 있다.'},
        {'label': 'mentorship_claim', 'quote': '한도윤 셰프는 달빛정원의 총괄 셰프로 있다.'},
    ]}
    a = adapter(c, payload)
    assert a.extract(doc(text), [], Gazetteer(c)) == []
    assert a.client.stopped is None and a.stats['findings'] == 0
    error = c.execute("SELECT error, result_json FROM ai_request WHERE task='web-extract-2' AND state='failed' ORDER BY rowid").fetchone()
    assert error['result_json'] is None
    assert error['error'].startswith('quote_not_exact ')
    for token in ('0:quote_not_exact', '1:date_not_iso', '2:name_absent', '3:mentorship_unsupported'):
        assert token in error['error']
    for secret in (near, '2021-05', '2021년', '없는사람', '한도윤', text):
        assert secret not in error['error']
    assert c.execute('SELECT COUNT(*) FROM extraction').fetchone()[0] == 0


def test_near_quote_and_too_many_findings_store_no_evidence(world):
    text = '달빛정원의 운영 소식입니다.'
    near = adapter(world, {'findings': [finding(text, quote='달빛정원의  운영 소식입니다.')]})
    assert near.extract(doc(text), [], Gazetteer(world)) == []
    assert near.client.stopped is None
    many = adapter(world, {'findings': [finding(text) for _ in range(9)]})
    assert many.extract(doc(text, '2'), [], Gazetteer(world)) == []
    assert many.client.stopped is None
    errors = [r['error'] for r in world.execute("SELECT error FROM ai_request WHERE state='failed'").fetchall()]
    assert any(e.startswith('too_many_findings n=9') for e in errors)
    assert all(text not in e for e in errors)
    assert world.execute('SELECT COUNT(*) FROM extraction').fetchone()[0] == 0


def test_bad_findings_are_dropped_and_valid_finding_is_stored_pending(world):
    text = '달빛정원의 운영 소식입니다.'
    payload = {'findings': [finding(text), finding(text, quote='not on the page'),
                            finding(text, date_text='2026-11-05')]}
    calls = []
    a = Adapters(Client(Budget(meter(world)), key='dummy',
                        transport=lambda *args: calls.append(1) or response(payload),
                        balance_transport=healthy_balance))
    before = world.execute('SELECT COUNT(*) FROM claim').fetchone()[0]
    assert process_doc(world, doc(text), Gazetteer(world), tier='official', model_adapter=a)['new'] == 1
    assert len(calls) == 1 and a.stats['called'] == 1 and a.stats['failed'] == 0
    assert a.stats['findings'] == 1 and a.stats['findings_dropped'] == 2
    assert a.client._schema_rejections == 0 and a.client.stopped is None and not a.defer_extract
    row = world.execute("SELECT * FROM extraction WHERE extractor='deepseek:web'").fetchone()
    assert row['quote'] == text and row['pending_only'] == 1 and row['asserted_by'] == 'ai_inference'
    detail = json.loads(row['model_meta_json'])['dropped']
    assert detail == '1:quote_not_exact,quote_len=15,in_text=0 2:date_not_verbatim'
    assert 'not on the page' not in detail and '2026-11-05' not in detail and text not in detail
    assert world.execute('SELECT COUNT(*) FROM claim').fetchone()[0] == before
    requests = world.execute('SELECT state,error,result_json FROM ai_request').fetchall()
    assert len(requests) == 1 and requests[0]['state'] == 'done' and requests[0]['error'] is None
    assert len(json.loads(requests[0]['result_json'])['dropped']) == 2


@pytest.mark.parametrize('cache', ['adapter', 'client', 'legacy'])
def test_cached_extraction_preserves_drop_reporting_without_another_paid_call(world, cache):
    text = '달빛정원의 운영 소식입니다.'
    a = adapter(world, {'findings': [finding(text), finding(text, quote='not on the page')]})
    rows = a.extract(doc(text), [], Gazetteer(world))
    assert len(rows) == 1 and a.stats['findings_dropped'] == 1
    if cache == 'legacy':
        request = world.execute('SELECT request_key,result_json FROM ai_request').fetchone()
        legacy_rows = json.loads(request['result_json'])['findings']
        world.execute('UPDATE ai_request SET result_json=? WHERE request_key=?',
                      (json.dumps(legacy_rows), request['request_key']))
        world.commit()
    if cache != 'adapter':
        a = Adapters(Client(Budget(meter(world)), key='dummy',
                            transport=lambda *args: pytest.fail('paid request for a cached extraction'),
                            balance_transport=healthy_balance))
    else:
        a.client.call = lambda *args, **kwargs: pytest.fail('adapter cache missed')
    billed = world.execute('SELECT SUM(units) FROM ai_request').fetchone()[0]
    cached_rows = a.extract(doc(text), [], Gazetteer(world))
    assert len(cached_rows) == 1 and cached_rows[0]['quote'] == text
    assert a.stats['findings_dropped'] == {'adapter': 2, 'client': 1, 'legacy': 0}[cache]
    detail = json.loads(cached_rows[0]['model_meta_json'])['dropped']
    assert detail == ('' if cache == 'legacy' else '1:quote_not_exact,quote_len=15,in_text=0')
    assert world.execute('SELECT COUNT(*) FROM ai_request').fetchone()[0] == 1
    assert world.execute('SELECT SUM(units) FROM ai_request').fetchone()[0] == billed


def test_billed_blank_content_skips_document_and_publishes_nothing(world):
    c = world
    text = '달빛정원의 운영 소식입니다.'
    calls = []
    usage = {'prompt_tokens': 8323, 'completion_tokens': 635, 'total_tokens': 8958, 'prompt_cache_hit_tokens': 0}
    def transport(payload, key):
        calls.append(json.loads(payload))
        body = {'usage': usage, 'choices': [{'finish_reason': 'stop', 'message': {
            'content': ' ', 'reasoning_content': 'REASONING-NOT-TASK ' + json.dumps({'findings': [finding(text)]})}}]}
        return 200, json.dumps(body).encode()
    a = Adapters(Client(Budget(meter(c)), key='dummy', transport=transport, balance_transport=healthy_balance))
    before = c.execute('SELECT COUNT(*) FROM claim').fetchone()[0]
    assert process_doc(c, doc(text), Gazetteer(c), tier='official', model_adapter=a)['new'] == 0
    assert len(calls) == 2 and a.client.stopped is None and a.client.status == 'enabled'
    assert not a.defer_extract
    assert a.stats['called'] == 1 and a.stats['failed'] == 1 and a.stats['findings'] == 0
    assert c.execute('SELECT COUNT(*) FROM extraction').fetchone()[0] == 0
    assert c.execute('SELECT COUNT(*) FROM claim').fetchone()[0] == before
    assert all('REASONING-NOT-TASK' not in json.dumps(call) for call in calls)


def test_bridge_preserves_model_version_and_pending_provenance(world,tmp_path):
    c=world; text='달빛정원의 운영 소식입니다.'
    # Fixture fetch record supplies the FK so the real bridge reads the model row.

    d=doc(text); d.fetch_id='f'
    process_doc(c,d,Gazetteer(c),tier='official',model_adapter=adapter(c,{'findings':[finding(text)]}))
    path=c.execute('PRAGMA database_list').fetchone()[2]
    records=collect_bridge.collector_records(path,{'press':'press_articles'})
    assert records
    body=next(iter(records.values()))[0]['body']
    candidates=collect_bridge.candidates(body,'2026-11-05')
    assert candidates and all(p['pending_only'] and p['asserted_by']=='ai_inference' for _,p,_,_ in candidates)
    root=tmp_path/'repo'; (root/'crawler').mkdir(parents=True)
    vault=root/'crawler/vault.json'; vault.write_text('{"records":[]}')
    rep=Daemon(c,stages.HANDLERS,env={'root':str(root),'vault_path':str(vault),'collect_db':path,'raw_dir':str(tmp_path/'raw'),
                                    'ai_extractor':lambda *a: pytest.fail('collector model output sent to model twice')}).run(resume=False)
    assert rep['status']=='succeeded'
    rows=c.execute("SELECT * FROM candidate_fact WHERE asserted_by='ai_inference'").fetchall()
    assert rows and all(r['resolution']=='needs_review' and r['applied_claim_id'] is None for r in rows)


def test_kg_and_model_findings_cannot_corroborate_seed_changes(tmp_path, monkeypatch):
    sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'crawler'))
    import importlib.util
    spec=importlib.util.spec_from_file_location('seed_apply',Path(__file__).resolve().parents[1]/'crawler/apply.py')
    module=importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    import subprocess
    subprocess.run(['git','init','-q',str(tmp_path)], check=True)
    monkeypatch.setattr(module,'ROOT',str(tmp_path))
    crawler=tmp_path/'crawler'; (crawler/'out').mkdir(parents=True)
    seed=tmp_path/'seed.json'; seed.write_text(json.dumps({'restaurants':[{'id':'r','name':'Fixture'}]}))
    original=seed.read_bytes()
    base={'id':'r','kind':'closure','url':'https://press.test/closure','host':'press.test','date':'2026-11-01','title':'closure'}
    kg={**base,'url':'https://www.instagram.com/p/x/','host':'instagram.com','instagram':True,'collector_account':'kitchen.genome','pending_only':True}
    ai={**base,'url':'https://official.test/closure','host':'official.test','extractor':'deepseek:web','asserted_by':'source','epistemic':'fact'}
    (crawler/'out/findings-20261105.json').write_text(json.dumps({'findings':[base,ai]}))
    (crawler/'out/ig-20261105.json').write_text(json.dumps({'findings':[kg]}))
    monkeypatch.setattr(module,'H',str(crawler)); monkeypatch.setattr(module,'SEED',str(seed))
    module.main()
    assert seed.read_bytes()==original
    summary=json.loads((crawler/'out/last_summary.json').read_text())
    assert summary['applied']==[] and summary['pending_count']==1
    published = (crawler/'pending.json').read_text() + (crawler/'applied.json').read_text()
    assert 'deepseek' not in published and 'kitchen.genome' not in published and 'ai_inference' not in published


def test_two_reliable_sources_publish_while_model_rows_stay_out(tmp_path, monkeypatch):
    import importlib.util
    spec = importlib.util.spec_from_file_location('seed_apply_pub', Path(__file__).resolve().parents[1]/'crawler/apply.py')
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    import subprocess
    subprocess.run(['git','init','-q',str(tmp_path)], check=True)
    monkeypatch.setattr(module,'ROOT',str(tmp_path))
    crawler = tmp_path/'crawler'; (crawler/'out').mkdir(parents=True)
    seed = tmp_path/'seed.json'
    seed.write_text(json.dumps({'restaurants':[{'id':'r','name':'Fixture'}]}))
    base = {'id':'r','kind':'closure','url':'https://press.test/closure','host':'press.test','date':'2026-11-01','title':'closure'}
    other = {**base, 'url':'https://news.test/closure', 'host':'news.test'}
    ai = {**base, 'url':'https://official.test/closure', 'host':'official.test', 'extractor':'deepseek:web', 'asserted_by':'source', 'epistemic':'fact'}
    kg = {**base, 'url':'https://www.instagram.com/p/x/', 'host':'instagram.com', 'instagram':True, 'collector_account':'kitchen.genome', 'pending_only':True}
    (crawler/'out/findings-20261105.json').write_text(json.dumps({'findings':[base, other, ai]}))
    (crawler/'out/ig-20261105.json').write_text(json.dumps({'findings':[kg]}))
    monkeypatch.setattr(module, 'H', str(crawler)); monkeypatch.setattr(module, 'SEED', str(seed))
    module.main()
    saved = json.loads(seed.read_text())
    assert saved['restaurants'][0]['closed_at'] == '2026-11-01'
    applied = json.loads((crawler/'applied.json').read_text())
    assert [a['sources'] for a in applied] == [['https://news.test/closure', 'https://press.test/closure']]
    published = (crawler/'pending.json').read_text() + (crawler/'applied.json').read_text() + seed.read_text()
    assert 'deepseek' not in published and 'kitchen.genome' not in published and 'official.test' not in published


def test_whitespace_pii_document_is_sent_and_offsets_use_filtered_text(world):
    """Real pages have newlines/tabs; sanitizing must not be mistaken for a failed redaction skip."""
    c = world
    raw = '달빛정원의\n운영에\t대한  중요한 소식입니다. 문의 me@example.test'
    clean = '달빛정원의 운영에 대한 중요한 소식입니다. 문의 [REDACTED_EMAIL]'
    quote = '달빛정원의 운영에 대한 중요한 소식입니다.'
    seen = []

    def transport(payload, key):
        data = json.loads(json.loads(payload)['messages'][1]['content'])
        seen.append(data)
        assert data['text'] == clean and 'me@example.test' not in data['text']
        return response({'findings': [finding(quote)]})

    a = Adapters(Client(Budget(meter(c)), key='dummy', transport=transport, balance_transport=healthy_balance))
    r = process_doc(c, doc(raw), Gazetteer(c), tier='official', model_adapter=a)
    assert r['new'] == 1 and seen
    assert a.stats['considered'] == 1 and a.stats['skipped_filter'] == 0
    assert a.stats['called'] == 1 and a.stats['findings'] == 1
    row = c.execute("SELECT quote,span_start,span_end FROM extraction WHERE extractor='deepseek:web'").fetchone()
    assert (row['quote'], row['span_start'], row['span_end']) == (quote, 0, len(quote))


SMOKE_PLAN_USAGE = {'prompt_tokens': 2399, 'completion_tokens': 1966, 'total_tokens': 4365, 'prompt_cache_hit_tokens': 0}
LEAD = '달빛정원은 중요한 곳이다. '


def _gap(conn):
    tid = conn.execute("SELECT target_id FROM target WHERE name_ko='달빛정원'").fetchone()[0]
    conn.execute("INSERT OR IGNORE INTO gap(gap_id,target_id,field,state,opened_at) VALUES ('g-budget',?,'chef','open',?)", (tid, db.utcnow()))
    conn.commit()


def test_queue_enumeration_does_not_spend_candidate_budget(world):
    c = world
    assert discover.enqueue(c, path='feed', pool='discovery', url='https://never-fetched.test/a', relevance=.9, caps={})
    assert c.execute('SELECT COUNT(*) FROM web_daily_url').fetchone()[0] == 0
    record_dispatch(c, 'https://actually-fetched.test/a', day='2026-11-05')
    assert c.execute('SELECT COUNT(*) FROM web_daily_url').fetchone()[0] == 1


def test_planner_history_lists_only_non_yielding_queries(world):
    c = world
    gap = gaps.gap_jobs(c)[0]
    c.execute('UPDATE gap SET attempts=-1 WHERE gap_id=?', (gap['gap_id'],))
    now = db.utcnow()
    c.execute("INSERT INTO crawl_job(job_id,path,pool,query,gap_id,state,last_error,created_at) VALUES ('jg','reverse_gap','backfill','good query',?,'done',NULL,?)", (gap['gap_id'], now))
    c.execute("INSERT INTO crawl_job(job_id,path,pool,query,gap_id,state,last_error,created_at) VALUES ('jb','reverse_gap','backfill','bad query',?,'failed','no_result',?)", (gap['gap_id'], now))
    c.commit()
    seen = {}

    def transport(payload, key):
        for row in json.loads(json.loads(payload)['messages'][1]['content']):
            if row['gap_id'] == gap['gap_id']:
                seen.update(row)
        return response({'queries': []})

    assert Adapters(Client(Budget(meter(c)), key='dummy', transport=transport, balance_transport=healthy_balance)).plan(c) == []
    assert seen['failed_queries'] == ['bad query']
