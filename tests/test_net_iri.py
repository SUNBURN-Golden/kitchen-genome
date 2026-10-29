"""IRI handling in the polite net layer (offline): non-ASCII URLs are percent-encoded before the request and before the robots check."""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'daemon'))

from kg_collect.net import PoliteNet, Response, to_uri   # noqa: E402

KO = 'https://ko.wikipedia.org/wiki/마스터셰프_코리아'
KO_ENC = 'https://ko.wikipedia.org/wiki/%EB%A7%88%EC%8A%A4%ED%84%B0%EC%85%B0%ED%94%84_%EC%BD%94%EB%A6%AC%EC%95%84'


def test_to_uri_encodes_path_query_fragment_and_idna_host():
    assert to_uri(KO) == KO_ENC
    assert to_uri('https://예시.한국/검색?q=셰프 모수&x=1#요약') == \
        'https://xn--vv4b11d.xn--3e0b707e/%EA%B2%80%EC%83%89?q=%EC%85%B0%ED%94%84%20%EB%AA%A8%EC%88%98&x=1#%EC%9A%94%EC%95%BD'
    assert to_uri('https://user@예시.한국:8443/a') == 'https://user@xn--vv4b11d.xn--3e0b707e:8443/a'


def test_to_uri_never_double_encodes_and_leaves_ascii_alone():
    assert to_uri(KO_ENC) == KO_ENC
    assert to_uri(to_uri(KO)) == KO_ENC
    mixed = 'https://ko.wikipedia.org/wiki/%EB%A7%88스터'          # half-encoded IRI
    assert to_uri(mixed) == 'https://ko.wikipedia.org/wiki/%EB%A7%88%EC%8A%A4%ED%84%B0'
    for u in ('https://bntnews.co.kr/article/view/bnt202610270224', 'https://x.test/a?b=c&d=%2F#f', 'https://x.test/p;q=1/~u/(x)'):
        assert to_uri(u) == u
    assert to_uri('https://x.test/100%25/50%off') == 'https://x.test/100%25/50%25off'   # stray % only
    assert to_uri('') == '' and to_uri(None) is None


def test_net_get_sends_ascii_and_checks_robots_on_the_encoded_path():
    seen = []

    def transport(url, headers, timeout):
        url.encode('ascii')                     # the real transport (http.client) raises UnicodeEncodeError on non-ASCII
        seen.append(url)
        if url.endswith('/robots.txt'):
            return Response(url, 200, {'content-type': 'text/plain'}, 'User-agent: *\nDisallow: /wiki/특수:\n'.encode())
        return Response(url, 200, {'content-type': 'text/html'}, b'<html>ok</html>')
    net = PoliteNet(transport=transport, sleep=lambda s: None, clock=lambda: 0.0, min_delay=0)
    r = net.get(KO)
    assert r.status == 200 and r.error is None
    assert seen == ['https://ko.wikipedia.org/robots.txt', KO_ENC]
    # robots rules written in Hangul still apply to both the raw IRI and its encoded form
    assert net.get('https://ko.wikipedia.org/wiki/특수:검색').error == 'robots'
    assert net.get('https://ko.wikipedia.org/wiki/%ED%8A%B9%EC%88%98:%EA%B2%80%EC%83%89').error == 'robots'
    assert net.robots_info(KO)['allowed'] is True
    assert len(seen) == 2                       # robots fetched once per host, blocked URLs never requested
