"""Use fake model transports and fixture credential paths."""
from pathlib import Path
import sys
import urllib.request
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'daemon'))


@pytest.fixture(autouse=True)
def no_live_deepseek(monkeypatch, tmp_path):
    from kg_ai import deepseek

    monkeypatch.setattr(deepseek, 'QWEN_KEY_PATH', str(tmp_path / 'missing-model-key'))
    # The Grok backup runs a local CLI; tests never reach it unless they inject a runner.
    from kg_ai import grok
    monkeypatch.setattr(grok, 'cli_path', lambda: None)
    monkeypatch.delenv('KG_AI_BACKUP', raising=False)
    original = urllib.request.OpenerDirector.open
    def guarded(opener, request, *args, **kwargs):
        url = request.full_url if isinstance(request, urllib.request.Request) else str(request)
        if 'deepseek.com' in url or 'qwencloudapi.com' in url:
            pytest.fail('live provider HTTP forbidden: inject both transports')
        return original(opener, request, *args, **kwargs)
    monkeypatch.setattr(urllib.request.OpenerDirector, 'open', guarded)
