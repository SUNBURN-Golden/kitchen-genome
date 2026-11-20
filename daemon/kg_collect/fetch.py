"""HTTP fetcher for APIs, feeds, HTML and JSON-LD.
Handles pagination, tables, PDFs, image OCR. Every attempt writes a fetch_record (raw ref, hash, fetched_at, extractor version).
A 200 that is a login page, a challenge page or an empty body is a FAILURE, recorded as such."""
import dataclasses
import datetime as dt
import email.utils
import json
import os
import re
import sys
import urllib.parse
import xml.etree.ElementTree as ET

from kg_daemon.db import ROOT, stable_id, utcnow

from . import EXTRACTOR_VERSION, FETCHER_VERSION
from .docs_ocr import ocr_image, pdf_text
from .htmlx import is_js_shell, parse_date_text, parse_html
from .net import CFG, PoliteNet, Response, host_of, matches_domain
from .rawstore import RawStore

sys.path.insert(0, os.path.join(ROOT, 'crawler'))
try:
    from vault import canonicalize_url
except Exception:  # pragma: no cover
    def canonicalize_url(u):
        return (u or '').strip()

MIN_TEXT_CHARS = 120
BROWSER_HOSTS = tuple(CFG['allowed_browser_hosts'])


@dataclasses.dataclass
class Doc:
    fetch_id: str
    url: str
    canonical_url: str
    host: str
    method: str
    outcome: str
    text: str = ''
    title: str | None = None
    published_at: str | None = None
    links: list = dataclasses.field(default_factory=list)       # (url, anchor, rel)
    items: list = dataclasses.field(default_factory=list)       # feed/sitemap items {url,title,date,summary}
    images: list = dataclasses.field(default_factory=list)
    next_url: str | None = None
    content_hash: str | None = None
    ocr_conf: float | None = None
    error: str | None = None
    page_no: int = 1
    unchanged: bool = False

    @property
    def ok(self):
        return self.outcome == 'ok'


def unreadable_js_shell(url, html, text, browser_hint=False):
    loading = text.strip().lower() in ('loading...', 'loading…', '로딩 중')
    return len(text) < MIN_TEXT_CHARS and (
        is_js_shell(html, len(text)) or loading and (browser_hint or matches_domain(host_of(url), BROWSER_HOSTS))
    )


def _rfc_date(s):
    try:
        return email.utils.parsedate_to_datetime(s).date().isoformat()
    except Exception:
        return parse_date_text(s)


def parse_feed(xml_text: str, base_url: str) -> list[dict]:
    """RSS 2.0 / Atom / sitemap urlset+sitemapindex -> items. Bing/Google redirect wrappers (url=) are unwrapped."""
    try:
        root = ET.fromstring(xml_text.encode('utf-8') if isinstance(xml_text, str) else xml_text)
    except ET.ParseError:
        return []
    tag = lambda e: e.tag.split('}')[-1]  # noqa: E731
    items = []
    if tag(root) in ('urlset', 'sitemapindex'):
        for e in root:
            d = {tag(c): (c.text or '').strip() for c in e}
            if d.get('loc'):
                items.append({'url': d['loc'], 'title': '', 'date': parse_date_text(d.get('lastmod')), 'summary': '', 'kind': 'sitemap_index' if tag(root) == 'sitemapindex' else 'sitemap_url'})
        return items
    for e in root.iter():
        if tag(e) not in ('item', 'entry'):
            continue
        d = {}
        for c in e:
            d[tag(c)] = (c.text or '').strip() if c.text else c.attrib.get('href', '')
            if tag(c) == 'link' and not c.text and c.attrib.get('href'):
                d['link'] = c.attrib['href']
        link = d.get('link') or d.get('guid') or ''
        q = urllib.parse.parse_qs(urllib.parse.urlparse(link).query)
        if 'url' in q:
            link = q['url'][0]
        summ = re.sub(r'<[^>]+>', ' ', d.get('description') or d.get('summary') or d.get('content') or '')
        items.append({'url': urllib.parse.urljoin(base_url, link), 'title': re.sub(r'\s+', ' ', d.get('title', '')),
                      'date': _rfc_date(d.get('pubDate') or d.get('published') or d.get('updated') or ''),
                      'summary': re.sub(r'\s+', ' ', summ).strip()[:400], 'kind': 'item'})
    return items


