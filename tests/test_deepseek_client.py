from tests_balance_fake import healthy_balance
"""All provider HTTP is injected. No credentials, internet or paid requests."""
import concurrent.futures
import json
import os
import sys
from pathlib import Path
import pytest
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'daemon'))
from kg_ai.budget import Budget
from kg_ai.deepseek import (Client, DeepSeekError, MAX_BODY, NoRedirect, RETRYABLE,
                            SCHEMA_REJECT_LIMIT, TaskReject, URL)
from kg_daemon.db import connect, migrate, new_id, utcnow
from kg_daemon.runner import CostCapExceeded, CostMeter


def meter(conn, cap=100):
    run = new_id('run')
    conn.execute('INSERT INTO daemon_run(run_id,daemon,status,cost_cap_units,started_at) VALUES (?,?,?,?,?)', (run, 'test', 'running', cap, utcnow()))
    conn.commit()
    return CostMeter(conn, run, cap, {'llm_tokens_k': .5})


@pytest.fixture
def conn():
    c = connect(':memory:'); migrate(c)
    yield c
    c.close()


def response(data=None, usage=None, finish='stop'):
    return 200, json.dumps({'usage': usage or {'prompt_tokens': 100, 'completion_tokens': 20, 'total_tokens': 120, 'prompt_cache_hit_tokens': 40},
                            'choices': [{'finish_reason': finish, 'message': {'content': json.dumps(data if data is not None else {'ok': True})}}]}).encode()


def test_measured_usage_cache_and_zero_cap(conn):
    calls = []
    def transport(payload, key):
        calls.append((json.loads(payload), key)); return response()
    m = meter(conn)
    client = Client(Budget(m), key='synthetic-key', transport=transport, balance_transport=healthy_balance)
    assert client.call('test', 'JSON', {'source': 'text'}, lambda o: o) == {'ok': True}
    assert m.used == pytest.approx(.06)
    assert conn.execute('SELECT SUM(units) FROM cost_ledger').fetchone()[0] == pytest.approx(.06)
    r = conn.execute('SELECT * FROM ai_request').fetchone()
    assert (r['prompt_tokens'], r['completion_tokens'], r['cache_tokens']) == (100, 20, 40)
    client.call('test', 'JSON', {'source': 'text'}, lambda o: o)
    assert len(calls) == 1 and calls[0][0]['model'] == 'deepseek-chat'
    assert calls[0][0]['max_tokens'] == 2048 and calls[0][1] == 'synthetic-key'
    assert calls[0][0]['thinking'] == {'type': 'disabled'} and calls[0][0]['temperature'] == 0
    assert 'reasoning_effort' not in calls[0][0] and calls[0][0]['response_format'] == {'type': 'json_object'}
    c2 = Client(Budget(meter(conn, 0)), key='dummy', transport=lambda *a: pytest.fail('paid request at zero cap'), balance_transport=healthy_balance)
    with pytest.raises(CostCapExceeded): c2.call('zero', 'JSON', {}, lambda o: o)


@pytest.mark.parametrize('status,code', [(400, 'model_or_request_rejected'), (401, 'auth_failed'), (404, 'model_unavailable'), (429, 'rate_limited'), (503, 'provider_failed'), (302, 'provider_failed')])
def test_http_errors_are_sanitized_and_not_retried(conn, status, code):
    calls = []
    client = Client(Budget(meter(conn)), key='never-log-me', transport=lambda *a: (calls.append(1) or (status, b'never-log-me')), balance_transport=healthy_balance)
    for _ in range(2):
        with pytest.raises(DeepSeekError, match=code): client.call('task', 'JSON', {}, lambda o: o)
    assert len(calls) == 1
    assert 'never-log-me' not in str(dict(conn.execute('SELECT * FROM ai_request').fetchone()))
    assert conn.execute('SELECT state FROM ai_request').fetchone()[0] == 'unknown'


def test_lost_response_survives_new_run(conn):
    def lost(*a): raise TimeoutError('synthetic-password')
    client = Client(Budget(meter(conn)), key='dummy', transport=lost, balance_transport=healthy_balance)
    with pytest.raises(DeepSeekError, match='invalid_response_or_usage'): client.call('task', 'JSON', {}, lambda o: o)
    used = client.budget.meter.used
    assert used > 0
    next_client = Client(Budget(meter(conn)), key='dummy', transport=lambda *a: pytest.fail('unknown attempt replayed'), balance_transport=healthy_balance)
    with pytest.raises(DeepSeekError, match='previous_attempt'): next_client.call('task', 'JSON', {}, lambda o: o)
    assert conn.execute('SELECT SUM(units) FROM ai_request').fetchone()[0] == used


