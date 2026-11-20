"""HTTP transport with zecbot preferred and a box route for confirmed tunnel failures.

The caller holds the shared host guard through transport selection and response classification.
Response and traffic bounds apply on both routes.
"""
import http.client
import ipaddress
import json
import os
import socket
import ssl
import struct
import threading
import urllib.parse
import zlib

DEFAULT_PROXY = 'socks5h://127.0.0.1:1080'
EXPECTED_EXIT_IP = '49.50.131.207'
IP_ECHO_URL = 'https://api.ipify.org?format=json'
IP_ECHO_URLS = (IP_ECHO_URL, 'https://api64.ipify.org?format=json', 'https://icanhazip.com')
MAX_BODY = int(os.environ.get('KG_WEB_MAX_BODY', 1_500_000))           # decoded-size cap per response
MAX_WIRE = int(os.environ.get('KG_WEB_MAX_WIRE', 2_000_000))           # declared Content-Length cap (bytes on the wire)
TEXT_TYPES = ('text/html', 'application/xhtml+xml', 'application/rss+xml', 'application/atom+xml', 'application/xml', 'text/xml', 'text/plain', 'application/json', 'application/ld+json')
SKIP_EXT = ('.jpg', '.jpeg', '.png', '.gif', '.webp', '.svg', '.bmp', '.ico', '.heic', '.avif', '.mp4', '.mov', '.avi', '.webm', '.m4v', '.mkv', '.mp3', '.wav',
            '.m4a', '.zip', '.gz', '.rar', '.7z', '.tar', '.exe', '.dmg', '.apk', '.pdf', '.hwp', '.doc', '.docx', '.xls', '.xlsx', '.ppt', '.pptx', '.woff', '.woff2', '.ttf')


class ProxyDown(Exception):
    """Local tunnel connection or SOCKS greeting failure before contacting the target."""


def proxy_url() -> str:
    return os.environ.get('KG_WEB_PROXY') or DEFAULT_PROXY


def _proxy_addr(url: str | None = None) -> tuple[str, int]:
    p = urllib.parse.urlsplit(url or proxy_url())
    if p.scheme not in ('socks5h', 'socks5'):
        raise ValueError(f'unsupported proxy scheme: {p.scheme}')
    return p.hostname or '127.0.0.1', p.port or 1080


def _recv_exact(s, n):
    buf = b''
    while len(buf) < n:
        chunk = s.recv(n - len(buf))
        if not chunk:
            raise ProxyDown('socks_eof')
        buf += chunk
    return buf


def socks5_connect(host: str, port: int, timeout: float = 20, proxy: str | None = None) -> socket.socket:
    """RFC 1928 CONNECT to an address already resolved and checked by the caller."""
    ph, pp = _proxy_addr(proxy)
    try:
        s = socket.create_connection((ph, pp), timeout=timeout)
    except OSError as e:
        raise ProxyDown(f'connect:{type(e).__name__}') from None
    try:
        try:
            s.sendall(b'\x05\x01\x00')
            if _recv_exact(s, 2) != b'\x05\x00':
                raise ProxyDown('socks_auth_rejected')
        except OSError:
            raise ProxyDown('socks_greeting_failed') from None
        try:
            address = ipaddress.ip_address(host)
            target = bytes([1 if address.version == 4 else 4]) + address.packed
        except ValueError:
            # Remote resolution: used only when the local resolver hands out fake-IP egress addresses.
            name = host.encode('idna')
            target = b'\x03' + bytes([len(name)]) + name
        s.sendall(b'\x05\x01\x00' + target + struct.pack('>H', port))
        rep = _recv_exact(s, 4)
        if rep[1] != 0:
            raise OSError('socks_target_unreachable')
        try:
            atyp = rep[3]
            _recv_exact(s, {1: 4, 4: 16}.get(atyp) or _recv_exact(s, 1)[0])
            _recv_exact(s, 2)
        except ProxyDown:
            raise OSError('socks_connect_incomplete') from None
        return s
    except BaseException:
        s.close()
        raise


def _connection(host, port, address, secure, timeout, via_proxy):
    cls = http.client.HTTPSConnection if secure else http.client.HTTPConnection
    conn = cls(host, port, timeout=timeout, **({'context': _CTX} if secure else {}))

    def dial(_target, timeout, _source):
        if via_proxy:
            return socks5_connect(host if fake_ip(address) else address, port, timeout)
        return socket.create_connection((address, port), timeout)

    conn._create_connection = dial
    return conn


