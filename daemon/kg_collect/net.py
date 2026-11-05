"""Polite HTTP: robots.txt, per-domain delay, banned hosts, block detection, response validation.

Policy (docs/data/SOURCES.md): on 429/403/503/challenge the DOMAIN pauses. We never rotate identity, proxy, or account, never solve
captchas, never use a headless browser to get past a block. A 200 that is a login wall, a challenge page or an empty shell is a FAILURE."""
import contextlib
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import urllib.robotparser

from kg_daemon.db import ROOT

CFG = json.load(open(os.path.join(ROOT, 'crawler', 'config.json'), encoding='utf-8'))
UA = CFG['user_agent']
BANNED = tuple(CFG['banned_hosts'])
TRAP = tuple(CFG['trapped_url_patterns'])

LOGIN_HINTS = ('로그인', '회원가입', 'sign in', 'log in', 'login', '비밀번호', 'password', '아이디')
CHALLENGE_HINTS = ('just a moment', 'captcha', 'checking your browser', 'access denied', 'attention required', 'unusual traffic', '접근이 제한', '비정상적인 접근', 'security check')


_PCT_STRAY = re.compile(r'%(?![0-9A-Fa-f]{2})')
_PATH_SAFE = "/%:@!$&'()*+,;=-._~"
_QUERY_SAFE = _PATH_SAFE + '?'


def _idna_host(host: str) -> str:
    if not host or host.isascii():
        return host
    try:
        return host.encode('idna').decode('ascii')
    except UnicodeError:
        return '.'.join(lbl if lbl.isascii() else 'xn--' + lbl.encode('punycode').decode('ascii') for lbl in host.split('.'))


def to_uri(url: str) -> str:
    """IRI -> URI (RFC 3987 3.1): non-ASCII host labels -> IDNA, non-ASCII/space in path, query and fragment -> UTF-8 percent-encoding.
    Existing %XX escapes are kept as they are (no double encoding); a stray '%' not followed by two hex digits becomes %25.
    Already-ASCII URLs without stray characters come back unchanged."""
    if not url:
        return url
    p = urllib.parse.urlsplit(url.strip())
    netloc = p.netloc
    if netloc and not netloc.isascii():
        userinfo, _, hostport = netloc.rpartition('@')
        host, port = hostport, ''
        if not hostport.startswith('[') and ':' in hostport:
            host, _, port = hostport.rpartition(':')
        netloc = (urllib.parse.quote(userinfo, safe="%:!$&'()*+,;=-._~") + '@' if userinfo else '') + _idna_host(host) + (':' + port if port else '')

    def enc(part, safe):
        return urllib.parse.quote(_PCT_STRAY.sub('%25', part), safe=safe) if part else part
    return urllib.parse.urlunsplit((p.scheme, netloc, enc(p.path, _PATH_SAFE), enc(p.query, _QUERY_SAFE), enc(p.fragment, _QUERY_SAFE)))


def host_of(url: str) -> str:
    return urllib.parse.urlparse(url or '').netloc.lower().removeprefix('www.').removeprefix('m.')


SEARCH_HOSTS = {'bing.com'}          # lead-generating search endpoints: their own pages are never 'sources' (Bot adds the host of the configured endpoint)


def is_search_host(host: str) -> bool:
    return bool(host) and any(host == h or host.endswith('.' + h) for h in SEARCH_HOSTS)


def is_banned(url: str) -> bool:
    h = host_of(url)
    return any(h == b or h.endswith('.' + b) or b in h for b in BANNED)


class Response:
    def __init__(self, url, status, headers, body, final_url=None, error=None):
        self.url, self.status, self.headers, self.body = url, status, {k.lower(): v for k, v in (headers or {}).items()}, body or b''
        self.final_url = final_url or url
        self.error = error

    @property
    def ctype(self) -> str:
        return (self.headers.get('content-type') or '').split(';')[0].strip().lower()

    def text(self) -> str:
        m = re.search(r'charset=([\w-]+)', self.headers.get('content-type') or '', re.I) or re.search(rb'<meta[^>]+charset=["\']?([\w-]+)', self.body[:4096], re.I)
        encs = ([m.group(1).decode() if isinstance(m.group(1), bytes) else m.group(1)] if m else []) + ['utf-8', 'cp949']
        for e in encs:
            try:
                return self.body.decode(e)
            except (UnicodeDecodeError, LookupError):
                continue
        return self.body.decode('utf-8', 'replace')


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def default_transport(url, headers, timeout):
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.build_opener(NoRedirect()).open(req, timeout=timeout) as r:
            return Response(url, r.status, dict(r.headers), r.read(8_000_000), final_url=r.geturl())
    except urllib.error.HTTPError as e:
        return Response(url, e.code, dict(e.headers or {}), e.read(200_000) if e.fp else b'', final_url=url)
    except Exception as e:  # noqa: BLE001
        return Response(url, 0, {}, b'', error=type(e).__name__)


