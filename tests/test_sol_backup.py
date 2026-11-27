"""Sol 6.2 backup slot (준태님 2026-11-27 t142u). The CLI runner is always faked; no live calls."""
import json
from pathlib import Path
import pytest
from kg_ai import deepseek as ds, sol
from kg_ai.budget import Budget
from kg_ai.deepseek import Client, DeepSeekError, http_transport
from test_deepseek_client import conn, meter, response  # noqa: F401
from test_deepseek_balance import wallet

ROOT = Path(__file__).resolve().parents[1]


def turn_result(text, **usage):
    u = {'input_tokens': 900, 'output_tokens': 30, 'cached_input_tokens': 100, 'cache_write_input_tokens': 0,
         'reasoning_output_tokens': 0}
    u.update(usage)
    return {'text': text, 'usage': u}


def sol_client(conn, transport, **kw):
    return Client(Budget(meter(conn)), key='ds-key', transport=lambda *a: pytest.fail('primary completion'),
                  balance_transport=lambda key: wallet(available=False),
                  fallback_provider='sol', fallback_key=sol.LOGIN_MARKER, fallback_transport=transport,
                  fallback_model=kw.get('model', sol.SOL_MODEL))


def test_config_backup_is_sol():
    cfg = json.loads((ROOT / 'config' / 'ai_providers.json').read_text())
    assert cfg['backup'] == 'sol'
    assert cfg['backup'] in ds.BACKUPS
    assert ds.configured_backup() == 'sol'


def test_config_file_selects_sol(monkeypatch, tmp_path):
    p = tmp_path / 'ai_providers.json'
    p.write_text('{"backup": "sol"}')
    monkeypatch.setattr(ds, 'PROVIDERS_CONFIG', str(p))
    assert ds.configured_backup() == 'sol'


def test_env_overrides_config(monkeypatch):
    monkeypatch.setenv('KG_AI_BACKUP', 'qwen')
    assert ds.configured_backup() == 'qwen'


def test_auto_client_uses_sol_cli_when_present(monkeypatch, conn):
    monkeypatch.setenv('KG_AI_BACKUP', 'sol')
    monkeypatch.setattr(sol, 'cli_path', lambda: '/usr/bin/codex')
    c = Client(Budget(meter(conn)), key='', transport=http_transport)
    assert c.fallback_provider == 'sol' and c.fallback_model == sol.SOL_MODEL
    assert c.fallback_transport is sol.sol_transport and c.fallback_available and c.configured


def test_kg_sol_model_overrides_default(monkeypatch, conn):
    monkeypatch.setenv('KG_AI_BACKUP', 'sol')
    monkeypatch.setenv('KG_SOL_MODEL', 'gpt-6.2-sol-fast')
    monkeypatch.setattr(sol, 'cli_path', lambda: '/usr/bin/codex')
    c = Client(Budget(meter(conn)), key='', transport=http_transport)
    assert c.fallback_model == 'gpt-6.2-sol-fast'


def test_auto_client_without_cli_has_no_backup(monkeypatch, conn):
    monkeypatch.setenv('KG_AI_BACKUP', 'sol')
    c = Client(Budget(meter(conn)), key='', transport=http_transport)
    assert c.fallback_provider == 'sol' and not c.fallback_available and not c.configured


def test_balance_low_routes_to_sol_and_keeps_validators(conn):
    seen = []
    def transport(payload, key):
        body = json.loads(payload)
        seen.append(body)
        assert 'thinking' not in body and 'enable_thinking' not in body and body['model'] == sol.SOL_MODEL
        return response([{'id': 'a', 'decision': 'approve'}])
    c = sol_client(conn, transport)
    out = c.call('task', 'JSON', {'n': 1}, lambda o: o, list_key='reviews')
    assert out == {'reviews': [{'id': 'a', 'decision': 'approve'}]}
    assert c.last_meta['provider'] == 'sol' and c.last_meta['model'] == sol.SOL_MODEL
    row = conn.execute('SELECT provider,model,state FROM ai_request').fetchone()
    assert tuple(row) == ('sol', sol.SOL_MODEL, 'done')


