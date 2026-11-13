"""Qwen is the first backup after the primary. All HTTP is injected; no live calls or secrets."""
import json
from pathlib import Path
import pytest
from kg_ai.budget import Budget
from kg_ai.deepseek import (Client, DeepSeekError, FALLBACK_CODES, QWEN_MODEL, QWEN_URL,
                            qwen_http_transport, MAX_BODY)
from kg_ai import review as rv
from test_ai_review import env, add, TEXT
from test_deepseek_client import conn, meter, response
from test_deepseek_balance import wallet
from tests_balance_fake import healthy_balance

ROOT = Path(__file__).resolve().parents[1]
SECRET = 'qwen-secret-never-log'


def fallback_client(conn, deepseek, qwen, **kw):
    return Client(Budget(meter(conn)), key='ds-key', transport=deepseek,
                  balance_transport=kw.get('balance_transport', healthy_balance),
                  fallback_key=SECRET, fallback_transport=qwen, fallback_model=kw.get('fallback_model', QWEN_MODEL))


def test_qwen_is_first_backup_in_every_dev_lane():
    lanes = json.loads((ROOT / 'config' / 'dev_lanes.json').read_text())
    for stage in ('design', 'design_review', 'build', 'build_review', 'review_fix', 're_review', 'audit'):
        assert lanes[stage][1] == 'qwen:qwen4-max-preview'
        assert lanes[stage][0] != 'qwen:qwen4-max-preview'


def test_run_stage_has_qwen_runner_and_redacts_token():
    text = (ROOT / 'scripts' / 'devpipe' / 'run_stage.sh').read_text()
    assert 'qwen)' in text
    assert 'source /workspace/qwen_env.sh' in text
    assert 'claude -p --dangerously-skip-permissions --model "$model"' in text
    assert 'token_plan_key' in text and 'text.replace(k,"***")' in text
    assert 'DEEPSEEK_API_KEY' in text and 's|$DEEPSEEK_API_KEY|***|g' in text


@pytest.mark.parametrize('status,code', [(401, 'auth_failed'), (404, 'model_unavailable'), (429, 'rate_limited'), (503, 'provider_failed')])
def test_provider_errors_fall_back_to_qwen(conn, status, code):
    seen = []
    def deepseek(payload, key):
        seen.append(('deepseek', key, json.loads(payload)))
        return status, b''
    def qwen(payload, key):
        body = json.loads(payload)
        seen.append(('qwen', key, body))
        assert body['model'] == QWEN_MODEL and 'thinking' not in body and body.get('enable_thinking') is False
        assert body['temperature'] == 0 and body['response_format'] == {'type': 'json_object'}
        return response({'ok': True})
    client = fallback_client(conn, deepseek, qwen)
    assert client.call('task', 'JSON', {'n': 1}, lambda o: o) == {'ok': True}
    assert client.last_meta['provider'] == 'qwen' and client.last_meta['model'] == QWEN_MODEL
    assert seen[0][0] == 'deepseek' and seen[0][1] == 'ds-key' and seen[0][2]['thinking'] == {'type': 'disabled'}
    assert seen[1][0] == 'qwen' and seen[1][1] == SECRET
    assert SECRET not in json.dumps(client.last_meta) + str([dict(r) for r in conn.execute('SELECT * FROM ai_request')])
    assert code in FALLBACK_CODES


def test_balance_low_falls_back_without_primary_charge(conn):
    qcalls = []
    client = fallback_client(conn, lambda *a: pytest.fail('primary completion'),
                             lambda *a: qcalls.append(1) or response(),
                             balance_transport=lambda key: wallet(available=False))
    assert client.call('task', 'JSON', {}, lambda o: o) == {'ok': True}
    assert len(qcalls) == 1 and client.last_meta['provider'] == 'qwen'
    rows = list(conn.execute('SELECT model,state FROM ai_request'))
    assert len(rows) == 1 and rows[0]['model'] == QWEN_MODEL and rows[0]['state'] == 'done'


def test_balance_unavailable_falls_back(conn):
    client = fallback_client(conn, lambda *a: pytest.fail('primary completion'),
                             lambda *a: response(), balance_transport=None)
    assert client.call('task', 'JSON', {}, lambda o: o) == {'ok': True}
    assert client.last_meta['provider'] == 'qwen'


def test_primary_success_does_not_call_qwen(conn):
    def qwen(*a):
        pytest.fail('qwen called on primary success')
    client = fallback_client(conn, lambda *a: response({'ok': True}), qwen)
    assert client.call('task', 'JSON', {}, lambda o: o) == {'ok': True}
    assert client.last_meta['provider'] == 'deepseek' and client.last_meta['model'] == 'deepseek-chat'