class PoliteNet:
    """All network access of the bot goes through here (so tests can inject a transport and a fake clock)."""

    def __init__(self, transport=default_transport, sleep=time.sleep, clock=time.time, min_delay=None, timeout=20, respect_robots=True, conn=None):
        self.conn = conn
        self.transport, self.sleep, self.clock, self.timeout = transport, sleep, clock, timeout
        self.min_delay = max(5, CFG['min_delay_sec'] if min_delay is None else min_delay)
        self.respect_robots = respect_robots
        self._robots: dict = {}
        self._last: dict = {}
        self.paused_hosts: dict = {}     # host -> reason (blocked this process; persisted by the caller in domain_profile)
        self.request_count = 0

    def _dispatch(self, url, headers, timeout):
        from .limits import Limits
        limits = Limits(self.conn) if self.conn is not None else None
        h = host_of(url)
        with limits.network_lock() if limits else contextlib.nullcontext():
            if limits:
                c = self.conn
                row = c.execute('SELECT * FROM web_host_guard WHERE host=?', (h,)).fetchone()
                if row and row['paused']:
                    self.paused_hosts[h] = row['paused']
                    return Response(url, 0, {}, b'', error='domain_paused')
                delay = self.min_delay
                if row:
                    delay = max(delay, row['delay_sec'])
                p = urllib.parse.urlparse(url)
                rp = self._robots.get(f'{p.scheme}://{p.netloc}')
                if rp:
                    delay = max(delay, rp.crawl_delay(UA) or rp.crawl_delay('*') or 0)
                if row and row['last_dispatch'] is not None:
                    wait = delay - (self.clock() - row['last_dispatch'])
                    if wait > 0:
                        self.sleep(wait)
                limits.dispatch()
                c.execute('INSERT INTO web_host_guard(host,last_dispatch) VALUES (?,?) ON CONFLICT(host) DO UPDATE SET last_dispatch=excluded.last_dispatch', (h, self.clock()))
                c.commit()
            r = self.transport(url, headers, timeout)
            if r.status in (403, 429, 503) or (r.status == 200 and looks_like_challenge(r)):
                self.paused_hosts[h] = f'blocked:{r.status}'
                if limits:
                    self.conn.execute('UPDATE web_host_guard SET paused=? WHERE host=?', (self.paused_hosts[h], h))
                    self.conn.commit()
            return r

    # ── robots ──
    def robots(self, url):
        p = urllib.parse.urlparse(to_uri(url))
        base = f'{p.scheme}://{p.netloc}'
        if base not in self._robots:
            self._wait(p.netloc)
            r = self._dispatch(base + '/robots.txt', {'User-Agent': UA}, self.timeout)
            self.request_count += 1
            rp = urllib.robotparser.RobotFileParser()
            if r.status == 200 and r.ctype in ('text/plain', ''):
                rp.parse(r.text().splitlines())
                self._robots[base] = rp
                if self.conn is not None:
                    self.conn.execute('UPDATE web_host_guard SET delay_sec=? WHERE host=?', (max(self.min_delay, rp.crawl_delay(UA) or rp.crawl_delay('*') or 0), host_of(url)))
                    self.conn.commit()
            elif r.status in (401, 403, 429, 503) or host_of(url) in self.paused_hosts:
                rp.disallow_all = True      # robots itself forbidden = treat as disallow
                self._robots[base] = rp
            else:
                rp.allow_all = True          # missing robots.txt (404/5xx/network) => RFC 9309: allowed
                self._robots[base] = rp
        return self._robots[base]

    def robots_info(self, url) -> dict:
        url = to_uri(url)
        rp = self.robots(url)
        return {'allowed': rp.can_fetch(UA, url), 'crawl_delay': rp.crawl_delay(UA) or rp.crawl_delay('*')}

    def site_maps(self, url) -> list:
        return list(self.robots(url).site_maps() or [])

    def _wait(self, netloc):
        last = self._last.get(netloc)
        if last is not None:
            wait = self.min_delay - (self.clock() - last)
            if wait > 0:
                self.sleep(wait)
        self._last[netloc] = self.clock()

    def get(self, url, extra_headers=None) -> Response:
        url = to_uri(url)               # IRIs (e.g. ko.wikipedia.org/wiki/마스터셰프_코리아) must be ASCII on the wire AND for the robots check
        h = host_of(url)
        if self.conn is not None:
            from .limits import Limits, DailyLimit
            if not Limits(self.conn).admit(url):
                raise DailyLimit('web_daily_candidate_cap')
        if is_banned(url):
            return Response(url, 0, {}, b'', error='banned')
        if h in self.paused_hosts:
            return Response(url, 0, {}, b'', error='domain_paused')
        delay = self.min_delay
        if self.respect_robots:
            info = self.robots_info(url)
            if not info['allowed']:
                return Response(url, 0, {}, b'', error='robots')
            delay = max(delay, info['crawl_delay'] or 0)       # robots Crawl-delay wins when longer
        netloc = urllib.parse.urlparse(url).netloc
        last = self._last.get(netloc)
        if last is not None and delay - (self.clock() - last) > 0:
            self.sleep(delay - (self.clock() - last))
        self._last[netloc] = self.clock()
        headers = {'User-Agent': UA, 'Accept-Language': 'ko,en;q=0.8', 'Accept': '*/*', **(extra_headers or {})}
        r = self._dispatch(url, headers, self.timeout)
        self.request_count += 1
        if r.status in (429, 403, 503) or (r.status == 200 and looks_like_challenge(r)):
            self.paused_hosts[h] = f'blocked:{r.status}'
        return r


def looks_like_challenge(r: Response) -> bool:
    if not r.ctype.startswith('text/html'):
        return False
    t = r.text()[:6000].lower()
    return any(h in t for h in CHALLENGE_HINTS) and len(re.sub(r'<[^>]+>', '', t)) < 1500


def looks_like_login(final_url: str, html: str, text_chars: int) -> bool:
    u = (final_url or '').lower()
    if re.search(r'/(login|signin|sign-in|member/login|auth)\b', urllib.parse.urlparse(u).path) and text_chars < 1500:
        return True
    low = html.lower()
    has_pw = bool(re.search(r'<input[^>]+type=["\']?password', low))
    hits = sum(1 for h in LOGIN_HINTS if h in low[:20000])
    return has_pw and hits >= 2 and text_chars < 1200
