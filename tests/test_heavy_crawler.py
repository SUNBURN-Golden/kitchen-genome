"""Heavy collector: zecbot SOCKS egress only (no direct fallback), block hard-stop, caps, politeness, traffic log, 07:00 hand-off."""
import http.server
import json
import os
import socket
import sys
import threading
import time

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'daemon'))

from kg_collect import cycle, heavy, net, proxynet   # noqa: E402
from kg_daemon.db import connect   # noqa: E402

SEED = json.load(open(os.path.join(ROOT, 'tests', 'fixtures', 'seed_minimal.json'), encoding='utf-8'))
ROBOTS_OK = b'User-agent: *\nDisallow: /private\n'


def _cfg(**caps):
    cfg = heavy.load_cfg()
    cfg['search'] = [{'id': 't', 'endpoint': 'https://search.test/rss?q={q}', 'revisit_hours': 24}]
    cfg['feeds'] = [{'url': 'https://press.test/rss.xml', 'revisit_hours': 6}]
    cfg['guides'] = []
    cfg['query_modifiers'] = {'chef': ['셰프'], 'restaurant': ['오픈'], 'general': []}
    cfg['caps'].update(caps)
    return cfg


def _rss(items):
    body = ''.join(f'<item><title>{t}</title><link>{u}</link><description>{d}</description></item>' for u, t, d in items)
    return f'<?xml version="1.0"?><rss><channel>{body}</channel></rss>'.encode()


ARTICLE = ('<html><head><title>가상식당1 테스트셰프B 셰프 인터뷰</title></head><body><article>'
           + ''.join(f'<p>테스트셰프B 셰프가 가상식당1을 새로 오픈했다({i}). 수셰프 출신으로 서울 가상동에서 레스토랑을 {i}년째 운영한다.</p>' for i in range(6))
           + '<a href="https://press.test/a2">가상식당2 테스트셰프C 셰프 레스토랑</a><a href="https://press.test/private/x">셰프 비공개</a>'
           + '<a href="https://img.test/photo.jpg">셰프 사진</a></article></body></html>').encode()


class FakeWeb:
    def __init__(self, routes, clock):
        self.routes, self.clock = routes, clock
        self.times = []
        self.calls, self.active, self.lock, self.max_par, self.same_host_overlap = [], {}, threading.Lock(), 0, False

    def __call__(self, url, headers, timeout):
        h = net.host_of(url)
        with self.lock:
            if self.active.get(h):
                self.same_host_overlap = True
            self.active[h] = self.active.get(h, 0) + 1
            self.max_par = max(self.max_par, sum(self.active.values()))
            self.calls.append((url, dict(headers)))
            self.times.append(self.clock())
        try:
            time.sleep(0.01)
            r = self.routes(url, headers)
            return r if r is not None else (404, {'content-type': 'text/html'}, b'nf', None)
        finally:
            with self.lock:
                self.active[h] -= 1


@pytest.fixture
def world(tmp_path):
    conn = connect(str(tmp_path / 'svc.db'))
    cycle.init_db(conn, seed=SEED)
    t = [1_800_000_000.0]
    clock = lambda: t[0]   # noqa: E731

    def sleep(s):
        t[0] += s
    return conn, tmp_path, clock, sleep, t


def _crawler(world, routes, egress=(True, '49.50.131.207'), **caps):
    conn, tmp, clock, sleep, t = world
    web = FakeWeb(routes, clock)
    hc = heavy.HeavyCrawler(str(tmp / 'q.db'), conn, cfg=_cfg(**caps), transport=web, clock=clock, sleep=sleep, egress=lambda: egress, data_dir=str(tmp))
    return hc, web


def default_routes(url, headers):
    if url.endswith('/robots.txt'):
        return 200, {'content-type': 'text/plain'}, ROBOTS_OK, None
    if url.startswith('https://search.test/rss'):
        return 200, {'content-type': 'application/rss+xml'}, _rss([('https://press.test/a1', '테스트셰프B 셰프 가상식당1 오픈', '셰프')]), None
    if url == 'https://press.test/rss.xml':
        if headers.get('If-None-Match') == '"v1"':
            return 304, {}, b'', None
        return 200, {'content-type': 'application/rss+xml', 'etag': '"v1"'}, _rss([('https://press.test/a1', '테스트셰프B 셰프 가상식당1', ''), ('https://press.test/off', '주식 시황', '')]), None
    if url in ('https://press.test/a1', 'https://press.test/a2'):
        return 200, {'content-type': 'text/html; charset=utf-8'}, ARTICLE, None
    return None