@pytest.mark.parametrize('raw', [b'not-json', b'x' * (MAX_BODY+1), response(usage={'prompt_tokens': True, 'completion_tokens': 1, 'total_tokens': 2})[1]])
def test_invalid_response_keeps_reservation(conn, raw):
    client = Client(Budget(meter(conn)), key='dummy', transport=lambda *a: (200, raw), balance_transport=healthy_balance)
    with pytest.raises(DeepSeekError): client.call('task', 'JSON', {}, lambda o: o)
    assert conn.execute('SELECT state,units FROM ai_request').fetchone()['state'] == 'unknown'
    assert client.budget.meter.used > 0


def test_invalid_schema_and_truncation_charge_actual_without_partial_cache(conn):
    calls = []
    client = Client(Budget(meter(conn)), key='dummy', transport=lambda *a: calls.append(1) or response(finish='length'), balance_transport=healthy_balance)
    with pytest.raises(DeepSeekError, match='truncated_json'): client.call('task', 'JSON', {}, lambda o: o)
    row = conn.execute('SELECT * FROM ai_request').fetchone()
    assert len(calls) == 1
    assert row['state'] == 'failed' and row['result_json'] is None and row['units'] == pytest.approx(.06)


def test_missing_key_and_redirects(conn):
    client = Client(Budget(meter(conn)), key='', transport=lambda *a: pytest.fail('missing key dispatched'), balance_transport=healthy_balance)
    with pytest.raises(DeepSeekError, match='missing_key'): client.call('task', 'JSON', {}, lambda o: o)
    assert client.status == 'disabled'
    assert NoRedirect().redirect_request(None, None, 302, '', {}, 'https://other.test') is None
    assert URL == 'https://api.deepseek.com/chat/completions'


def test_reservation_day_cap_is_atomic_across_connections(tmp_path):
    path = str(tmp_path / 'state.db'); c = connect(path); migrate(c)
    runs = [meter(c).run_id for _ in range(2)]; c.close()
    def reserve(run):
        conn = connect(path)
        try:
            b = Budget(CostMeter(conn, run, 100, {'llm_tokens_k': .5}), daily_cap=5, day=lambda: '2026-11-05')
            return b.reserve(run, 'task', 'deepseek-chat', 8000)
        except CostCapExceeded:
            return False
        finally:
            conn.close()
    with concurrent.futures.ThreadPoolExecutor(2) as pool:
        results = list(pool.map(reserve, runs))
    assert sorted(results) == [False, True]
    c = connect(path)
    assert c.execute('SELECT SUM(units) FROM ai_request').fetchone()[0] == 4


def test_usage_above_bound_is_recorded_in_full(conn):
    client = Client(Budget(meter(conn)), key='dummy', transport=lambda *a: response(usage={'prompt_tokens': 100000, 'completion_tokens': 0, 'total_tokens': 100000}), balance_transport=healthy_balance)
    with pytest.raises(CostCapExceeded): client.call('task', 'JSON', {}, lambda o: o)
    assert client.budget.meter.used == 50


def test_task_specific_output_limit_and_purged_cache_does_not_disable_run(conn):
    seen = []
    def transport(payload, key):
        seen.append(json.loads(payload)); return response({'ok': True})
    client = Client(Budget(meter(conn)), key='dummy', transport=transport, balance_transport=healthy_balance)
    assert client.call('planner', 'JSON', {'n': 1}, lambda o: o, max_tokens=4096) == {'ok': True}
    assert seen[0]['max_tokens'] == 4096
    # Retention purges cached IG results; a repeat must not stop the model for the whole run.
    conn.execute("UPDATE ai_request SET state='expired', result_json=NULL"); conn.commit()
    with pytest.raises(DeepSeekError, match='cached_result_purged'):
        client.call('planner', 'JSON', {'n': 1}, lambda o: o, max_tokens=4096)
    assert client.stopped is None and client.status == 'enabled'
    assert client.call('planner', 'JSON', {'n': 2}, lambda o: o) == {'ok': True}
    assert len(seen) == 2


