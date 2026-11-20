"""G0 access acceptance with fake dispatches and isolated SQLite scopes."""
import datetime as dt
import multiprocessing
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'daemon'))

from kg_collect import fetch, net, proxynet
from kg_collect.rawstore import RawStore
from kg_daemon.db import connect, migrate


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / 'svc.db'
    with connect(path) as conn:
        migrate(conn)
    return path


def test_robots_and_spacing_apply_only_to_korean_press(db_path):
    now, calls = [100.0], []

    def send(url, headers, timeout):
        assert headers['User-Agent'] == net.UA and 'user-agent' not in headers
        calls.append((url, now[0]))
        if url.endswith('/robots.txt'):
            return net.Response(url, 200, {'Content-Type': 'text/plain'},
                                b'User-agent: KitchenGenomeBot\nDisallow: /denied\nCrawl-delay: 12')
        return net.Response(url, 200, {}, b'fixture article')

    def sleep(seconds):
        now[0] += seconds

    conn = connect(db_path)
    client = net.PoliteNet(send, sleep, lambda: now[0], conn=conn)
    for domain in ('hotelrestaurant.co.kr', 'sub.hankyung.com', 'foodnews.news', 'news.fixture.co.kr'):
        assert client.get(f'https://{domain}/denied').error == 'robots'
        for path in ('a', 'b'):
            assert client.get(f'https://{domain}/{path}', {'user-agent': 'wrong'}).status == 200
        assert [t2 - t1 for (_, t1), (_, t2) in zip(calls[-3:], calls[-2:])] == [12, 12]
    conn.execute("INSERT INTO domain_profile(host, tier_guess, first_seen_at) VALUES ('publisher.kr','press','fixture')")
    assert client.korean_press('publisher.kr')
    earlier = len(calls)
    for domain in ('restaurant.co.kr', 'guide.michelin.com', 'ko.wikipedia.org', 'hankyung.com.example.org', 'fakehankyung.com',
                   'daily.co.kr', 'naver.com.example.org'):
        for path in ('denied', 'kr/ko'):
            assert client.get(f'https://{domain}/{path}').status == 200
        assert client.site_maps(f'https://{domain}/') == []
    assert all(not url.endswith('/robots.txt') and t == now[0] for url, t in calls[earlier:])
    assert net.host_of('https://WWW.식당.kr.:8443/x') == net.host_of('https://m.xn--ok1b941a.kr/x')
    assert {r[0] for r in conn.execute('SELECT host FROM web_host_guard WHERE last_dispatch IS NOT NULL')} == {
        'hotelrestaurant.co.kr', 'sub.hankyung.com', 'foodnews.news', 'news.fixture.co.kr',
    }


def test_redirect_login_block_propagates_and_retry_after_crosses_scopes(db_path, tmp_path):
    t, calls = [12345.0], []
    login = b'<html><title>Sign in</title><form><input type="password"></form></html>'

    def send(url, headers, timeout):
        calls.append(url)
        if net.host_of(url) == 'lead.test':
            return net.Response(url, 302, {'Location': 'https://www.publisher.test/article'}, b'')
        if url.endswith('/article'):
            return net.Response(url, 200, {'Content-Type': 'text/html', 'Retry-After': '60'}, login)
        return net.Response(url, 200, {'Content-Type': 'text/plain'}, b'fixture')

    a = net.PoliteNet(send, clock=lambda: t[0], conn=connect(db_path))
    b = net.PoliteNet(send, clock=lambda: t[0], conn=connect(db_path))
    a.start_run(run_id='a', expires_at=t[0] + 30)
    b.start_run(run_id='b', expires_at=t[0] + 40)
    doc = fetch.Fetcher(a.conn, a, RawStore(str(tmp_path / 'raw'))).fetch('https://lead.test/feed')[0]
    assert (doc.host, doc.outcome) == ('publisher.test', 'fail_login_page')
    assert b.get('https://m.publisher.test/other').error == 'login_page'
    assert a.get('https://lead.test/feed').error == 'login_page'
    assert calls == ['https://lead.test/feed', 'https://www.publisher.test/article', 'https://lead.test/feed']
    assert {tuple(r) for r in a.conn.execute('SELECT run_id,host,reason FROM web_run_block')} == {
        ('a', 'publisher.test', 'login_page'), ('b', 'publisher.test', 'login_page'),
    }
    t[0] += 41
    c = net.PoliteNet(send, clock=lambda: t[0], conn=connect(db_path))
    assert c.get('https://publisher.test/other').error == 'retry_after'
    t[0] += 20
    assert c.get('https://publisher.test/other').status == 200
    assert len(calls) == 4