# Some sandboxes run a transparent egress proxy whose resolver answers every name with an
# address from the RFC 2544 benchmark block (198.18.0.0/15). Those answers are the box's
# real path to the public internet, not a local destination. Accept them only when they
# come from the resolver for a hostname; IP literals in URLs are still rejected.
FAKE_IP_NET = ipaddress.ip_network('198.18.0.0/15')


def fake_ip(addr):
    try:
        return ipaddress.ip_address(addr) in FAKE_IP_NET
    except ValueError:
        return False


def _public_ip(host, port):
    from .net import public_address

    addresses = [row[4][0] for row in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)]
    if addresses and all(fake_ip(a) for a in addresses):
        try:
            ipaddress.ip_address(host)
        except ValueError:
            return addresses[0]
        return None
    if not addresses or any(not public_address(ipaddress.ip_address(addr)) for addr in addresses):
        return None
    return next((addr for addr in addresses if ':' not in addr), addresses[0])


_CTX = ssl.create_default_context()


class Traffic:
    """Thread-safe byte/request counters of one process (the heavy crawler writes them to data_store/crawl_traffic.jsonl)."""

    def __init__(self):
        self.lock = threading.Lock()
        self.requests = self.bytes_in = self.bytes_out = self.not_modified = self.skipped_large = self.skipped_type = self.proxy_errors = 0

    def add(self, **kw):
        with self.lock:
            for k, v in kw.items():
                setattr(self, k, getattr(self, k) + v)

    def snapshot(self) -> dict:
        with self.lock:
            return {k: getattr(self, k) for k in ('requests', 'bytes_in', 'bytes_out', 'not_modified', 'skipped_large', 'skipped_type', 'proxy_errors')}


TRAFFIC = Traffic()


def _decode(body: bytes, enc: str, cap: int = MAX_BODY) -> bytes:
    enc = (enc or '').lower()
    if enc == 'gzip':
        d = zlib.decompressobj(16 + zlib.MAX_WBITS)
    elif enc == 'deflate':
        d = zlib.decompressobj()
    else:
        return body[:cap]
    try:
        return d.decompress(body, cap)
    except zlib.error:
        return b''


def _send(p, host, port, address, headers, timeout, max_body, text_only, traffic, via_proxy, method):
    path = (p.path or '/') + (('?' + p.query) if p.query else '')
    hdrs = {'Accept-Encoding': 'gzip', 'Connection': 'close', **headers}
    conn = _connection(host, port, address, p.scheme == 'https', timeout, via_proxy)
    status, rh = 0, {}
    out_bytes = head_bytes = 0
    cap = max_body or MAX_BODY
    try:
        conn.request(method, path, headers=hdrs)
        out_bytes = len(method) + len(path) + sum(len(k) + len(v) + 4 for k, v in hdrs.items()) + 40
        r = conn.getresponse()
        status = r.status
        rh = {k.lower(): v for k, v in r.getheaders()}
        head_bytes = sum(len(k) + len(v) + 4 for k, v in r.getheaders()) + 20
        ctype = (rh.get('content-type') or '').split(';')[0].strip().lower()
        clen = rh.get('content-length')
        if r.status == 304:
            traffic.add(requests=1, bytes_in=head_bytes, bytes_out=out_bytes, not_modified=1)
            return 304, rh, b'', None
        if text_only and r.status == 200 and ctype and not (ctype in TEXT_TYPES or ctype.startswith('text/')):
            traffic.add(requests=1, bytes_in=head_bytes, bytes_out=out_bytes, skipped_type=1)
            return r.status, rh, b'', f'skipped_type:{ctype}'
        if clen and clen.isdigit() and int(clen) > MAX_WIRE:
            traffic.add(requests=1, bytes_in=head_bytes, bytes_out=out_bytes, skipped_large=1)
            return r.status, rh, b'', f'skipped_large:{clen}'
        wire_cap = min(MAX_WIRE, cap * 2) if rh.get('content-encoding') else cap
        raw = r.read(wire_cap + 1) if method != 'HEAD' else b''
        truncated = len(raw) > wire_cap
        received = len(raw)
        body = _decode(raw[:wire_cap], rh.get('content-encoding'), cap)
        traffic.add(requests=1, bytes_in=head_bytes + received, bytes_out=out_bytes, skipped_large=int(truncated))
        rh.pop('content-encoding', None)
        return r.status, rh, body, ('truncated' if truncated else None)
    except ProxyDown:
        traffic.add(proxy_errors=1)
        raise
    except (OSError, http.client.HTTPException, ValueError) as e:
        if out_bytes:
            traffic.add(requests=1, bytes_in=head_bytes, bytes_out=out_bytes)
        return status, rh, b'', type(e).__name__
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass


