import socket
from kg_collect import proxynet, net


def _fake(addr):
    return lambda host, port, type=None: [(socket.AF_INET, socket.SOCK_STREAM, 6, '', (addr, port))]


def test_resolver_fake_ip_accepted_for_hostname(monkeypatch):
    monkeypatch.setattr(proxynet.socket, 'getaddrinfo', _fake('198.18.0.1'))
    assert proxynet._public_ip('www.bing.com', 443) == '198.18.0.1'


def test_fake_ip_literal_still_rejected(monkeypatch):
    monkeypatch.setattr(proxynet.socket, 'getaddrinfo', _fake('198.18.0.1'))
    assert proxynet._public_ip('198.18.0.1', 443) is None
    assert not net.valid_destination('https://198.18.0.1/')


def test_private_resolution_still_rejected(monkeypatch):
    monkeypatch.setattr(proxynet.socket, 'getaddrinfo', _fake('10.0.0.5'))
    assert proxynet._public_ip('evil.example', 443) is None


def test_proxy_dials_hostname_for_fake_ip(monkeypatch):
    seen = {}
    monkeypatch.setattr(proxynet, 'socks5_connect', lambda h, p, t: seen.setdefault('h', h))
    conn = proxynet._connection('www.bing.com', 443, '198.18.0.1', False, 5, True)
    conn._create_connection(None, 5, None)
    assert seen['h'] == 'www.bing.com'