def test_sol_429_is_unbilled_and_released_next_run(conn):
    calls = []
    c = sol_client(conn, lambda *a: calls.append(1) or (429, b''))
    with pytest.raises(DeepSeekError, match='rate_limited'):
        c.call('task', 'JSON', {'n': 1}, lambda o: o)
    c2 = Client(Budget(meter(conn)), key='ds-key', transport=lambda *a: pytest.fail('primary'),
                balance_transport=lambda key: wallet(available=False), fallback_provider='sol',
                fallback_key=sol.LOGIN_MARKER, fallback_transport=lambda *a: calls.append(2) or response())
    assert c2.call('task', 'JSON', {'n': 1}, lambda o: o) == {'ok': True}
    assert calls == [1, 2]



def test_transport_respects_effort_override(monkeypatch):
    monkeypatch.setattr(sol, 'cli_path', lambda: '/usr/bin/codex')
    monkeypatch.setenv('KG_SOL_EFFORT', 'xhigh')
    got = {}
    def runner(cmd, env, cwd, timeout):
        got['cmd'] = cmd
        return turn_result('{"ok": true}'), ''
    payload = json.dumps({'model': 'gpt-6.2-sol', 'messages': []}).encode()
    assert sol.sol_transport(payload, runner=runner)[0] == 200
    assert 'model_reasoning_effort="xhigh"' in got['cmd']


def test_transport_maps_result_and_strips_keys(monkeypatch):
    monkeypatch.setattr(sol, 'cli_path', lambda: '/usr/bin/codex')
    monkeypatch.setenv('OPENAI_API_KEY', 'never-pass')
    monkeypatch.setenv('DEEPSEEK_API_KEY', 'never-pass')
    got = {}
    def runner(cmd, env, cwd, timeout):
        got.update(cmd=cmd, env=env, timeout=timeout)
        return turn_result('{"ok": true}'), ''
    payload = json.dumps({'model': 'gpt-6.2-sol', 'messages': [
        {'role': 'system', 'content': 'Return JSON only. Do X.'}, {'role': 'user', 'content': '{"a":1}'}]}).encode()
    status, raw = sol.sol_transport(payload, sol.LOGIN_MARKER, timeout=12, runner=runner)
    assert status == 200
    body = json.loads(raw)
    assert body['choices'][0]['finish_reason'] == 'stop' and json.loads(body['choices'][0]['message']['content']) == {'ok': True}
    assert body['usage'] == {'prompt_tokens': 900, 'completion_tokens': 30, 'total_tokens': 930, 'prompt_cache_hit_tokens': 100}
    assert got['cmd'][:2] == ['/usr/bin/codex', 'exec']
    assert got['cmd'][got['cmd'].index('--sandbox') + 1] == 'read-only'
    assert got['cmd'][got['cmd'].index('-m') + 1] == 'gpt-6.2-sol'
    assert 'model_reasoning_effort="high"' in got['cmd']
    assert '--ephemeral' in got['cmd'] and '--json' in got['cmd']
    assert 'OPENAI_API_KEY' not in got['env'] and 'DEEPSEEK_API_KEY' not in got['env'] and got['timeout'] == 12
    assert 'Do X.' in got['cmd'][-1] and '{"a":1}' in got['cmd'][-1]


@pytest.mark.parametrize('result,err,status', [
    (None, 'Error: not logged in', 401),
    (None, 'rate limit exceeded', 429),
    (turn_result('{}'), 'usage limit reached', 429),
])
def test_transport_classifies_failures(monkeypatch, result, err, status):
    monkeypatch.setattr(sol, 'cli_path', lambda: '/usr/bin/codex')
    payload = json.dumps({'model': 'm', 'messages': []}).encode()
    assert sol.sol_transport(payload, runner=lambda *a: (result, err))[0] == status