def proxy_alive(timeout: float = 5) -> bool:
    ph, pp = _proxy_addr()
    try:
        s = socket.create_connection((ph, pp), timeout=timeout)
    except OSError:
        return False
    try:
        s.sendall(b'\x05\x01\x00')
        return _recv_exact(s, 2) == b'\x05\x00'
    except Exception:  # noqa: BLE001
        return False
    finally:
        s.close()


class Route:
    """Once box fallback is confirmed, use it for the rest of this invocation."""

    def __init__(self):
        self.lock = threading.Lock()
        self.name = None
        self.reason = 'no_dispatch'

    def select(self, *, failed=False):
        with self.lock:
            if self.name is None or (failed and self.name == 'zecbot'):
                if proxy_alive():
                    self.name = 'zecbot'
                    if self.reason == 'no_dispatch':
                        self.reason = 'tunnel_healthy'
                else:
                    self.name, self.reason = 'box', 'tunnel_down'
            return self.name

    def request(self, url, headers=None, timeout=20, method='GET', max_body=None, text_only=True, traffic=TRAFFIC,
                on_traffic=None):
        """Perform a single HTTP hop, with one fallback on a confirmed local failure."""
        from .net import UA, valid_destination

        if method not in ('GET', 'HEAD'):
            return 0, {}, b'', 'unsupported_method'
        if not valid_destination(url):
            return 0, {}, b'', 'unsafe_destination'
        p = urllib.parse.urlsplit(url)
        host, port = p.hostname.rstrip('.'), p.port or (443 if p.scheme == 'https' else 80)
        if text_only and p.path.lower().endswith(SKIP_EXT):
            traffic.add(skipped_type=1)
            return 0, {}, b'', 'skipped_binary_ext'
        try:
            address = _public_ip(host, port)
        except OSError:
            return 0, {}, b'', 'dns_error'
        if address is None:
            return 0, {}, b'', 'unsafe_destination'
        headers = {k: v for k, v in (headers or {}).items() if k.lower() not in ('user-agent', 'host')}
        headers['User-Agent'] = UA
        def send(route):
            local = Traffic()
            try:
                return _send(p, host, port, address, headers, timeout, max_body, text_only, local,
                             via_proxy=route == 'zecbot', method=method)
            finally:
                snapshot = local.snapshot()
                traffic.add(**snapshot)
                if on_traffic and snapshot['requests']:
                    on_traffic(route, snapshot)

        name = self.select()
        try:
            return send(name)
        except ProxyDown:
            if name == 'zecbot' and self.select(failed=True) == 'box':
                return send('box')
            return 0, {}, b'', 'proxy_failure'

    def egress_ok(self, expected=None, probe=None):
        if self.select() == 'box':
            return True, self.reason
        if probe is None:
            from .net import PoliteNet, Response

            def transport(url, headers, timeout):
                status, headers, body, err = self.request(url, headers, timeout=timeout)
                return Response(url, status, headers, body, error=err)
            net = PoliteNet(transport=transport)
            probe = lambda url, timeout: net.get(url, diagnostic=True, timeout=timeout)
        ip = exit_ip(probe)
        want = expected or os.environ.get('KG_WEB_EXPECTED_IP') or EXPECTED_EXIT_IP
        with self.lock:
            if self.name == 'zecbot':
                self.reason = 'exit_ip_unknown' if ip is None else ('exit_ip_match' if ip == want else 'exit_ip_mismatch')
            return True, self.reason


DEFAULT_ROUTE = Route()
request = DEFAULT_ROUTE.request
egress_ok = DEFAULT_ROUTE.egress_ok


def exit_ip(probe, timeout=15):
    """One advisory read from an IP echo service. The address is never logged."""
    for url in IP_ECHO_URLS:
        response = probe(url, timeout)
        if response.blocked:
            return None
        if response.status != 200:
            continue
        try:
            ip = response.text().strip() if url == IP_ECHO_URLS[-1] else json.loads(response.text()).get('ip')
            if isinstance(ip, str):
                ipaddress.ip_address(ip)
                return ip
        except (ValueError, AttributeError):
            continue
    return None


def record_traffic(conn, day, route, traffic):
    with conn:
        conn.execute("""INSERT INTO web_traffic_day(day,route,requests,bytes_in,bytes_out) VALUES (?,?,?,?,?)
            ON CONFLICT(day,route) DO UPDATE SET requests=requests+excluded.requests,
            bytes_in=bytes_in+excluded.bytes_in,bytes_out=bytes_out+excluded.bytes_out""",
            (day, route, traffic['requests'], traffic['bytes_in'], traffic['bytes_out']))
