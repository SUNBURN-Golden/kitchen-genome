"""Heavy collector: shared access policy and zecbot fallback, block hard-stop, caps, politeness, traffic log, 07:00 hand-off."""
import json
import os
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
    cfg['feeds'] = [{'url': 'https://press.test/rss.xml', 'revisit_hours': 6, 'country': 'KR'}]
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


def _crawler(world, routes, egress=(True, 'exit_ip_match'), **caps):
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


@pytest.mark.parametrize('reason', ['exit_ip_unknown', 'exit_ip_mismatch'])
def test_ip_advisory_neither_skips_nor_halts(world, reason):
    hc, web = _crawler(world, default_routes, max_concurrency=1)
    clock, sleep = world[2:4]
    checks = []
    handle = hc.handle

    def advance_after_first_result(result):
        if hc.stats['requests'] == 0:
            sleep(901)
        return handle(result)

    def egress():
        checks.append(clock())
        return True, reason

    hc.egress, hc.handle = egress, advance_after_first_result
    started = clock()
    rep = hc.run(started + 3600)
    assert rep['outcome'] == 'stopped' and rep['stats']['content_handoff'] > 0
    assert rep['egress_reason'] == reason and checks == [started]
    assert max(web.times) > started + 900


@pytest.mark.parametrize('bodies,expected', [
    ((b'{"ip":"49.50.131.207"}',), '49.50.131.207'),
    ((b'<html>not json</html>', b'{"ip":"203.0.113.7"}'), '203.0.113.7'),
    ((b'{"ip":"999.999.1.1"}', b'{"ip":42}', b' 203.0.113.9 \n'), '203.0.113.9'),
    ((b'{"ip":"not-an-ip"}', b'', b''), None),
    ((), None),
])
def test_exit_ip_validation_and_fallback_order(bodies, expected):
    pending, seen = list(bodies), []

    def probe(url, timeout):
        seen.append(url)
        assert timeout == 15
        if not pending:
            return net.Response(url, 503, {}, b'')
        return net.Response(url, 200, {'Content-Type': 'application/json'}, pending.pop(0))

    assert proxynet.exit_ip(probe) == expected
    assert seen == list(proxynet.IP_ECHO_URLS[:len(seen)])


def test_exit_ip_blocked_probe_returns_none():
    def probe(url, timeout):
        r = net.Response(url, 0, {}, b'', error='retry_after')
        r.blocked = True
        return r

    assert proxynet.exit_ip(probe) is None


@pytest.mark.parametrize('outage_at_start', [True, False])
def test_heavy_uses_box_for_confirmed_outage(world, monkeypatch, outage_at_start):
    conn, tmp, clock, sleep, _ = world
    calls = []
    healthy = not outage_at_start
    monkeypatch.setattr(proxynet, 'proxy_alive', lambda: healthy)
    monkeypatch.setattr(proxynet, '_public_ip', lambda *_: '8.8.8.8')

    def send(p, host, port, address, headers, timeout, cap, text, traffic, *, via_proxy, method):
        nonlocal healthy
        url = p.geturl()
        calls.append((url, via_proxy))
        if via_proxy and host == 'api.ipify.org':
            return 200, {'content-type': 'application/json'}, b'{"ip":"203.0.113.1"}', None
        if via_proxy and host == 'press.test':
            healthy = False
            raise proxynet.ProxyDown('socks_greeting_failed')
        return default_routes(url, headers) or (404, {}, b'', None)

    monkeypatch.setattr(proxynet, '_send', send)
    hc = heavy.HeavyCrawler(str(tmp / 'q.db'), conn, cfg=_cfg(max_concurrency=1), clock=clock, sleep=sleep)
    rep = hc.run(clock() + 3600)
    assert rep['outcome'] == 'stopped' and rep['stats']['content_handoff'] > 0
    assert rep['egress'] == 'box' and rep['egress_reason'] == 'tunnel_down'
    relevant = [via for url, via in calls if url.startswith('https://press.test/')]
    assert relevant and relevant[-1] is False
    assert sum(relevant) == (0 if outage_at_start else 1)


