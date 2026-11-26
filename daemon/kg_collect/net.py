"""Shared public-web access policy for HTTP collectors."""
import contextlib
import datetime as dt
import email.utils
import fcntl
import hashlib
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import socket
import threading
import time
import urllib.parse
import urllib.robotparser
import uuid

import yaml

from kg_daemon.db import ROOT
from .limits import record_dispatch

CFG = json.load(open(os.path.join(ROOT, 'crawler', 'config.json'), encoding='utf-8'))
UA = CFG['user_agent']
BANNED = tuple(CFG['banned_hosts'])
TRAP = tuple(CFG['trapped_url_patterns'])

LOGIN_HINTS = ('로그인', '회원가입', 'sign in', 'log in', 'login', '비밀번호', 'password', '아이디')
CHALLENGE_HINTS = ('just a moment', 'captcha', 'checking your browser', 'access denied', 'access_denied',
                   'you have been blocked', 'request blocked', 'attention required', 'unusual traffic',
                   '접근이 제한', '비정상적인 접근', 'security check')


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
    try:
        host = urllib.parse.urlsplit(url or '').hostname or ''
    except ValueError:
        return ''
    host = _idna_host(host.rstrip('.').lower())
    while host.startswith(('www.', 'm.')) and host.count('.') > 1:
        host = host.split('.', 1)[1]
    return host


SEARCH_HOSTS = {'bing.com'}          # lead-generating search endpoints: their own pages are never 'sources' (Bot adds the host of the configured endpoint)


def is_search_host(host: str) -> bool:
    return bool(host) and any(host == h or host.endswith('.' + h) for h in SEARCH_HOSTS)


def is_banned(url: str) -> bool:
    return matches_domain(host_of(url), BANNED)


class Response:
    def __init__(self, url, status, headers, body, final_url=None, error=None):
        self.url, self.status, self.headers, self.body = url, status, {k.lower(): v for k, v in (headers or {}).items()}, body or b''
        self.final_url = final_url or url
        self.error = error
        self.blocked = False

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


def matches_domain(host, domains):
    return any(host == domain or host.endswith('.' + domain) for domain in domains)