def test_default_http_endpoint_auth_timeout_and_sanitized_exception(monkeypatch):
    import urllib.request
    from kg_ai.deepseek import http_transport
    seen = []
    class Response:
        status = 200
        def __enter__(self): return self
        def __exit__(self,*a): pass
        def read(self,n): assert n==MAX_BODY+1; return b'{}'
    class Opener:
        def open(self,req,timeout):
            seen.append(req)
            assert req.full_url==URL and req.get_header('Authorization')=='Bearer fixture-token'
            assert req.data==b'{"model":"fixture"}' and timeout==30
            return Response()
    monkeypatch.setattr(urllib.request,'build_opener',lambda *handlers:Opener())
    assert http_transport(b'{"model":"fixture"}','fixture-token')==(200,b'{}')
    assert len(seen)==1
    class Lost:
        def open(self,*a,**kw): raise RuntimeError('fixture-token')
    monkeypatch.setattr(urllib.request,'build_opener',lambda *handlers:Lost())
    with pytest.raises(DeepSeekError,match='^transport_unknown$'):
        http_transport(b'{}','fixture-token')


SMOKE_USAGE = {'prompt_tokens': 8323, 'completion_tokens': 635, 'total_tokens': 8958, 'prompt_cache_hit_tokens': 0}


def envelope(content, usage=None, finish='stop', reasoning='{"ok": true} do not treat this as the task'):
    return 200, json.dumps({'usage': usage or {'prompt_tokens': 10, 'completion_tokens': 2, 'total_tokens': 12, 'prompt_cache_hit_tokens': 0},
                            'choices': [{'finish_reason': finish, 'message': {'role': 'assistant', 'content': content, 'reasoning_content': reasoning}}]}).encode()


def test_json_mode_blank_retries_once_then_skips_without_using_reasoning(conn):
    """The observed smoke: HTTP 200, real usage, finish_reason stop, content replaced by spaces."""
    calls = []
    def transport(payload, key):
        body = json.loads(payload)
        calls.append(body)
        assert body['thinking'] == {'type': 'disabled'}
        assert 'do not treat this as the task' not in payload.decode()
        return envelope('   ', SMOKE_USAGE)
    client = Client(Budget(meter(conn)), key='dummy', transport=transport, balance_transport=healthy_balance)
    # Prompt-sized input keeps the 8,958-token smoke usage inside the byte reservation.
    with pytest.raises(DeepSeekError, match='^json_blank$'):
        client.call('web-extract-1', 'JSON', {'text': 'source ' * 800}, lambda o: o)
    assert len(calls) == 2 and client.stopped is None and client.status == 'enabled'
    assert 'json_blank' in calls[1]['messages'][0]['content']
    rows = conn.execute('SELECT state,error,result_json,prompt_tokens,completion_tokens FROM ai_request ORDER BY rowid').fetchall()
    assert len(rows) == 2
    assert all(r['state'] == 'failed' and r['error'] == 'json_blank' and r['result_json'] is None for r in rows)
    assert (rows[0]['prompt_tokens'], rows[0]['completion_tokens']) == (8323, 635)
    later = Client(Budget(meter(conn)), key='dummy', transport=lambda *a: pytest.fail('rejected task replayed'), balance_transport=healthy_balance)
    with pytest.raises(DeepSeekError, match='^task_previously_rejected$'):
        later.call('web-extract-1', 'JSON', {'text': 'source ' * 800}, lambda o: o)
    assert later.stopped is None and later.status == 'enabled'


def test_blank_then_valid_json_is_cached_and_reasoning_is_ignored(conn):
    calls = []
    def transport(payload, key):
        calls.append(json.loads(payload))
        if len(calls) == 1:
            return envelope(' \n\t ')
        return envelope('```json\n{"ok": true}\n```', reasoning='not the answer')
    client = Client(Budget(meter(conn)), key='dummy', transport=transport, balance_transport=healthy_balance)
    assert client.call('task', 'JSON', {'n': 1}, lambda o: o) == {'ok': True}
    assert client.stopped is None and len(calls) == 2
    assert 'not the answer' not in json.dumps(calls[1])
    again = Client(Budget(meter(conn)), key='dummy', transport=lambda *a: pytest.fail('validated retry replayed'), balance_transport=healthy_balance)
    assert again.call('task', 'JSON', {'n': 1}, lambda o: o) == {'ok': True}
    assert again.stopped is None