def test_block_end_and_old_traffic_thresholds(world):
    conn, tmp, clock, sleep, t = world
    hc, web = _crawler(world, default_routes)
    assert hc.run(clock() + 30)['reason'] == 'block_over'
    hc.bump(requests=8_000, bytes_in=16_000_000_000, candidates=5_000, content=1_500)
    hc.q.commit()
    rep = hc.run(clock() + 3600)
    assert rep['reason'] == 'queue_empty' and web.calls
    assert rep['day_totals']['requests'] > 8_000 and rep['day_totals']['content'] > 1_500
    assert rep['day_totals']['candidates'] > 5_000


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
    assert n == 1
    row = world[0].execute('SELECT host,reason FROM web_run_block WHERE host=?', ('search.test',)).fetchone()
    assert tuple(row) == ('search.test', 'http_403')


def test_excluded_hosts_never_enter_the_frontier(world):
    hc, web = _crawler(world, default_routes)
    for u in ('http://127.0.0.1:8080/x', 'http://localhost/a', 'http://10.0.0.5/a', 'http://[::1/x', 'https://www.instagram.com/x', 'https://bluer.co.kr/a', 'https://a.test/file.pdf', 'https://a.test/v.mp4'):
        assert not hc.add(u, 'article', 1, 0.9)


    assert hc.add('https://guide.michelin.com/kr/ko', 'article', 1, 0.9)


@pytest.mark.parametrize('status,headers,body', [
    (403, {}, b'forbidden'), (429, {}, b'slow down'), (503, {'retry-after': '30'}, b''),
    (200, {'content-type': 'text/html'}, b'<h1>Checking your browser</h1>'), (0, {}, b''),
])
def test_origin_refusal_or_ambiguous_timeout_never_falls_back(monkeypatch, status, headers, body):
    route = proxynet.Route()
    checks, calls = [], []
    monkeypatch.setattr(proxynet, '_public_ip', lambda *_: '8.8.8.8')
    monkeypatch.setattr(proxynet, 'proxy_alive', lambda: not checks.append(True))

    def send(*args, via_proxy, **kwargs):
        calls.append(via_proxy)
        return status, headers, body, 'TimeoutError' if not status else None

    monkeypatch.setattr(proxynet, '_send', send)
    def transport(url, headers, timeout):
        code, headers, body, err = route.request(url, headers)
        return net.Response(url, code, headers, body, error=err)

    client = net.PoliteNet(transport)
    assert client.get('https://refusal.test/a').status == status
    if status:
        assert client.get('https://www.refusal.test/b').blocked
    assert calls == [True] and checks == [True] and route.name == 'zecbot'


def test_local_handshake_failure_needs_confirmation(monkeypatch):
    route = proxynet.Route()
    checks, calls = [], []
    monkeypatch.setattr(proxynet, 'proxy_alive', lambda: not checks.append(True))
    monkeypatch.setattr(proxynet, '_public_ip', lambda *_: '8.8.8.8')
    def send(*args, via_proxy, **kwargs):
        calls.append(via_proxy)
        raise proxynet.ProxyDown('socks_greeting_failed')
    monkeypatch.setattr(proxynet, '_send', send)
    assert route.request('https://example.test/a')[3] == 'proxy_failure'
    assert calls == [True] and checks == [True, True]


def test_size_and_type_guards(monkeypatch):
    class Reply:
        status = 200

        def __init__(self, media, length):
            self.headers = {'Content-Type': media, 'Content-Length': str(length)}

        def getheaders(self):
            return list(self.headers.items())

        def read(self, size):
            pytest.fail('unexpected download')

    class Connection:
        def request(self, method, path, headers):
            self.path = path
            assert headers['User-Agent'] == net.UA

        def getresponse(self):
            return Reply('image/jpeg', 10) if self.path == '/img' else Reply('text/html', proxynet.MAX_WIRE + 1)

        def close(self):
            pass

    monkeypatch.setattr(proxynet, '_connection', lambda *_: Connection())
    monkeypatch.setattr(proxynet, 'proxy_alive', lambda: False)
    monkeypatch.setattr(proxynet, '_public_ip', lambda *_: '8.8.8.8')
    route, traffic = proxynet.Route(), proxynet.Traffic()
    assert route.request('https://venue.test/img', traffic=traffic)[3] == 'skipped_type:image/jpeg'
    assert route.request('https://venue.test/big', traffic=traffic)[3].startswith('skipped_large:')
    assert route.request('https://venue.test/a.jpg', traffic=traffic)[3] == 'skipped_binary_ext'
    assert traffic.snapshot()['skipped_large'] == 1 and traffic.snapshot()['skipped_type'] == 2