def _blocked_worker(db_path, ready, release, results, leader):
    conn = connect(db_path)

    def send(url, headers, timeout):
        results.put(('dispatch', url))
        if leader:
            ready.set()
            assert release.wait(5)
        return net.Response(url, 200, {'Content-Type': 'text/html'}, b'<h1>Captcha required</h1>')

    client = net.PoliteNet(send, conn=conn)
    client.start_run()
    if not leader:
        ready.set()
    prefix = 'WWW' if leader else 'm'
    results.put(('result', client.get(f'https://{prefix}.race.test/a').error))


def test_host_lock_spans_processes_and_commits_captcha_block_before_release(db_path):
    ctx = multiprocessing.get_context('spawn')
    sending, contender_ready, release = ctx.Event(), ctx.Event(), ctx.Event()
    results = ctx.Queue()
    a = ctx.Process(target=_blocked_worker, args=(db_path, sending, release, results, True))
    b = ctx.Process(target=_blocked_worker, args=(db_path, contender_ready, release, results, False))
    a.start()
    try:
        assert sending.wait(5)
        b.start()
        assert contender_ready.wait(5)
        release.set()
        events = [results.get(timeout=5) for _ in range(3)]
        assert sorted(events) == [('dispatch', 'https://WWW.race.test/a'),
                                  ('result', 'challenge_page'), ('result', 'domain_paused')]
    finally:
        release.set()
        for child in (a, b):
            if child.pid is not None:
                child.join(5)
                if child.is_alive():
                    child.terminate()
                    child.join()
    assert a.exitcode == b.exitcode == 0


@pytest.mark.parametrize('destination', [
    'file:///fixture', 'https://user:pass@a.test/', 'http://127.1/', 'http://[::ffff:127.0.0.1]/',
    'http://100.64.0.5/', 'https://x.local/', 'https://[::1',
])
def test_redirect_rechecks_destinations_and_writes_are_rejected(destination):
    calls = []

    def send(url, *args):
        calls.append(url)
        return net.Response(url, 302, {'location': destination}, b'')

    client = net.PoliteNet(send)
    assert client.get('https://start.test/', method='POST').error == 'unsupported_method'
    assert client.get('https://start.test/').error == 'unsafe_destination'
    assert calls == ['https://start.test/']


def test_real_transport_rejects_mixed_private_dns_answers(monkeypatch):
    def answers(*args, **kwargs):
        return [(0, 0, 0, '', (address, 443)) for address in ('8.8.8.8', '127.0.0.1')]

    monkeypatch.setattr(proxynet.socket, 'getaddrinfo', answers)
    monkeypatch.setattr(proxynet, '_send', lambda *a, **k: pytest.fail('unsafe resolved address'))
    assert proxynet.Route().request('https://public.test/')[3] == 'unsafe_destination'