def test_run_hands_off_promising_pages_logs_traffic_and_respects_politeness(world):
    conn, tmp, clock, sleep, t = world
    hc, web = _crawler(world, default_routes)
    rep = hc.run(clock() + 3 * 3600, block='test')
    assert rep['outcome'] == 'stopped', rep
    urls = [u for u, _ in web.calls]
    assert 'https://press.test/private/x' not in urls                    # robots Disallow
    assert not any('img.test' in u for u in urls)                       # binary/image never requested
    assert 'https://press.test/off' not in urls                         # irrelevant feed item below the floor
    assert not web.same_host_overlap and web.max_par <= 6
    rows = conn.execute("SELECT f.url, p.score FROM fetch_record f JOIN fetch_priority p USING(fetch_id) WHERE p.source='heavy'").fetchall()
    assert {r['url'] for r in rows} >= {'https://press.test/a1'} and all(0 < r['score'] <= 1 for r in rows)
    line = json.loads(open(tmp / 'crawl_traffic.jsonl').read().splitlines()[-1])
    assert line['stats']['requests'] == len(web.calls) and line['day_totals']['requests'] == len(web.calls)
    assert line['stats']['content_handoff'] == len(rows)


def test_revisit_sends_conditional_get_and_counts_304(world):
    conn, tmp, clock, sleep, t = world
    hc, web = _crawler(world, default_routes)
    hc.run(clock() + 3600, block='a')
    t[0] += 7 * 3600                                                    # feed revisit (6 h) is due
    hc2, web2 = _crawler(world, default_routes)
    rep = hc2.run(clock() + 3600, block='b')
    feed_calls = [h for u, h in web2.calls if u == 'https://press.test/rss.xml']
    assert feed_calls and feed_calls[0].get('If-None-Match') == '"v1"' and rep['stats']['not_modified'] >= 1


def test_proxy_down_skips_without_any_request(world):
    hc, web = _crawler(world, default_routes, egress=(False, 'proxy_down'))
    rep = hc.run(world[2]() + 3600)
    assert rep['outcome'] == 'skipped' and rep['reason'] == 'proxy_down' and web.calls == []


def test_tunnel_lost_mid_run_halts(world):
    def routes(url, headers):
        if 'press.test' in url and not url.endswith('robots.txt'):
            raise proxynet.ProxyDown('connect:ConnectionRefusedError')
        return default_routes(url, headers)
    hc, web = _crawler(world, routes)
    rep = hc.run(world[2]() + 3600)
    assert rep['outcome'] == 'halted' and rep['reason'] == 'proxy_lost'


def test_block_end_and_caps(world):
    conn, tmp, clock, sleep, t = world
    hc, web = _crawler(world, default_routes)
    assert hc.run(clock() + 30)['reason'] == 'block_over'
    hc, web = _crawler(world, default_routes, daily_requests=3)
    rep = hc.run(clock() + 3600)
    assert rep['reason'] == 'daily_request_cap' and len(web.calls) <= 3 + 6


def test_deadline_is_a_hard_stop(world):
    conn, tmp, clock, sleep, t = world
    hc, web = _crawler(world, default_routes)
    end = clock() + 90
    rep = hc.run(end)                                                    # 90 s block: nothing is dispatched in the last 25 s
    assert rep['reason'] in ('deadline', 'queue_empty', 'nothing_ready') and web.times and max(web.times) < end


def test_blocked_domain_is_paused_not_bypassed(world):
    def routes(url, headers):
        if url.startswith('https://search.test/rss'):
            return 403, {'content-type': 'text/html'}, b'forbidden', None
        return default_routes(url, headers)
    hc, web = _crawler(world, routes)
    hc.run(world[2]() + 3600)
    n = sum(1 for u, _ in web.calls if u.startswith('https://search.test/rss'))
    assert n == 1 and hc.host_paused('search.test')


