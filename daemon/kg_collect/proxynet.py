"""Public-web egress through the zecbot SOCKS5 tunnel (owner order 2026-11-11 10:33 KST).

Every public web request of the collector leaves through ``socks5h://127.0.0.1:1080`` (exit IP 49.50.131.207). There is NO direct
fallback: when the tunnel is down a request fails with ``proxy_down`` and the caller skips. Loopback hosts (local fixture servers in
tests) are not public web and stay local. DeepSeek / Tavily / GitHub / Cloudflare clients do not use this module (they stay direct),
and Instagram never uses it. Stdlib only (no browser, no extra packages): survives box resets.

Size guard (zecbot egress is billed over 20 GB/month): only text-like content types are read, Content-Length over the cap is not
downloaded, bodies are read with a hard byte cap, and gzip is requested so the wire stays small."""
import gzip
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
MAX_BODY = int(os.environ.get('KG_WEB_MAX_BODY', 1_500_000))           # decoded-size cap per response
MAX_WIRE = int(os.environ.get('KG_WEB_MAX_WIRE', 2_000_000))           # declared Content-Length cap (bytes on the wire)
TEXT_TYPES = ('text/html', 'application/xhtml+xml', 'application/rss+xml', 'application/atom+xml', 'application/xml', 'text/xml', 'text/plain', 'application/json', 'application/ld+json')
SKIP_EXT = ('.jpg', '.jpeg', '.png', '.gif', '.webp', '.svg', '.bmp', '.ico', '.heic', '.avif', '.mp4', '.mov', '.avi', '.webm', '.m4v', '.mkv', '.mp3', '.wav',
            '.m4a', '.zip', '.gz', '.rar', '.7z', '.tar', '.exe', '.dmg', '.apk', '.pdf', '.hwp', '.doc', '.docx', '.xls', '.xlsx', '.ppt', '.pptx', '.woff', '.woff2', '.ttf')


class ProxyDown(Exception):
    """The SOCKS tunnel is unreachable or refused the CONNECT. Never answered by a direct connection."""


def proxy_url() -> str:
    return os.environ.get('KG_WEB_PROXY') or DEFAULT_PROXY


def _proxy_addr(url: str | None = None) -> tuple[str, int]:
    p = urllib.parse.urlsplit(url or proxy_url())
    if p.scheme not in ('socks5h', 'socks5'):
        raise ValueError(f'unsupported proxy scheme: {p.scheme}')
    return p.hostname or '127.0.0.1', p.port or 1080


def is_loopback(host: str) -> bool:
    h = (host or '').strip('[]').lower()
    if h in ('localhost', ''):
        return True
    try:
        return ipaddress.ip_address(h).is_loopback
    except ValueError:
        return False


def _recv_exact(s, n):
    buf = b''
    while len(buf) < n:
        chunk = s.recv(n - len(buf))
        if not chunk:
            raise ProxyDown('socks_eof')
        buf += chunk
    return buf


def socks5_connect(host: str, port: int, timeout: float = 20, proxy: str | None = None) -> socket.socket:
    """RFC 1928 CONNECT with the HOSTNAME sent to the proxy (socks5h: DNS resolves on zecbot, no local DNS leak)."""
    ph, pp = _proxy_addr(proxy)
    try:
        s = socket.create_connection((ph, pp), timeout=timeout)
    except OSError as e:
        raise ProxyDown(f'connect:{type(e).__name__}') from None
    try:
        s.sendall(b'\x05\x01\x00')
        if _recv_exact(s, 2) != b'\x05\x00':
            raise ProxyDown('socks_auth_rejected')
        hb = host.encode('idna') if not host.isascii() else host.encode()
        s.sendall(b'\x05\x01\x00\x03' + bytes([len(hb)]) + hb + struct.pack('>H', port))
        rep = _recv_exact(s, 4)
        if rep[1] != 0:
            s.close()
            raise OSError(f'socks_connect_failed:{rep[1]}')          # target unreachable via proxy: a normal network error, NOT proxy down
        atyp = rep[3]
        _recv_exact(s, {1: 4, 4: 16}.get(atyp) or _recv_exact(s, 1)[0])
        _recv_exact(s, 2)
        return s
    except (ProxyDown, OSError):
        s.close()
        raise
    except Exception as e:  # noqa: BLE001
        s.close()
        raise ProxyDown(f'socks_protocol:{type(e).__name__}') from None


