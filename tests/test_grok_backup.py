"""Grok backup slot (준태님 2026-11-24 t134u). The CLI runner is always faked; no live calls."""
import json
from pathlib import Path
import pytest
from kg_ai import deepseek as ds, grok
from kg_ai.budget import Budget
from kg_ai.deepseek import Client, DeepSeekError, http_transport
from test_deepseek_client import conn, meter, response  # noqa: F401
from test_deepseek_balance import wallet

ROOT = Path(__file__).resolve().parents[1]


def result_event(text, **kw):
    ev = {'type': 'result', 'subtype': 'success', 'is_error': False, 'result': text,
          'usage': {'inputTokens': 900, 'outputTokens': 30, 'cacheReadTokens': 100, 'cacheWriteTokens': 0}}
    ev.update(kw)
    return ev


def grok_client(conn, transport, **kw):
    return Client(Budget(meter(conn)), key='ds-key', transport=lambda *a: pytest.fail('primary completion'),
                  balance_transport=lambda key: wallet(available=False),
                  fallback_provider='grok', fallback_key=grok.LOGIN_MARKER, fallback_transport=transport,
                  fallback_model=kw.get('model', grok.GROK_MODEL))


def test_config_selects_grok_backup():
    cfg = json.loads((ROOT / 'config' / 'ai_providers.json').read_text())
    assert cfg['backup'] == 'grok'
    assert ds.configured_backup() == 'grok'


def test_env_overrides_config(monkeypatch):
    monkeypatch.setenv('KG_AI_BACKUP', 'qwen')
    assert ds.configured_backup() == 'qwen'


def test_auto_client_uses_grok_cli_when_present(monkeypatch, conn):
    monkeypatch.setattr(grok, 'cli_path', lambda: '/usr/bin/cursor-agent')
    c = Client(Budget(meter(conn)), key='', transport=http_transport)
    assert c.fallback_provider == 'grok' and c.fallback_model == grok.GROK_MODEL
    assert c.fallback_transport is grok.grok_transport and c.fallback_available and c.configured


def test_auto_client_without_cli_has_no_backup(conn):
    c = Client(Budget(meter(conn)), key='', transport=http_transport)
    assert c.fallback_provider == 'grok' and not c.fallback_available and not c.configured


def test_balance_low_routes_to_grok_and_keeps_validators(conn):
    seen = []
    def transport(payload, key):
        body = json.loads(payload)
        seen.append(body)
        assert 'thinking' not in body and 'enable_thinking' not in body and body['model'] == grok.GROK_MODEL
        return response([{'id': 'a', 'decision': 'approve'}])
    c = grok_client(conn, transport)
    out = c.call('task', 'JSON', {'n': 1}, lambda o: o, list_key='reviews')
    assert out == {'reviews': [{'id': 'a', 'decision': 'approve'}]}
    assert c.last_meta['provider'] == 'grok' and c.last_meta['model'] == grok.GROK_MODEL
    row = conn.execute('SELECT provider,model,state FROM ai_request').fetchone()
    assert tuple(row) == ('grok', grok.GROK_MODEL, 'done')


def test_grok_429_is_unbilled_and_released_next_run(conn):
    calls = []
    c = grok_client(conn, lambda *a: calls.append(1) or (429, b''))
    with pytest.raises(DeepSeekError, match='rate_limited'):
        c.call('task', 'JSON', {'n': 1}, lambda o: o)
    c2 = Client(Budget(meter(conn)), key='ds-key', transport=lambda *a: pytest.fail('primary'),
                balance_transport=lambda key: wallet(available=False), fallback_provider='grok',
                fallback_key=grok.LOGIN_MARKER, fallback_transport=lambda *a: calls.append(2) or response())
    assert c2.call('task', 'JSON', {'n': 1}, lambda o: o) == {'ok': True}
    assert calls == [1, 2]


def test_transport_maps_result_and_strips_keys(monkeypatch):
    monkeypatch.setattr(grok, 'cli_path', lambda: '/usr/bin/cursor-agent')
    monkeypatch.setenv('XAI_API_KEY', 'never-pass')
    monkeypatch.setenv('DEEPSEEK_API_KEY', 'never-pass')
    got = {}
    def runner(cmd, env, cwd, timeout):
        got.update(cmd=cmd, env=env, timeout=timeout)
        return result_event('{"ok": true}'), ''
    payload = json.dumps({'model': 'grok-4.7-high', 'messages': [
        {'role': 'system', 'content': 'Return JSON only. Do X.'}, {'role': 'user', 'content': '{"a":1}'}]}).encode()
    status, raw = grok.grok_transport(payload, grok.LOGIN_MARKER, timeout=12, runner=runner)
    assert status == 200
    body = json.loads(raw)
    assert body['choices'][0]['finish_reason'] == 'stop' and json.loads(body['choices'][0]['message']['content']) == {'ok': True}
    assert body['usage'] == {'prompt_tokens': 1000, 'completion_tokens': 30, 'total_tokens': 1030, 'prompt_cache_hit_tokens': 100}
    assert '--mode' in got['cmd'] and got['cmd'][got['cmd'].index('--mode') + 1] == 'ask'
    assert got['cmd'][got['cmd'].index('--model') + 1] == 'grok-4.7-high'
    assert 'XAI_API_KEY' not in got['env'] and 'DEEPSEEK_API_KEY' not in got['env'] and got['timeout'] == 12
    assert 'Do X.' in got['cmd'][-1] and '{"a":1}' in got['cmd'][-1]


@pytest.mark.parametrize('ev,err,status', [
    (None, 'Error: not logged in', 401),
    (None, 'rate limit exceeded', 429),
    ({'type': 'result', 'subtype': 'error', 'is_error': True, 'result': 'usage limit reached'}, '', 429),
])
def test_transport_classifies_failures(monkeypatch, ev, err, status):
    monkeypatch.setattr(grok, 'cli_path', lambda: '/usr/bin/cursor-agent')
    payload = json.dumps({'model': 'm', 'messages': []}).encode()
    assert grok.grok_transport(payload, runner=lambda *a: (ev, err))[0] == status


def test_transport_timeout_is_unknown(monkeypatch):
    monkeypatch.setattr(grok, 'cli_path', lambda: '/usr/bin/cursor-agent')
    payload = json.dumps({'model': 'm', 'messages': []}).encode()
    with pytest.raises(DeepSeekError, match='transport_unknown'):
        grok.grok_transport(payload, runner=lambda *a: (None, ''))


def test_missing_cli_is_auth_failed():
    assert grok.grok_transport(json.dumps({'model': 'm', 'messages': []}).encode())[0] == 401


def test_run_cli_stops_at_result_event(tmp_path):
    script = tmp_path / 'fake.sh'
    script.write_text('#!/bin/sh\necho \'{"type":"system"}\'\necho \'{"type":"result","subtype":"success","result":"{}"}\'\nsleep 60\n')
    script.chmod(0o755)
    ev, _ = grok.run_cli([str(script)], {}, str(tmp_path), 20)
    assert ev['type'] == 'result'