def test_rejected_primary_does_not_fall_back(conn):
    qcalls = []
    client = fallback_client(conn, lambda *a: (400, b''), lambda *a: qcalls.append(1) or response())
    with pytest.raises(DeepSeekError, match='model_or_request_rejected'):
        client.call('task', 'JSON', {}, lambda o: o)
    assert qcalls == [] and client.stopped == 'model_or_request_rejected'


def test_qwen_failure_stops_without_retrying_primary(conn):
    dcalls = []
    client = fallback_client(conn, lambda *a: dcalls.append(1) or (503, b''), lambda *a: (429, b''))
    with pytest.raises(DeepSeekError, match='rate_limited'):
        client.call('task', 'JSON', {}, lambda o: o)
    assert len(dcalls) == 1 and client.stopped == 'rate_limited'
    with pytest.raises(DeepSeekError, match='rate_limited'):
        client.call('task', 'JSON', {'n': 2}, lambda o: o)
    assert len(dcalls) == 1


def test_qwen_result_is_cached_and_validated(conn):
    qcalls = []
    def qwen(payload, key):
        qcalls.append(1)
        return response({'ok': True})
    def validate(obj):
        assert obj == {'ok': True}
        return obj
    first = fallback_client(conn, lambda *a: (503, b''), qwen)
    assert first.call('task', 'JSON', {'n': 1}, validate) == {'ok': True}
    again = fallback_client(conn, lambda *a: (503, b''), lambda *a: pytest.fail('cached qwen replayed'))
    assert again.call('task', 'JSON', {'n': 1}, validate) == {'ok': True}
    assert len(qcalls) == 1 and again.last_meta['provider'] == 'qwen'


def test_review_logs_qwen_provider_after_balance_pause(env):
    conn, sp, log, raw = env
    add(conn, 'x_open', 'opening', '서울 한남동의 달빛정원은 2026-10-01 문을 열었다', rest='달빛정원', date='2026-10-01')
    def qwen(payload, key):
        body = json.loads(payload)
        assert body['model'] == QWEN_MODEL and 'thinking' not in body and body.get('enable_thinking') is False
        data = json.loads(body['messages'][2]['content'])
        return response({'reviews': [{'id': f['id'], 'decision': 'approve', 'reason': 'ok'} for f in data['findings']]})
    client = fallback_client(conn, lambda *a: pytest.fail('primary completion'), qwen,
                             balance_transport=lambda key: wallet(available=False))
    stats = rv.review(conn, str(sp), str(log), raw, client=client)
    assert stats['approved'] == 1 and stats['published'] == 1 and stats['stopped'] is None
    rec = json.loads(log.read_text().splitlines()[0])
    assert rec['model_meta']['provider'] == 'qwen' and rec['model_meta']['model'] == QWEN_MODEL
    assert SECRET not in log.read_text()


def test_qwen_http_endpoint_auth_timeout_and_sanitized_exception(monkeypatch):
    import urllib.request
    seen = []
    class Response:
        status = 200
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def read(self, n):
            assert n == MAX_BODY + 1
            return b'{}'
    class Opener:
        def open(self, req, timeout):
            seen.append(req)
            assert req.full_url == QWEN_URL and req.get_header('Authorization') == 'Bearer fixture-token'
            assert timeout == 180
            return Response()
    monkeypatch.setattr(urllib.request, 'build_opener', lambda *handlers: Opener())
    assert qwen_http_transport(b'{"model":"fixture"}', 'fixture-token') == (200, b'{}')
    assert len(seen) == 1
    class Lost:
        def open(self, *a, **kw):
            raise RuntimeError('fixture-token')
    monkeypatch.setattr(urllib.request, 'build_opener', lambda *handlers: Lost())
    with pytest.raises(DeepSeekError, match='^transport_unknown$'):
        qwen_http_transport(b'{}', 'fixture-token')

def test_transport_unknown_falls_back_to_qwen_and_is_never_replayed(conn):
    assert 'transport_unknown' in FALLBACK_CODES
    dcalls, qcalls = [], []
    def lost(payload, key):
        dcalls.append(1)
        raise DeepSeekError('transport_unknown')
    def qwen(payload, key):
        body = json.loads(payload)
        qcalls.append(body)
        assert body['model'] == QWEN_MODEL and 'thinking' not in body and body.get('enable_thinking') is False
        assert body['temperature'] == 0 and body['response_format'] == {'type': 'json_object'}
        return response({'ok': True})
    first = fallback_client(conn, lost, qwen)
    assert first.call('task', 'JSON', {}, lambda o: o) == {'ok': True}
    assert len(dcalls) == 1 and len(qcalls) == 1
    assert first.stopped is None and first.last_meta['provider'] == 'qwen'
    row = conn.execute("SELECT state,error FROM ai_request WHERE model='deepseek-chat'").fetchone()
    assert row['state'] == 'unknown' and row['error'] == 'transport_unknown'
    # A later run must not resend the lost payload to DeepSeek; the cached backup result answers.
    later = fallback_client(conn, lambda *a: pytest.fail('lost primary replayed'),
                            lambda *a: pytest.fail('cached qwen result replayed'))
    assert later.call('task', 'JSON', {}, lambda o: o) == {'ok': True}
    assert len(dcalls) == 1 and later.last_meta['provider'] == 'qwen'