class _SocksHTTP(http.client.HTTPConnection):
    def connect(self):
        self.sock = socks5_connect(self.host, self.port, self.timeout)


class _SocksHTTPS(http.client.HTTPSConnection):
    def connect(self):
        raw = socks5_connect(self.host, self.port, self.timeout)
        self.sock = self._context.wrap_socket(raw, server_hostname=self.host)


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


def request(url: str, headers: dict | None = None, timeout: float = 20, method: str = 'GET', max_body: int | None = None, text_only: bool = True,
            traffic: Traffic = TRAFFIC):
    """-> (status, headers{lower}, body, error). Raises ProxyDown when the tunnel is unusable. Never follows redirects (caller decides)."""
    p = urllib.parse.urlsplit(url)
    host, port = p.hostname or '', p.port or (443 if p.scheme == 'https' else 80)
    path = (p.path or '/') + (('?' + p.query) if p.query else '')
    if p.scheme not in ('http', 'https'):
        return 0, {}, b'', 'bad_scheme'
    if text_only and p.path.lower().endswith(SKIP_EXT):
        traffic.add(skipped_type=1)
        return 0, {}, b'', 'skipped_binary_ext'
    hdrs = {'Accept-Encoding': 'gzip', 'Connection': 'close', **(headers or {})}
    local = is_loopback(host)
    if local:
        cls = http.client.HTTPSConnection if p.scheme == 'https' else http.client.HTTPConnection
        conn = cls(host, port, timeout=timeout, **({'context': _CTX} if p.scheme == 'https' else {}))
    else:
        conn = (_SocksHTTPS(host, port, timeout=timeout, context=_CTX) if p.scheme == 'https' else _SocksHTTP(host, port, timeout=timeout))
    cap = max_body or MAX_BODY
    try:
        conn.request(method, path, headers=hdrs)
        out_bytes = len(method) + len(path) + sum(len(k) + len(v) + 4 for k, v in hdrs.items()) + 40
        r = conn.getresponse()
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
        raw = raw[:wire_cap]
        traffic.add(requests=1, bytes_in=head_bytes + len(raw), bytes_out=out_bytes, skipped_large=1 if truncated else 0)
        body = _decode(raw, rh.get('content-encoding'), cap)
        rh.pop('content-encoding', None)
        return r.status, rh, body, ('truncated' if truncated else None)
    except ProxyDown:
        traffic.add(proxy_errors=1)
        raise
    except (OSError, http.client.HTTPException, ssl.SSLError) as e:
        return 0, {}, b'', type(e).__name__
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


def exit_ip(timeout: float = 15) -> str | None:
    """The public IP the target web sees for our proxied requests (via a public IP echo). None if unknown / proxy down."""
    try:
        st, _, body, _ = request(IP_ECHO_URL, {'User-Agent': 'KitchenGenomeBot/1.0'}, timeout=timeout, text_only=True)
    except ProxyDown:
        return None
    if st != 200:
        return None
    try:
        return json.loads(body.decode()).get('ip')
    except (ValueError, AttributeError):
        return None


def egress_ok(expected: str | None = None) -> tuple[bool, str]:
    """Gate for every crawl run: tunnel answers AND the exit IP is zecbot's. (False, reason) means skip the run; never go direct."""
    if not proxy_alive():
        return False, 'proxy_down'
    ip = exit_ip()
    want = expected or os.environ.get('KG_WEB_EXPECTED_IP') or EXPECTED_EXIT_IP
    if ip is None:
        return False, 'exit_ip_unknown'
    if ip != want:
        return False, f'exit_ip_mismatch:{ip}'
    return True, ip
