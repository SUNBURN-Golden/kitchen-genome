"""Foundation unit tests: polite net layer, raw store, html helpers (offline)."""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'daemon'))
sys.path.insert(0, os.path.join(ROOT, 'tests'))

from kg_collect import htmlx   # noqa: E402
from kg_collect.net import PoliteNet, Response   # noqa: E402
from kg_collect.rawstore import RawStore   # noqa: E402


# ── net ──
def make_net(routes, delay=5.0):
    sleeps, t = [], [0.0]
    def transport(url, headers, timeout):
        t[0] += 0.01
        return routes(url)
    return PoliteNet(transport=transport, sleep=lambda s: (sleeps.append(s), t.__setitem__(0, t[0] + s)), clock=lambda: t[0], min_delay=delay), sleeps


def test_net_obeys_robots_disallow_and_crawl_delay():
    def routes(url):
        if url.endswith('/robots.txt'):
            return Response(url, 200, {'content-type': 'text/plain'}, b'User-agent: *\nDisallow: /private\nCrawl-delay: 9\n')
        return Response(url, 200, {'content-type': 'text/html'}, b'<html>ok</html>')
    net, sleeps = make_net(routes)
    assert net.get('https://hankyung.com/private/a').error == 'robots'
    net.get('https://hankyung.com/a')
    net.get('https://hankyung.com/b')
    assert max(sleeps) >= 8.9                                  # Crawl-delay 9 beats our 5 s minimum


def test_net_pauses_domain_on_block_and_never_retries_it():
    calls = []
    def routes(url):
        calls.append(url)
        return Response(url, 404 if url.endswith('robots.txt') else 429, {}, b'slow down')
    net, _ = make_net(routes)
    assert net.get('https://y.test/a').status == 429
    r = net.get('https://y.test/b')
    assert r.error == 'domain_paused' and not any(u.endswith('/b') for u in calls)


def test_net_banned_host_is_never_requested():
    called = []
    net, _ = make_net(lambda u: called.append(u) or Response(u, 200, {}, b'x'))
    assert net.get('https://www.instagram.com/p/abc').error == 'banned'
    assert not called


# ── rawstore / htmlx ──
def test_rawstore_is_content_addressed(tmp_path):
    r = RawStore(str(tmp_path))
    a = r.put('h.test', b'abc', 'html')
    assert r.put('h.test', b'abc', 'html') == a and r.read(a[0]) == b'abc'


def test_feed_links_and_pagination():
    h = '<html><head><link rel="alternate" type="application/rss+xml" href="/rss.xml"></head><body><a rel="next" href="/p2">next</a></body></html>'
    assert htmlx.feed_links(h, 'https://a.test/x') == ['https://a.test/rss.xml']
    assert htmlx.parse_html(h + '<p>' + 'x' * 10 + '</p>', 'https://a.test/p1')['next_url'] == 'https://a.test/p2'