class Fetcher:
    def __init__(self, conn, net: PoliteNet, raw: RawStore, charge=None, browser=None, clock=utcnow, ocr=True):
        self.conn, self.net, self.raw, self.clock = conn, net, raw, clock
        self.charge = charge or (lambda kind, units=1.0: None)
        self.ocr = ocr

    # ── persistence ──
    def _record(self, job_id, url, method, outcome, *, r: Response | None = None, text='', raw_ref=None, content_hash=None, published=None,
                parent=None, page_no=None, error=None) -> str:
        fetched = self.clock()
        fid = stable_id('fch', url, fetched, method, str(page_no), str(self.conn.execute('SELECT COUNT(*) FROM fetch_record').fetchone()[0]))
        text_ref = self.raw.put_text(host_of(url), text) if text else None
        self.conn.execute("""INSERT INTO fetch_record(fetch_id,job_id,url,canonical_url,host,method,http_status,outcome,content_type,bytes,content_hash,raw_ref,text_ref,text_chars,page_no,parent_fetch_id,fetcher_version,extractor_version,published_at,fetched_at,error)
                             VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                          (fid, job_id, url, canonicalize_url(url), host_of(url), method, r.status if r else None, outcome, r.ctype if r else None,
                           len(r.body) if r else None, content_hash, raw_ref, text_ref, len(text) if text else 0, page_no, parent, FETCHER_VERSION, EXTRACTOR_VERSION, published, fetched, error))
        self.conn.commit()
        return fid

    def _fail(self, job_id, url, method, outcome, r=None, error=None, **kw) -> Doc:
        fid = self._record(job_id, url, method, outcome, r=r, error=error or outcome, **kw)
        return Doc(fid, url, canonicalize_url(url), host_of(url), method, outcome, error=error or outcome)

    def _unchanged(self, url, content_hash) -> bool:
        return self.conn.execute("SELECT 1 FROM fetch_record WHERE canonical_url=? AND content_hash=? AND outcome='ok' LIMIT 1", (canonicalize_url(url), content_hash)).fetchone() is not None

    # ── main entry ──
    def fetch(self, url: str, job_id: str | None = None, allow_browser: bool | None = None, parent: str | None = None, page_no: int | None = None,
              follow_next: bool = False, before_send=None) -> list[Doc]:
        docs = []
        seen_hashes, seen_urls = set(), set()
        cur, n = url, 0
        while cur and canonicalize_url(cur) not in seen_urls:
            seen_urls.add(canonicalize_url(cur))
            n += 1
            d = self._fetch_one(cur, job_id, allow_browser, parent, n if follow_next else page_no, before_send)
            docs.append(d)
            if not d.ok or not follow_next or not d.next_url or d.content_hash in seen_hashes:
                break
            seen_hashes.add(d.content_hash)
            nxt = d.next_url
            if host_of(nxt) != host_of(url) or canonicalize_url(nxt) == canonicalize_url(cur):
                break
            cur, parent = nxt, parent or d.fetch_id
        return docs

    def _fetch_one(self, url, job_id, allow_browser, parent, page_no, before_send) -> Doc:
        before = self.net.request_count
        r = self.net.get(url, before_send=before_send) if before_send else self.net.get(url)
        if self.net.request_count > before:
            self.charge('http', self.net.request_count - before)
        if r.blocked:
            kind = 'fail_login_page' if r.error == 'login_page' else 'fail_blocked'
            return self._fail(job_id, r.final_url, 'html', kind, r, error=r.error, parent=parent, page_no=page_no)
        if r.status == 0:
            kind = {'banned': 'fail_banned', 'robots': 'fail_robots'}.get(r.error, 'fail_network')
            return self._fail(job_id, url, 'html', kind, r, error=r.error, parent=parent, page_no=page_no)
        if r.status >= 400:
            return self._fail(job_id, url, 'html', 'fail_http', r, error=f'http_{r.status}', parent=parent, page_no=page_no)
        if not r.body:
            return self._fail(job_id, url, 'html', 'fail_empty', r, error='empty_body', parent=parent, page_no=page_no)
        url = r.final_url
        ct = r.ctype
        body_text = r.text() if ct.startswith(('text/', 'application/xml', 'application/json', 'application/rss', 'application/atom')) or ct == '' else ''
        # ── feed / sitemap / API json ──
        if ct in ('application/rss+xml', 'application/atom+xml', 'application/xml', 'text/xml') or re.match(r'\s*<\?xml|\s*<(rss|feed|urlset|sitemapindex)', body_text[:200]):
            items = parse_feed(r.body, url)
            if not items:
                return self._fail(job_id, url, 'feed', 'fail_empty', r, error='feed_without_items', parent=parent, page_no=page_no)
            is_sm = items[0].get('kind', '').startswith('sitemap')
            text = '\n'.join(f"{i['title']} — {i['summary']} ({i['url']})" if not is_sm else i['url'] for i in items)
            raw_ref, h = self.raw.put(host_of(url), r.body, 'xml')
            fid = self._record(job_id, url, 'sitemap' if is_sm else 'feed', 'ok', r=r, text=text, raw_ref=raw_ref, content_hash=h, parent=parent, page_no=page_no)
            return Doc(fid, url, canonicalize_url(url), host_of(url), 'sitemap' if is_sm else 'feed', 'ok', text=text, items=items, content_hash=h, unchanged=False)
        if ct == 'application/json':
            try:
                data = json.loads(body_text)
            except ValueError:
                return self._fail(job_id, url, 'api', 'fail_empty', r, error='bad_json', parent=parent, page_no=page_no)
            raw_ref, h = self.raw.put(host_of(url), r.body, 'json')
            text = json.dumps(data, ensure_ascii=False)[:200000]
            fid = self._record(job_id, url, 'api', 'ok', r=r, text=text, raw_ref=raw_ref, content_hash=h, parent=parent, page_no=page_no)
            return Doc(fid, url, canonicalize_url(url), host_of(url), 'api', 'ok', text=text, content_hash=h)
        # ── PDF ──
        if ct == 'application/pdf' or r.body[:5] == b'%PDF-':
            raw_ref, h = self.raw.put(host_of(url), r.body, 'pdf')
            res = pdf_text(r.body)
            if res['method'] == 'ocr':
                self.charge('ocr_page', max(1, res['pages']))
            if len(re.sub(r'\s+', '', res['text'])) < 40:
                return self._fail(job_id, url, 'pdf', 'fail_empty', r, error='pdf_no_text', raw_ref=raw_ref, content_hash=h, parent=parent, page_no=page_no)
            fid = self._record(job_id, url, 'ocr' if res['method'] == 'ocr' else 'pdf', 'ok', r=r, text=res['text'], raw_ref=raw_ref, content_hash=h, parent=parent, page_no=page_no)
            return Doc(fid, url, canonicalize_url(url), host_of(url), 'ocr' if res['method'] == 'ocr' else 'pdf', 'ok', text=res['text'], content_hash=h, ocr_conf=res['ocr_conf'])
        # ── image (menu / poster) ──
        if ct.startswith('image/'):
            raw_ref, h = self.raw.put(host_of(url), r.body, ct.split('/')[1].replace('jpeg', 'jpg')[:4])
            if not self.ocr:
                return self._fail(job_id, url, 'ocr', 'fail_type', r, error='ocr_disabled', raw_ref=raw_ref, content_hash=h, parent=parent, page_no=page_no)
            self.charge('ocr_page', 1.0)
            res = ocr_image(r.body)
            if len(re.sub(r'\s+', '', res['text'])) < 20 or (res['ocr_conf'] is not None and res['ocr_conf'] < 35):
                return self._fail(job_id, url, 'ocr', 'fail_empty', r, error=f"ocr_unreadable conf={res['ocr_conf']}", raw_ref=raw_ref, content_hash=h, parent=parent, page_no=page_no)
            fid = self._record(job_id, url, 'ocr', 'ok', r=r, text=res['text'], raw_ref=raw_ref, content_hash=h, parent=parent, page_no=page_no)
            return Doc(fid, url, canonicalize_url(url), host_of(url), 'ocr', 'ok', text=res['text'], content_hash=h, ocr_conf=res['ocr_conf'])
        if not (ct.startswith('text/html') or ct in ('application/xhtml+xml', '')):
            return self._fail(job_id, url, 'html', 'fail_type', r, error=f'unsupported_content_type:{ct}', parent=parent, page_no=page_no)
        # ── HTML (+ JSON-LD, tables) ──
        html = body_text
        doc = parse_html(html, r.final_url or url)
        method = 'jsonld' if doc['jsonld_types'] and len(doc['text']) < MIN_TEXT_CHARS else 'html'
        text_chars = len(doc['text'])
        if unreadable_js_shell(url, html, doc['text'], allow_browser):
            return self._fail(job_id, url, 'html', 'fail_empty', r, error='js_renderer_unavailable', parent=parent, page_no=page_no)
        if text_chars < MIN_TEXT_CHARS:
            return self._fail(job_id, url, 'html', 'fail_empty', r, error=f'thin_or_empty_body:{text_chars}', parent=parent, page_no=page_no)
        # Publisher attribution uses the successful destination after the normal transport redirect checks.
        raw_ref, h = self.raw.put(host_of(url), r.body, 'html')
        unchanged = self._unchanged(url, h)
        fid = self._record(job_id, url, method, 'ok', r=r, text=doc['text'], raw_ref=raw_ref, content_hash=h, published=doc['published_at'], parent=parent, page_no=page_no)
        return Doc(fid, url, canonicalize_url(url), host_of(url), method, 'ok', text=doc['text'], title=doc['title'], published_at=doc['published_at'], links=doc['links'],
                   images=doc['images'], next_url=doc['next_url'], content_hash=h, unchanged=unchanged, page_no=page_no or 1)