def test_schema_rejection_retries_once_and_double_encoded_json_is_accepted(conn):
    calls = []
    def transport(payload, key):
        calls.append(1)
        if len(calls) == 1:
            return response({'nope': True})
        return envelope(json.dumps(json.dumps({'ok': True})))
    def validate(obj):
        if obj.get('ok') is not True:
            raise ValueError('schema')
        return obj
    client = Client(Budget(meter(conn)), key='dummy', transport=transport, balance_transport=healthy_balance)
    assert client.call('task', 'JSON', {'n': 2}, validate) == {'ok': True}
    assert len(calls) == 2 and client.stopped is None
    bad = Client(Budget(meter(conn)), key='dummy', transport=lambda *a: response({'nope': True}), balance_transport=healthy_balance)
    with pytest.raises(DeepSeekError, match='^invalid_task_json$'):
        bad.call('task', 'JSON', {'n': 3}, validate)
    assert bad.stopped is None and bad.status == 'enabled'
    # One rejected sample from the recovered call, then both samples of the unrecovered call.
    assert conn.execute("SELECT COUNT(*) FROM ai_request WHERE state='failed' AND error='invalid_task_json'").fetchone()[0] == 3


def test_task_reject_keeps_a_sanitized_category_and_is_not_replayed(conn):
    secret = 'SECRET-QUOTE should not be stored'
    calls = []
    def validate(obj):
        raise TaskReject('quote_not_exact', '0:quote_not_exact,quote_len=21,in_text=0 ' + secret)
    client = Client(Budget(meter(conn)), key='dummy', transport=lambda *a: calls.append(1) or response({'findings': []}), balance_transport=healthy_balance)
    with pytest.raises(DeepSeekError, match='^quote_not_exact$'):
        client.call('web-extract-1', 'JSON', {'text': 'source'}, validate)
    assert client.stopped is None and client.status == 'enabled' and len(calls) == 2
    rows = conn.execute("SELECT error, result_json FROM ai_request WHERE state='failed'").fetchall()
    assert len(rows) == 2
    assert all(r['result_json'] is None and r['error'].startswith('quote_not_exact 0:quote_not_exact,quote_len=21,in_text=0') for r in rows)
    assert secret not in ' '.join(r['error'] for r in rows)
    later = Client(Budget(meter(conn)), key='dummy', transport=lambda *a: pytest.fail('categorized rejection replayed'), balance_transport=healthy_balance)
    with pytest.raises(DeepSeekError, match='^task_previously_rejected$'):
        later.call('web-extract-1', 'JSON', {'text': 'source'}, validate)
    assert later.stopped is None


@pytest.mark.parametrize('code', RETRYABLE)
def test_final_schema_rejections_pause_only_at_run_limit(conn, code):
    calls, qcalls = [], []
    def transport(payload, key):
        calls.append(1)
        if code == 'json_blank':
            return envelope(' ')
        if code == 'invalid_task_json':
            return envelope('not JSON')
        return response()
    def validate(obj):
        raise TaskReject(code)
    client = Client(Budget(meter(conn)), key='dummy', transport=transport,
                    balance_transport=healthy_balance, fallback_key='fixture-backup',
                    fallback_transport=lambda *a: qcalls.append(1) or response())
    assert SCHEMA_REJECT_LIMIT == 3
    for n in range(1, SCHEMA_REJECT_LIMIT + 1):
        with pytest.raises(DeepSeekError, match='^' + code + '$'):
            client.call('web-extract-1', 'JSON', {'document': n}, validate)
        assert len(calls) == 2 * n
        assert client._schema_rejections == n
        if n < SCHEMA_REJECT_LIMIT:
            assert client.stopped is None and client.status == 'enabled'
            # A billed rejection cannot be replayed or counted toward the brake again.
            with pytest.raises(DeepSeekError, match='^task_previously_rejected$'):
                client.call('web-extract-1', 'JSON', {'document': n}, validate)
            assert len(calls) == 2 * n and client._schema_rejections == n
    assert client.stopped == 'schema_reject_limit' and client.status == 'paused'
    with pytest.raises(DeepSeekError, match='^schema_reject_limit$'):
        client.call('web-extract-1', 'JSON', {'document': 'later'}, lambda o: o)
    assert len(calls) == 6 and qcalls == []
    rows = conn.execute('SELECT state,error,result_json FROM ai_request').fetchall()
    assert len(rows) == 6 and all(r['state'] == 'failed' and r['error'] == code and r['result_json'] is None for r in rows)
    assert client.budget.meter.used > 0
    # The brake belongs to this Client, not to the persisted requests or day.
    later = Client(client.budget, key='dummy', transport=lambda *a: response(), balance_transport=healthy_balance)
    assert later.call('web-extract-1', 'JSON', {'document': 'later'}, lambda o: o) == {'ok': True}
    assert later.stopped is None and later.status == 'enabled'