def valid_destination(url):
    """Reject unsafe schemes, credentials, local name forms and non-global IP literals."""
    try:
        p = urllib.parse.urlsplit(url)
        host = (p.hostname or '').lower().rstrip('.')
        p.port
    except ValueError:
        return False
    if p.scheme not in ('http', 'https') or p.username is not None or p.password is not None:
        return False
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        # inet_aton also recognizes noncanonical IPv4 spellings accepted by system resolvers.
        try:
            socket.inet_aton(host)
        except OSError:
            return ('.' in host and not host.endswith(('.localhost', '.local', '.localdomain', '.internal', '.home.arpa'))
                    and all(re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', label) for label in _idna_host(host).split('.')))
        return False
    return public_address(address)


def public_address(address):
    address = getattr(address, 'ipv4_mapped', None) or address
    return address.is_global and not address.is_multicast and not address.is_reserved


def _press_hosts():
    def load(name):
        return yaml.safe_load((Path(ROOT) / 'sources' / name).read_text(encoding='utf-8'))

    registry = load('registry.yaml')
    discovery = load('discovery_seeds.yaml')
    hosts = set(registry['access']['korean_press_hosts'])
    hosts.update(h for h, meta in discovery['publication']['press_hosts'].items() if meta.get('country') == 'KR')
    feeds = [*discovery['feeds'], *discovery['sitemaps'], *load('heavy_sources.yaml')['feeds']]
    hosts.update(host_of(entry['url']) for entry in feeds if entry.get('country') == 'KR')
    exclude = registry['access'].get('korean_press_exclude', [])
    return frozenset(h for h in (host_of('https://' + x) for x in hosts) if not matches_domain(h, exclude))


KOREAN_PRESS = _press_hosts()
NEWS_LABELS = {'news', 'newsroom', 'press', 'media', 'daily', 'newspaper', 'journal'}
NEWS_TOKENS = ('news', 'press', 'ilbo', 'sinmun', 'times', 'daily', 'herald', 'tv', 'media', 'journal')
RETRY_AFTER_CAP = 7 * 86400
REDIRECTS = (301, 302, 303, 307, 308)
ROBOTS_TTL = 86400
RUN_TTL = 3600
_busy = {}
_busy_lock = threading.Lock()


def default_transport(url, headers, timeout, *, on_traffic=None):
    from . import proxynet

    status, headers, body, error = proxynet.request(url, headers, timeout=timeout, text_only=False, max_body=8_000_000, on_traffic=on_traffic)
    return Response(url, status, headers, body, error=error)


class PoliteNet:
    """Serialize dispatch and block recording by normalized host across all web collectors."""

    def __init__(self, transport=default_transport, sleep=time.sleep, clock=time.time, min_delay=None, timeout=20, conn=None,
                 *, korean_press_hosts=(), extra_banned=(), run_id=None, deadline=None):
        self.conn = conn
        self.transport, self.sleep, self.clock, self.timeout = transport, sleep, clock, timeout
        self.min_delay = max(5, CFG['min_delay_sec'] if min_delay is None else min_delay)
        self.press_hosts = KOREAN_PRESS.union(korean_press_hosts)
        self.extra_banned = extra_banned
        self.run_id = run_id
        self.deadline = deadline
        self._robots = {}
        self._last = {}
        self._retry_until = {}
        self.paused_hosts = {}
        self.request_count = self.robots_count = 0
        self._expires = None
        db_path = conn.execute('PRAGMA database_list').fetchone()[2] if conn is not None else None
        self.lock_dir = Path(db_path).resolve().parent / 'web_host_locks' if db_path else None

    def korean_press(self, host):
        if matches_domain(host, self.press_hosts):
            return True
        if not host.endswith('.kr'):
            return False
        labels = host.split('.')
        # A news-like first label ( incl. the publisher's own name, e.g. yonhapnews.co.kr) is press.
        if any(token in labels[0] for token in NEWS_TOKENS):
            return True
        # Labels to the left of the registered domain may indicate a news desk.
        domain_labels = 3 if host.endswith(('.co.kr', '.or.kr', '.ne.kr', '.re.kr', '.go.kr', '.ac.kr')) else 2
        if NEWS_LABELS.intersection(labels[:-domain_labels]):
            return True
        return self.conn is not None and self.conn.execute(
            "SELECT 1 FROM domain_profile WHERE host=? AND tier_guess='press'", (host,)).fetchone() is not None

    def start_run(self, *, run_id=None, expires_at=None, join=False):
        self.run_id = run_id or 'web_' + uuid.uuid4().hex
        self.paused_hosts.clear()
        now = self.clock()
        self._expires = now + RUN_TTL if expires_at is None else expires_at
        if self.conn is not None:
            # The INSERT obtains the write lock before reading overlapping blocks.
            if join:
                self.conn.execute('INSERT OR IGNORE INTO web_access_run VALUES (?,?)', (self.run_id, self._expires))
                self._expires = self.conn.execute('SELECT expires_at FROM web_access_run WHERE run_id=?', (self.run_id,)).fetchone()[0]
            else:
                self.conn.execute('INSERT INTO web_access_run VALUES (?,?)', (self.run_id, self._expires))
            self.conn.execute("""INSERT OR IGNORE INTO web_run_block
                SELECT ?,b.host,b.reason,b.blocked_at FROM web_run_block b JOIN web_access_run r USING(run_id)
                WHERE r.expires_at>? AND r.run_id<>? ORDER BY b.blocked_at""", (self.run_id, now, self.run_id))
            self.conn.commit()
        return self.run_id

    def finish_run(self):
        if self.conn is not None and self.run_id is not None:
            self.conn.execute('UPDATE web_access_run SET expires_at=MIN(expires_at,?) WHERE run_id=?', (self.clock(), self.run_id))
            self.conn.commit()
        self.run_id = self._expires = None
        self.paused_hosts.clear()

    def _ensure_run(self):
        if self.conn is not None:
            # A caller's pending SQLite write must never wait on a host lock.
            self.conn.commit()
        if self.run_id is None or (self._expires is not None and self._expires <= self.clock()):
            self.start_run()

    @contextlib.contextmanager
    def _host_lock(self, host):
        with _busy_lock:
            lock = _busy.setdefault(host, threading.Lock())
        remaining = max(0, self.deadline - self.clock()) if self.deadline is not None else None
        acquired = lock.acquire() if remaining is None else lock.acquire(timeout=remaining)
        if not acquired:
            yield False
            return
        try:
            if self.lock_dir is None:
                yield True
                return
            self.lock_dir.mkdir(exist_ok=True)
            key = hashlib.sha256(host.encode()).hexdigest()
            with (self.lock_dir / key).open('a') as path:
                if self.deadline is None:
                    fcntl.flock(path, fcntl.LOCK_EX)
                else:
                    end = time.monotonic() + max(0, self.deadline - self.clock())
                    while True:
                        try:
                            fcntl.flock(path, fcntl.LOCK_EX | fcntl.LOCK_NB)
                            break
                        except BlockingIOError:
                            wait = min(0.1, end - time.monotonic())
                            if wait <= 0:
                                yield False
                                return
                            time.sleep(wait)
                yield True
        finally:
            lock.release()

    def blocked_response(self, url):
        host = host_of(url)
        reason = self.paused_hosts.get(host)
        retry_until = self._retry_until.get(host, 0)
        if self.conn is not None:
            row = self.conn.execute("""SELECT reason FROM web_run_block b JOIN web_access_run r USING(run_id)
                WHERE b.run_id=? AND host=? AND expires_at>?""", (self.run_id, host, self.clock())).fetchone()
            reason = row[0] if row else None
            row = self.conn.execute('SELECT retry_after_at FROM web_host_guard WHERE host=?', (host,)).fetchone()
            retry_until = (row[0] or 0) if row else 0
        if reason or retry_until > self.clock():
            r = Response(url, 0, {}, b'', error='login_page' if reason == 'login_page' else ('domain_paused' if reason else 'retry_after'))
            r.blocked = True
            return r
        return None

    def _record_block(self, host, reason, retry_until):
        self.paused_hosts[host] = reason
        if retry_until:
            self._retry_until[host] = max(retry_until, self._retry_until.get(host, 0))
        if self.conn is not None:
            now = self.clock()
            self.conn.execute("""INSERT OR IGNORE INTO web_run_block
                SELECT run_id,?,?,? FROM web_access_run WHERE expires_at>? OR run_id=?""", (host, reason, now, now, self.run_id))
            if retry_until:
                self.conn.execute("""INSERT INTO web_host_guard(host,retry_after_at) VALUES (?,?)
                    ON CONFLICT(host) DO UPDATE SET retry_after_at=MAX(COALESCE(web_host_guard.retry_after_at,0),excluded.retry_after_at)""",
                    (host, retry_until))
            self.conn.commit()

    def _dispatch(self, url, headers, *, robots=False, delay=None, diagnostic=False, timeout=None, before_send=None):
        host = host_of(url)
        if res := self.blocked_response(url):
            return res
        timeout = self.timeout if timeout is None else timeout
        press = self.korean_press(host)
        wait = 0
        if press:
            row = self.conn.execute('SELECT last_dispatch,delay_sec FROM web_host_guard WHERE host=?', (host,)).fetchone() if self.conn is not None else None
            last = row[0] if row else self._last.get(host)
            spacing = max(self.min_delay, delay or 0, row[1] if row and delay is None else 0)
            if last is not None:
                wait = max(0, last + spacing - self.clock())
        if self.deadline is not None and self.clock() + wait + timeout > self.deadline:
            return Response(url, 0, {}, b'', error='block_end')
        if wait:
            self.sleep(wait)
        if before_send and not before_send():
            return Response(url, 0, {}, b'', error='attempts_exhausted')
        if self.conn is not None:
            day = dt.datetime.fromtimestamp(self.clock(), dt.timezone(dt.timedelta(hours=9))).date().isoformat()
            if not diagnostic:
                from .fetch import canonicalize_url
                record_dispatch(self.conn, canonicalize_url(url), robots=robots, day=day)
        if press:
            now = self.clock()
            self._last[host] = now
            if self.conn is not None:
                self.conn.execute("""INSERT INTO web_host_guard(host,last_dispatch,delay_sec) VALUES (?,?,?)
                    ON CONFLICT(host) DO UPDATE SET last_dispatch=excluded.last_dispatch, delay_sec=excluded.delay_sec""", (host, now, spacing))
                self.conn.commit()
        self.request_count += not diagnostic
        self.robots_count += robots
        headers = {k: v for k, v in headers.items() if k.lower() not in ('user-agent', 'host')}
        headers['User-Agent'] = UA
        if self.transport is default_transport and self.conn is not None:
            from .proxynet import record_traffic
            r = self.transport(url, headers, timeout,
                               on_traffic=lambda route, counters: record_traffic(self.conn, day, route, counters))
        else:
            r = self.transport(url, headers, timeout)
        reason = block_reason(r)
        if reason:
            self._record_block(host, reason, retry_after(r.headers.get('retry-after'), self.clock()))
            r.error, r.blocked = reason, True
        return r

    def _cached_robots(self, base):
        if self.conn is not None:
            row = self.conn.execute('SELECT status,body,fetched_at FROM web_robots_cache WHERE origin=?', (base,)).fetchone()
            if row and row['fetched_at'] + ROBOTS_TTL > self.clock():
                return _parse_robots(row['status'], row['body'])
        cached = self._robots.get(base)
        if cached and cached[0] + ROBOTS_TTL > self.clock():
            return cached[1]
        return None

    def _cache_robots(self, base, response):
        body = response.text()[:200_000]
        if response.error or (response.status not in (404, 410) and
                              (response.status != 200 or response.ctype not in ('text/plain', ''))):
            return None
        rp = _parse_robots(response.status, body)
        self._robots[base] = (self.clock(), rp)
        if self.conn is not None:
            self.conn.execute('INSERT OR REPLACE INTO web_robots_cache VALUES (?,?,?,?)', (base, response.status, body, self.clock()))
            self.conn.commit()
        return rp

    def _dispatchable(self, url):
        if not valid_destination(url):
            return Response(url, 0, {}, b'', error='unsafe_destination')
        if is_banned(url) or matches_domain(host_of(url), self.extra_banned):
            return Response(url, 0, {}, b'', error='banned')
        return None

    def _robots_for(self, url):
        p = urllib.parse.urlsplit(url)
        base = f'{p.scheme}://{p.netloc.lower()}'
        robots_url = base + '/robots.txt'
        with self._host_lock(host_of(url)) as locked:
            if not locked:
                return None, Response(url, 0, {}, b'', error='block_end')
            if blocked := self.blocked_response(url):
                return None, blocked
            if rp := self._cached_robots(base):
                return rp, None
            r = self._dispatch(robots_url, {}, robots=True)
            if not _redirect(r):
                return self._cache_robots(base, r), r
        # Robots redirects are policy retrieval. Lock every hop as for content redirects.
        for _ in range(5):
            try:
                robots_url = to_uri(urllib.parse.urljoin(robots_url, r.headers['location']))
            except ValueError:
                return None, Response(robots_url, 0, {}, b'', error='unsafe_destination')
            if invalid := self._dispatchable(robots_url):
                return None, invalid
            with self._host_lock(host_of(robots_url)) as locked:
                if not locked:
                    return None, Response(url, 0, {}, b'', error='block_end')
                r = self._dispatch(robots_url, {}, robots=True)
            if not _redirect(r):
                with self._host_lock(host_of(url)) as locked:
                    return self._cache_robots(base, r) if locked else None, r
        return None, r

    def robots_info(self, url):
        url = to_uri(url)
        if self._dispatchable(url):
            return {'allowed': False, 'crawl_delay': None}
        if not self.korean_press(host_of(url)):
            return {'allowed': True, 'crawl_delay': None}
        self._ensure_run()
        rp, r = self._robots_for(url)
        info = {'allowed': rp is not None and rp.can_fetch(UA, url), 'crawl_delay': _crawl_delay(rp) if rp else None}
        if rp is None and r is not None and r.error == 'block_end':
            info['unknown'] = True   # not attempted (deadline/host lock); still disallowed for fetching
        return info

    def site_maps(self, url):
        url = to_uri(url)
        if self._dispatchable(url) or not self.korean_press(host_of(url)):
            return []
        self._ensure_run()
        rp, _ = self._robots_for(url)
        return list(rp.site_maps() or []) if rp else []

    def get(self, url, extra_headers=None, *, method='GET', diagnostic=False, timeout=None, before_send=None, restrict_host=None):
        if method != 'GET':
            return Response(url, 0, {}, b'', error='unsupported_method')
        self._ensure_run()
        headers = {'Accept-Language': 'ko,en;q=0.8', 'Accept': '*/*', **(extra_headers or {})}
        for _ in range(6):
            try:
                url = to_uri(url)
            except ValueError:
                return Response(url, 0, {}, b'', error='unsafe_destination')
            if invalid := self._dispatchable(url):
                return invalid
            host = host_of(url)
            if restrict_host and host != restrict_host:
                return Response(url, 0, {}, b'', error='redirect_outside_host')
            delay = None
            if self.korean_press(host):
                rp, rr = self._robots_for(url)
                if rr is not None and (rr.blocked or rr.error == 'block_end'):
                    return rr
                if rp is None:
                    return Response(url, 0, {}, b'', error='robots_unavailable')
                if not rp.can_fetch(UA, url):
                    return Response(url, 0, {}, b'', error='robots')
                delay = _crawl_delay(rp) or 0
            with self._host_lock(host) as locked:
                if not locked:
                    return Response(url, 0, {}, b'', error='block_end')
                r = self._dispatch(url, headers, delay=delay, diagnostic=diagnostic, timeout=timeout, before_send=before_send)
            if not _redirect(r):
                return r
            try:
                next_url = urllib.parse.urljoin(url, r.headers['location'])
            except ValueError:
                return Response(url, 0, {}, b'', error='unsafe_destination')
            if urllib.parse.urlsplit(next_url)[:2] != urllib.parse.urlsplit(url)[:2]:
                headers = {k: v for k, v in headers.items() if k.lower() in ('accept', 'accept-language')}
            url = next_url
        return Response(url, 0, {}, b'', error='redirect_limit')


def _redirect(r):
    return not r.blocked and r.status in REDIRECTS and bool(r.headers.get('location'))


def _crawl_delay(rp):
    return rp.crawl_delay(UA) or rp.crawl_delay('*')


def _parse_robots(status, body):
    rp = urllib.robotparser.RobotFileParser()
    if status == 200:
        rp.parse(body.splitlines())
    else:
        rp.allow_all = True
    return rp


def retry_after(value, now):
    """Honor a valid Retry-After, clamped to a 7-day ceiling so one hostile header cannot retire a host forever."""
    if not value:
        return None
    try:
        if re.fullmatch(r'[0-9]+', value.strip()):
            delay = float(value)
            return now + min(delay, RETRY_AFTER_CAP) if math.isfinite(delay) else None
        date = email.utils.parsedate_to_datetime(value)
        return min(date.replace(tzinfo=date.tzinfo or dt.timezone.utc).timestamp(), now + RETRY_AFTER_CAP)
    except (ValueError, OverflowError, AttributeError):
        return None


def looks_like_challenge(r):
    if r.headers.get('cf-mitigated', '').lower() == 'challenge':
        return True
    if r.ctype not in ('text/html', 'application/xhtml+xml', 'text/plain', '', 'application/json'):
        return False
    text = r.body[:6000].decode('utf-8', 'replace').lower()
    heading = ' '.join(re.findall(r'<(?:title|h1)\b[^>]*>([^<]+)', text))
    return (any(h in heading for h in CHALLENGE_HINTS) or
            (any(h in text for h in CHALLENGE_HINTS) and len(re.sub(r'<[^>]+>', '', text)) < 1500))


def login_path(url):
    try:
        path = urllib.parse.urlsplit(url).path.lower()
    except ValueError:
        return False
    return bool(re.search(r'/(login|signin|sign-in|auth)(?:/|$)', path))


def looks_like_login(final_url, html, text_chars):
    if login_path(final_url) and text_chars < 1500:
        return True
    low = html.lower()
    has_pw = bool(re.search(r'<input\b[^>]*\btype\s*=\s*["\']?password\b', low))
    return has_pw and any(hint in low for hint in LOGIN_HINTS) and text_chars < 1500


def block_reason(r):
    if r.status == 401 or (r.status in REDIRECTS and login_path(r.headers.get('location', ''))):
        return 'login_page'
    if r.status in (403, 429, 451) or (500 <= r.status < 600 and 'retry-after' in r.headers):
        return f'http_{r.status}'
    if looks_like_challenge(r):
        return 'challenge_page'
    if r.status == 200 and r.ctype in ('text/html', 'application/xhtml+xml', 'text/plain', '', 'application/json'):
        text = r.text()[:20000]
        if r.ctype == 'application/json' and re.search(r'"(?:login_required|authentication_required)"', text):
            return 'login_page'
        if looks_like_login(r.final_url, text, len(re.sub(r'<[^>]+>', '', text))):
            return 'login_page'
    return None
