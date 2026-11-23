"""Unbilled HTTP rejections (429/quota/401/404) must not poison a request key forever."""
import pytest
from test_deepseek_client import conn, meter, healthy_balance, response  # noqa: F401
from kg_ai.budget import Budget
from kg_ai.deepseek import Client, DeepSeekError


@pytest.mark.parametrize('status,body,code', [
    (429, b'{"error":{"code":"insufficient_quota"}}', 'quota_exhausted'),
    (429, b'slow down', 'rate_limited'),
    (401, b'', 'auth_failed'),
    (404, b'', 'model_unavailable'),
])
def test_unbilled_rejection_is_retried_by_a_later_run(conn, status, body, code):
    first = Client(Budget(meter(conn)), key='k', transport=lambda *a: (status, body), balance_transport=healthy_balance)
    with pytest.raises(DeepSeekError, match=code):
        first.call('task', 'JSON', {}, lambda o: o)
    row = conn.execute('SELECT state, error FROM ai_request').fetchone()
    assert row['state'] == 'unknown' and row['error'] == code
    calls = []
    later = Client(Budget(meter(conn)), key='k', transport=lambda *a: (calls.append(1) or response({'ok': True})),
                   balance_transport=healthy_balance)
    assert later.call('task', 'JSON', {}, lambda o: o) == {'ok': True}
    assert len(calls) == 1
    assert [tuple(r) for r in conn.execute('SELECT state, error FROM ai_request')] == [('done', None)]


@pytest.mark.parametrize('status,code', [(400, 'model_or_request_rejected'), (503, 'provider_failed')])
def test_ambiguous_or_request_errors_stay_replay_forbidden(conn, status, code):
    first = Client(Budget(meter(conn)), key='k', transport=lambda *a: (status, b''), balance_transport=healthy_balance)
    with pytest.raises(DeepSeekError, match=code):
        first.call('task', 'JSON', {}, lambda o: o)
    later = Client(Budget(meter(conn)), key='k', transport=lambda *a: pytest.fail('replayed'), balance_transport=healthy_balance)
    with pytest.raises(DeepSeekError, match='previous_attempt'):
        later.call('task', 'JSON', {}, lambda o: o)