def test_js_shell_is_unavailable_and_browser_callback_is_never_called(db_path, tmp_path):
    url = 'https://www.lottehotel.com/seoul-hotel/ko/dining.html'
    response = net.Response(url, 200, {'Content-Type': 'text/html'}, b'<html><div id="root">Loading...</div></html>')
    conn = connect(db_path)
    client = net.PoliteNet(lambda *_: response, conn=conn)
    f = fetch.Fetcher(conn, client, RawStore(str(tmp_path / 'raw')), browser=lambda *_: pytest.fail('unguarded browser'))
    doc = f.fetch(url, allow_browser=True)[0]
    assert doc.outcome == 'fail_empty' and doc.error == 'js_renderer_unavailable'


def test_migration_requeues_only_stale_michelin_policy_skips(tmp_path):
    old = tmp_path / 'old-schema'
    old.mkdir()
    schema = ROOT / 'schema/migrations'
    for path in sorted(schema.glob('*.sql'))[:11]:
        (old / path.name).write_bytes(path.read_bytes())
    conn = connect(tmp_path / 'upgrade.db')
    migrate(conn, old)
    rows = [
        ('manual', 'guide.michelin.com/selection', 'skipped', 'manual_only: robots/ToS forbid automated access (inaccessible, tracked for a human)'),
        ('robots', 'guide.michelin.com/robots-policy', 'blocked', 'fail_robots: robots'),
        ('http', 'guide.michelin.com/blocked', 'blocked', 'fail_blocked: http_403'),
        ('unrelated', 'guide.michelin.com.example.org/selection', 'skipped', 'banned'),
    ]
    conn.executemany("""INSERT INTO crawl_job(job_id,url,state,last_error,path,pool,created_at)
                        VALUES (?, 'https://' || ?, ?, ?, 'directory', 'discovery', 'fixture')""", rows)
    conn.execute("INSERT INTO web_host_guard(host,paused) VALUES ('blocked.test','blocked:403')")
    conn.commit()
    assert migrate(conn) == ['0012_web_access_guard.sql']
    assert {r['job_id']: r['state'] for r in conn.execute('SELECT * FROM crawl_job')} == {
        'manual': 'queued', 'robots': 'queued', 'http': 'blocked', 'unrelated': 'skipped',
    }
    assert conn.execute("SELECT paused FROM web_host_guard WHERE host='blocked.test'").fetchone()[0] == 'blocked:403'
    assert migrate(conn) == []


def test_retry_after_http_date():
    stamp = dt.datetime(2026, 11, 20, 7, tzinfo=dt.timezone.utc).timestamp()
    assert net.retry_after('Fri, 20 Nov 2026 07:00:00 GMT', 0) == stamp
    assert net.retry_after('invalid', 0) is None


def test_daily_adapters_join_existing_scope_between_processes(db_path):
    clock = lambda: 100.0
    first = net.PoliteNet(lambda url, *_: net.Response(url, 401, {}, b''), clock=clock, conn=connect(db_path))
    first.start_run(run_id='daily', join=True)
    assert first.get('https://publisher.test/a').error == 'login_page'
    second = net.PoliteNet(lambda *_: pytest.fail('access wall already recorded'), clock=clock, conn=connect(db_path))
    second.start_run(run_id='daily', join=True)
    assert second.get('https://www.publisher.test/b').error == 'login_page'
    second.finish_run()
    next_run = net.PoliteNet(lambda url, *_: net.Response(url, 200, {}, b'public'), clock=clock, conn=connect(db_path))
    assert next_run.get('https://publisher.test/b').status == 200
    assert first.conn.execute('SELECT reason FROM web_run_block').fetchone()[0] == 'login_page'


def test_korean_robots_provider_refusal_blocks_content(db_path):
    calls = []

    def send(url, *args):
        calls.append(url)
        return net.Response(url, 403, {}, b'')

    client = net.PoliteNet(send, conn=connect(db_path))
    assert client.get('https://newsis.com/feed').error == 'http_403'
    assert client.get('https://m.newsis.com/article').error == 'domain_paused'
    assert calls == ['https://newsis.com/robots.txt']
    assert tuple(client.conn.execute('SELECT host,reason FROM web_run_block').fetchone()) == ('newsis.com', 'http_403')