def test_http_transport_timeout_falls_back_to_qwen(conn, monkeypatch):
    import urllib.request
    from kg_ai.deepseek import http_transport
    class Lost:
        def open(self, *a, **kw):
            raise TimeoutError('lost response')
    monkeypatch.setattr(urllib.request, 'build_opener', lambda *handlers: Lost())
    qcalls = []
    def qwen(payload, key):
        body = json.loads(payload)
        qcalls.append(body)
        assert body['model'] == QWEN_MODEL and 'thinking' not in body and body.get('enable_thinking') is False
        return response({'ok': True})
    client = fallback_client(conn, http_transport, qwen)
    assert client.call('task', 'JSON', {}, lambda o: o) == {'ok': True}
    assert len(qcalls) == 1 and client.stopped is None and client.last_meta['provider'] == 'qwen'


def test_previous_unknown_attempt_routes_to_qwen_without_primary_replay(conn):
    # A prior run reserved and lost the response with no backup configured.
    def lost(*a):
        raise DeepSeekError('transport_unknown')
    prior = Client(Budget(meter(conn)), key='ds-key', transport=lost, balance_transport=healthy_balance)
    with pytest.raises(DeepSeekError, match='transport_unknown'):
        prior.call('task', 'JSON', {}, lambda o: o)
    qcalls = []
    def qwen(payload, key):
        body = json.loads(payload)
        qcalls.append(body)
        assert body['model'] == QWEN_MODEL and 'thinking' not in body and body.get('enable_thinking') is False
        return response({'ok': True})
    later = fallback_client(conn, lambda *a: pytest.fail('unknown primary replayed'), qwen)
    assert later.call('task', 'JSON', {}, lambda o: o) == {'ok': True}
    assert len(qcalls) == 1 and later.stopped is None and later.last_meta['provider'] == 'qwen'
    rows = list(conn.execute('SELECT model,state,error FROM ai_request ORDER BY rowid'))
    assert [(r['model'], r['state']) for r in rows] == [('deepseek-chat', 'unknown'), (QWEN_MODEL, 'done')]
    assert rows[0]['error'] == 'transport_unknown'


def test_previous_unclassifiable_attempt_still_tries_qwen(conn):
    def lost(*a):
        raise DeepSeekError('transport_unknown')
    prior = Client(Budget(meter(conn)), key='ds-key', transport=lost, balance_transport=healthy_balance)
    with pytest.raises(DeepSeekError, match='transport_unknown'):
        prior.call('task', 'JSON', {}, lambda o: o)
    # A crash between reserve and fail leaves no classifiable error; the backup still answers.
    conn.execute("UPDATE ai_request SET error=NULL WHERE state='unknown'"); conn.commit()
    later = fallback_client(conn, lambda *a: pytest.fail('unknown primary replayed'), lambda *a: response({'ok': True}))
    assert later.call('task', 'JSON', {}, lambda o: o) == {'ok': True}
    assert later.last_meta['provider'] == 'qwen'


def test_previous_definite_rejection_does_not_fall_back(conn):
    prior = Client(Budget(meter(conn)), key='ds-key', transport=lambda *a: (400, b''), balance_transport=healthy_balance)
    with pytest.raises(DeepSeekError, match='model_or_request_rejected'):
        prior.call('task', 'JSON', {}, lambda o: o)
    later = fallback_client(conn, lambda *a: pytest.fail('rejected primary replayed'),
                            lambda *a: pytest.fail('qwen after definite rejection'))
    with pytest.raises(DeepSeekError, match='previous_attempt_unknown_or_failed'):
        later.call('task', 'JSON', {}, lambda o: o)
    assert later.stopped == 'previous_attempt_unknown_or_failed'