def test_extract_order_prefers_heavy_priority(world):
    conn = world[0]
    src = open(os.path.join(ROOT, 'daemon', 'kg_collect', 'cycle.py'), encoding='utf-8').read()
    assert 'LEFT JOIN fetch_priority' in src and 'COALESCE(p.score, 0.75) DESC' in src
    assert conn.execute("SELECT 1 FROM sqlite_master WHERE name='fetch_priority'").fetchone()


def test_cron_script_is_plain_and_ig_free():
    s = open(os.path.join(ROOT, 'scripts', 'heavy_crawl.sh'), encoding='utf-8').read()
    assert 'flock -n' in s and 'KG_IG=0' in s and 'kg_collect.heavy' in s
    assert 'exec python3 -m kg_collect.heavy' in s  # cron timeout must reach the interpreter
    for bad in ('deepseek', 'instagram', 'git ', 'deploy', 'ig_blocked'):
        assert bad not in s.lower().replace('never touches deepseek, git, deploy or instagram', '')


def test_robots_unreachable_is_not_cached_and_url_retries_later(world):
    def routes(url, headers):
        if url == 'https://press.test/robots.txt':
            return 502, {'content-type': 'text/html'}, b'bad gateway', None
        return default_routes(url, headers)
    hc, web = _crawler(world, routes)
    hc.run(world[2]() + 3600)
    assert not any(u.startswith('https://press.test/') and not u.endswith('robots.txt') for u, _ in web.calls)
    row = hc.q.execute("SELECT state, error FROM frontier WHERE url='https://press.test/rss.xml'").fetchone()
    assert row['state'] == 'queued' and row['error'].startswith('robots_unavailable')
    assert world[0].execute("SELECT 1 FROM web_robots_cache WHERE origin='https://press.test'").fetchone() is None


def test_executor_size_is_configurable_and_due_scan_passes_busy_hosts(tmp_path, world):
    import yaml

    cfg = heavy.load_cfg()
    cfg['caps']['max_concurrency'] = 12
    fixture = tmp_path / 'config.yaml'
    fixture.write_text(yaml.safe_dump(cfg))
    assert heavy.load_cfg(fixture)['caps']['max_concurrency'] == 12
    assert cfg['caps']['backlog_for_optional_block'] == 1

    hc, _ = _crawler(world, default_routes)
    hc.access = hc._net(world[0])
    hc.stopped_hosts = set()
    for i in range(425):
        hc.add(f'https://busy.test/p/{i}', 'article', depth=i, score=.9)
    hc.add('https://ready.test/final', 'article', score=.5)
    assert [r['host'] for r in hc.pick({'busy.test'}, 1)] == ['ready.test']


def test_crash_still_writes_traffic_log(world):
    conn, tmp, clock, sleep, t = world
    hc, web = _crawler(world, default_routes)
    def boom(*a, **k):
        raise RuntimeError('x')
    hc.handle = boom
    rep = hc.run(clock() + 3600)
    assert rep['outcome'] == 'stopped' and rep['stats']['errors']
    assert conn.execute("SELECT 1 FROM guard_lane WHERE signature='runtimeerror'").fetchone()
    assert json.loads(open(tmp / 'crawl_traffic.jsonl').read().splitlines()[-1])['stats']['errors']


def test_status_reader_is_not_blocked_by_a_running_block(world):
    conn, tmp, clock, sleep, t = world
    hc, web = _crawler(world, default_routes)
    hc.seed()
    hc.bump(requests=1)                     # uncommitted write in the crawler connection
    import sqlite3
    r = sqlite3.connect(str(tmp / 'q.db'), timeout=0.5)
    assert r.execute('SELECT COUNT(*) FROM frontier').fetchone()[0] > 0
    hc.q.commit()


def test_heavy_keeps_relevant_feed_items_past_the_old_rss_slice(world):
    hc, _ = _crawler(world, default_routes)
    items = [(f'https://press.test/item/{i}', '테스트셰프B 셰프 가상식당1', '') for i in range(55)]
    feed = {'url': 'https://press.test/rss.xml', 'target_id': None, 'query': None, 'depth': 0}
    hc.process_feed(feed, _rss(items))
    assert hc.q.execute("SELECT COUNT(*) FROM frontier WHERE state='queued'").fetchone()[0] == 55
