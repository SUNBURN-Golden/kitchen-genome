"""Tests may use local fixture HTTP, but never a live provider endpoint."""
import urllib.request
import pytest


@pytest.fixture(autouse=True)
def no_live_deepseek(monkeypatch):
    original = urllib.request.OpenerDirector.open
    def guarded(opener, request, *args, **kwargs):
        url = request.full_url if isinstance(request, urllib.request.Request) else str(request)
        if 'deepseek.com' in url or 'qwencloudapi.com' in url:
            pytest.fail('live provider HTTP forbidden: inject both transports')
        return original(opener, request, *args, **kwargs)
    monkeypatch.setattr(urllib.request.OpenerDirector, 'open', guarded)
    monkeypatch.setenv('KG_IG', '0')