def test_transport_timeout_is_unknown(monkeypatch):
    monkeypatch.setattr(sol, 'cli_path', lambda: '/usr/bin/codex')
    payload = json.dumps({'model': 'm', 'messages': []}).encode()
    with pytest.raises(DeepSeekError, match='transport_unknown'):
        sol.sol_transport(payload, runner=lambda *a: (None, ''))


def test_missing_cli_is_auth_failed():
    assert sol.sol_transport(json.dumps({'model': 'm', 'messages': []}).encode())[0] == 401


def test_run_cli_stops_at_turn_completed(tmp_path):
    script = tmp_path / 'fake.sh'
    script.write_text(
        '#!/bin/sh\n'
        'echo \'{"type":"thread.started"}\'\n'
        'echo \'{"type":"item.completed","item":{"id":"item_0","type":"agent_message","text":"{}"}}\'\n'
        'echo \'{"type":"turn.completed","usage":{"input_tokens":10,"cached_input_tokens":2,"output_tokens":3}}\'\n'
        'sleep 60\n')
    script.chmod(0o755)
    ev, _ = sol.run_cli([str(script)], {}, str(tmp_path), 20)
    assert ev['text'] == '{}' and ev['usage']['input_tokens'] == 10


def test_extract_call_budget_auto_caps_to_worst_case_timeout():
    # Default collect extract window is 1200s; worst-case sol call is 240s → 5 calls.
    assert sol.extract_call_budget(1200, 0) == 5
    assert sol.extract_call_budget(420, 0) == 1          # after plan+fetch ate ~780s of the 1200s window
    assert sol.extract_call_budget(239, 0) == 0
    assert sol.extract_call_budget(None, 0) is None      # no deadline: no numeric cap
    assert sol.extract_call_budget(1200, 0, max_calls=2) == 2
    assert sol.extract_call_budget(1200, 0, max_calls=0) == 0
    assert sol.extract_call_budget(36_000, 0) == 150     # a long overnight window is not clipped


def test_allow_extract_call_stops_before_a_call_can_overrun():
    assert sol.allow_extract_call(1200, 0, 0) is True
    assert sol.allow_extract_call(1200, 5, 0) is False   # cap reached
    assert sol.allow_extract_call(200, 0, 0) is False    # remaining < 240s worst case
    assert sol.allow_extract_call(None, 0, 0) is True
    assert sol.allow_extract_call(1200, 1, 0, max_calls=2) is True
    assert sol.allow_extract_call(1200, 2, 0, max_calls=2) is False
    # Even with a high count cap, do not start if the remaining window is too short.
    assert sol.allow_extract_call(100, 0, 0, max_calls=10) is False


def test_sol_extract_blocked_when_backup_is_sol_and_budget_spent(monkeypatch, conn):
    from kg_collect.deepseek import Adapters
    c = sol_client(conn, lambda *a: pytest.fail('should not call'))
    c.deadline = 100  # shorter than SOL_TIMEOUT_SEC, so the first call is refused
    adapter = Adapters(c)
    assert adapter._sol_extract_will_run() is True
    assert adapter._sol_extract_blocked() is True


def test_sol_extract_not_blocked_when_primary_is_usable(monkeypatch, conn):
    from kg_collect.deepseek import Adapters
    # DeepSeek has balance: extracts stay on the primary, no sol cap.
    c = Client(Budget(meter(conn)), key='ds-key', transport=lambda *a: pytest.fail('unused'),
               balance_transport=lambda key: wallet('5.00', available=True),
               fallback_provider='sol', fallback_key=sol.LOGIN_MARKER,
               fallback_transport=lambda *a: pytest.fail('unused'), fallback_model=sol.SOL_MODEL)
    c.deadline = 100
    adapter = Adapters(c)
    assert adapter._sol_extract_will_run() is False
    assert adapter._sol_extract_blocked() is False