def test_excluded_hosts_never_enter_the_frontier(world):
    hc, web = _crawler(world, default_routes)
    for u in ('https://www.instagram.com/x', 'https://guide.michelin.com/kr/ko', 'https://a.test/file.pdf', 'https://a.test/v.mp4'):
        assert not hc.add(u, 'article', 1, 0.9)


# ── proxynet: no leak to the box IP ──
def test_wrong_proxy_port_raises_and_never_connects_direct(monkeypatch):
    monkeypatch.setenv('KG_WEB_PROXY', 'socks5h://127.0.0.1:1')
    seen = []
    real = socket.create_connection

    def spy(addr, *a, **k):
        seen.append(addr)
        return real(addr, *a, **k)
    monkeypatch.setattr(socket, 'create_connection', spy)
    with pytest.raises(proxynet.ProxyDown):
        proxynet.request('https://example.com/', {'User-Agent': 'x'}, timeout=3)
    assert seen == [('127.0.0.1', 1)]                                    # only the proxy was dialled
    r = net.default_transport('https://example.com/', {'User-Agent': 'x'}, 3)
    assert r.status == 0 and r.error == 'proxy_down'
    assert all(a == ('127.0.0.1', 1) for a in seen)
    assert proxynet.egress_ok() == (False, 'proxy_down')


def test_loopback_fixture_servers_stay_local(monkeypatch):
    monkeypatch.setenv('KG_WEB_PROXY', 'socks5h://127.0.0.1:1')

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header('Content-Type', 'text/plain')
            self.end_headers()
            self.wfile.write(b'ok')

        def log_message(self, *a):
            pass
    srv = http.server.HTTPServer(('127.0.0.1', 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        st, _, body, _ = proxynet.request(f'http://127.0.0.1:{srv.server_port}/x', {}, timeout=3)
        assert st == 200 and body == b'ok'
    finally:
        srv.shutdown()


def test_size_and_type_guards(monkeypatch):
    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            if self.path == '/img':
                self.send_header('Content-Type', 'image/jpeg')
                self.send_header('Content-Length', '10')
                self.end_headers()
                self.wfile.write(b'0123456789')
            else:
                self.send_header('Content-Type', 'text/html')
                self.send_header('Content-Length', str(5_000_000))
                self.end_headers()
                try:
                    self.wfile.write(b'a' * 100)
                except OSError:
                    pass

        def log_message(self, *a):
            pass
    srv = http.server.HTTPServer(('127.0.0.1', 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    tr = proxynet.Traffic()
    try:
        st, _, body, err = proxynet.request(f'http://127.0.0.1:{srv.server_port}/img', {}, timeout=3, traffic=tr)
        assert body == b'' and err.startswith('skipped_type')
        st, _, body, err = proxynet.request(f'http://127.0.0.1:{srv.server_port}/big', {}, timeout=3, traffic=tr)
        assert body == b'' and err.startswith('skipped_large')
        assert proxynet.request('https://x.test/a.jpg', {}, traffic=tr)[3] == 'skipped_binary_ext'
        assert tr.snapshot()['skipped_large'] == 1 and tr.snapshot()['skipped_type'] == 2
    finally:
        srv.shutdown()


def test_extract_order_prefers_heavy_priority(world):
    conn = world[0]
    src = open(os.path.join(ROOT, 'daemon', 'kg_collect', 'cycle.py'), encoding='utf-8').read()
    assert 'LEFT JOIN fetch_priority' in src and 'COALESCE(p.score, 0.75) DESC' in src
    assert conn.execute("SELECT 1 FROM sqlite_master WHERE name='fetch_priority'").fetchone()


def test_cron_script_is_plain_and_ig_free():
    s = open(os.path.join(ROOT, 'scripts', 'heavy_crawl.sh'), encoding='utf-8').read()
    assert 'flock -n' in s and 'KG_IG=0' in s and 'kg_collect.heavy' in s
    for bad in ('deepseek', 'instagram', 'git ', 'deploy', 'ig_blocked'):
        assert bad not in s.lower().replace('never touches deepseek, git, deploy or instagram', '')