def test_review_approves_via_qwen_after_primary_timeout(env):
    conn, sp, log, raw = env
    add(conn, 'x_open', 'opening', '서울 한남동의 달빛정원은 2026-10-01 문을 열었다', rest='달빛정원', date='2026-10-01')
    dcalls = []
    def lost(payload, key):
        dcalls.append(1)
        raise DeepSeekError('transport_unknown')
    def qwen(payload, key):
        body = json.loads(payload)
        assert body['model'] == QWEN_MODEL and 'thinking' not in body and body.get('enable_thinking') is False
        data = json.loads(body['messages'][2]['content'])
        return response({'reviews': [{'id': f['id'], 'decision': 'approve', 'reason': 'ok'} for f in data['findings']]})
    client = fallback_client(conn, lost, qwen)
    stats = rv.review(conn, str(sp), str(log), raw, client=client)
    assert stats['approved'] == 1 and stats['published'] == 1 and stats['stopped'] is None
    assert len(dcalls) == 1 and stats['calls'] == 1
    rec = json.loads(log.read_text().splitlines()[0])
    assert rec['model_meta']['provider'] == 'qwen' and rec['model_meta']['model'] == QWEN_MODEL
    assert SECRET not in log.read_text()


def test_failed_truncated_row_is_not_replayed_and_does_not_halt(conn):
    """A billed truncated_json failure is not an unknown in-flight request."""
    from test_deepseek_client import response as resp
    first = fallback_client(conn, lambda *a: (503, b''), lambda *a: resp(finish='length'))
    with pytest.raises(DeepSeekError, match='truncated_json'):
        first.call('task', 'JSON', {}, lambda o: o)
    assert first.stopped == 'truncated_json'
    row = conn.execute("SELECT state,error,result_json FROM ai_request WHERE model=?", (QWEN_MODEL,)).fetchone()
    assert row['state'] == 'failed' and row['error'] == 'truncated_json' and row['result_json'] is None
    later = fallback_client(conn, lambda *a: pytest.fail('primary replayed after billed truncation'),
                            lambda *a: pytest.fail('truncated qwen request replayed'))
    with pytest.raises(DeepSeekError, match='^task_previously_rejected$'):
        later.call('task', 'JSON', {}, lambda o: o)
    assert later.stopped is None


def test_truncated_qwen_batch_is_split_and_review_continues(env):
    """The 2026-11-12 cleanup blocker: a 2-finding Qwen batch hit truncated_json and halted the queue."""
    from test_ai_review import add, TEXT
    from test_deepseek_client import response as resp
    conn, sp, log, raw = env
    add(conn, 'x_open', 'opening', '서울 한남동의 달빛정원은 2026-10-01 문을 열었다', rest='달빛정원', date='2026-10-01')
    add(conn, 'x_rep', 'employment_claim', '한도윤 셰프가 총괄 셰프를 맡는다', chef='한도윤', role='총괄 셰프')
    sizes = []
    def lost(*a):
        raise DeepSeekError('transport_unknown')
    def qwen(payload, key):
        body = json.loads(payload)
        assert body['model'] == QWEN_MODEL and 'thinking' not in body and body.get('enable_thinking') is False
        data = json.loads(body['messages'][2]['content'])
        sizes.append(len(data['findings']))
        if len(data['findings']) > 1:
            return resp(finish='length')
        return resp({'reviews': [{'id': f['id'], 'decision': 'approve', 'reason': 'ok'} for f in data['findings']]})
    client = fallback_client(conn, lost, qwen)
    stats = rv.review(conn, str(sp), str(log), raw, client=client)
    assert sizes[0] == 2 and set(sizes[1:]) == {1}
    assert stats['approved'] == 2 and stats['published'] == 2 and stats['stopped'] is None
    assert stats['remaining'] == 0
    # The truncated 2-finding request stays charged and is not replayed.
    rows = list(conn.execute('SELECT model,state,error FROM ai_request WHERE model=? ORDER BY rowid', (QWEN_MODEL,)))
    assert rows[0]['state'] == 'failed' and rows[0]['error'] == 'truncated_json'
    assert all(r['state'] == 'done' for r in rows[1:])


def test_qwen_transport_uses_longer_timeout(monkeypatch):
    from kg_ai import deepseek as ds
    seen = {}

    class Resp:
        status = 200
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self, n): return b'{}'

    class Opener:
        def open(self, req, timeout):
            seen[req.full_url] = timeout
            return Resp()

    monkeypatch.setattr(ds.urllib.request, 'build_opener', lambda *a: Opener())
    ds.qwen_http_transport(b'{}', 'k')
    ds.http_transport(b'{}', 'k')
    assert seen[ds.QWEN_URL] == ds.QWEN_TIMEOUT_SEC >= 120
    assert seen[ds.URL] == ds.DEEPSEEK_TIMEOUT_SEC == 30
