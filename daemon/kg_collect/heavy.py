"""Heavy continuous web collector (owner order 2026-11-11 10:33 KST).

Runs in time blocks (00:00-03:00, 11:00-14:00 KST; 22:00-24:00 only when the backlog is large), resumes a SQLite frontier across
runs and hard-stops at the end of its block. HTTP only (no browser), <= 6 requests in flight, one request per domain at a time with a
5 s gap (or the longer robots Crawl-delay), robots.txt obeyed, a domain that answers 403/429/503/challenge is left alone (no bypass).

Egress: every request goes through the zecbot SOCKS tunnel (proxynet). The run starts only when the tunnel answers AND the exit IP is
zecbot's; if the tunnel is lost mid-run the run stops. There is no direct fallback.

Output: pages that mention a registered active chef/restaurant are handed to the 07:00 pipeline as ordinary fetch_record rows
(raw store + fetch_priority score). The 07:00 extract stage reads the highest scores first, so the fixed DeepSeek budget (cap 20)
goes to the most promising pages. This module never calls DeepSeek, never commits, merges or deploys."""
import argparse
import ipaddress
import concurrent.futures as cf
import datetime as dt
import json
import os
import re
import resource
import sqlite3
import sys
import time
import urllib.parse
import urllib.robotparser
import uuid
from zoneinfo import ZoneInfo

import yaml

from kg_daemon.db import DEFAULT_DB, ROOT, connect, migrate, stable_id, utcnow

from . import EXTRACTOR_VERSION, FETCHER_VERSION, proxynet
from .discover import FOOD_CUE, item_relevance
from .fetch import MIN_TEXT_CHARS, canonicalize_url, parse_feed
from .htmlx import parse_html
from .net import BANNED, CHALLENGE_HINTS, TRAP, UA, host_of, looks_like_login, to_uri
from .rawstore import RawStore
from .targets import Gazetteer, eligibility

KST = ZoneInfo('Asia/Seoul')
SOURCES_PATH = os.path.join(ROOT, 'sources', 'heavy_sources.yaml')
HEAVY_FETCHER = FETCHER_VERSION + '+heavy1'
REVISIT_KINDS = ('search', 'feed', 'guide')
HARD = {'max_concurrency': 6, 'per_domain_delay_sec': 5, 'max_depth': 3}      # ceilings the yaml cannot loosen
CEIL = {'daily_candidates': 5000, 'daily_content': 1500, 'daily_requests': 8000, 'daily_bytes': 500_000_000, 'monthly_bytes': 15_000_000_000}
MAX_CRAWL_DELAY = 60                                                         # a robots Crawl-delay longer than this: host is skipped

SCHEMA = """
CREATE TABLE IF NOT EXISTS frontier(
  url TEXT PRIMARY KEY, host TEXT NOT NULL, kind TEXT NOT NULL, depth INTEGER NOT NULL DEFAULT 0, score REAL NOT NULL DEFAULT 0.5,
  query TEXT, target_id TEXT, parent TEXT, state TEXT NOT NULL DEFAULT 'queued', attempts INTEGER NOT NULL DEFAULT 0,
  etag TEXT, last_modified TEXT, content_hash TEXT, revisit_hours REAL, next_due REAL NOT NULL DEFAULT 0,
  added_day TEXT, added_at REAL, fetched_at REAL, status INTEGER, error TEXT);
CREATE INDEX IF NOT EXISTS frontier_due ON frontier(state, next_due, score);
CREATE INDEX IF NOT EXISTS frontier_host ON frontier(host);
CREATE TABLE IF NOT EXISTS host_state(host TEXT PRIMARY KEY, paused_until REAL, reason TEXT, robots TEXT, robots_status INTEGER, robots_at REAL);
CREATE TABLE IF NOT EXISTS daily(day TEXT PRIMARY KEY, requests INTEGER NOT NULL DEFAULT 0, bytes_in INTEGER NOT NULL DEFAULT 0,
  bytes_out INTEGER NOT NULL DEFAULT 0, candidates INTEGER NOT NULL DEFAULT 0, content INTEGER NOT NULL DEFAULT 0,
  items INTEGER NOT NULL DEFAULT 0, not_modified INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS run(run_id TEXT PRIMARY KEY, started REAL, ended REAL, block TEXT, outcome TEXT, stats TEXT);
"""


def kst_now(clock=time.time) -> dt.datetime:
    return dt.datetime.fromtimestamp(clock(), KST)


def load_cfg(path=SOURCES_PATH) -> dict:
    cfg = yaml.safe_load(open(path, encoding='utf-8'))
    caps = cfg['caps']
    caps['max_concurrency'] = max(1, min(HARD['max_concurrency'], int(caps.get('max_concurrency', 6))))
    caps['per_domain_delay_sec'] = max(HARD['per_domain_delay_sec'], float(caps.get('per_domain_delay_sec', 5)))
    caps['max_depth'] = min(HARD['max_depth'], int(caps.get('max_depth', 3)))
    for k, v in CEIL.items():
        caps[k] = min(v, int(caps.get(k, v)))
    return cfg


def deadline_for(until: str, clock=time.time) -> float:
    """'02:58' -> epoch of the NEXT such KST wall time within 4 hours of now; a block whose end already passed returns 0 (do not run)."""
    now = kst_now(clock)
    hh, mm = map(int, until.split(':'))
    end = now.replace(hour=hh % 24, minute=mm, second=0, microsecond=0)
    if end <= now:
        end2 = end + dt.timedelta(days=1)
        return end2.timestamp() if (end2 - now) <= dt.timedelta(hours=4) else 0.0
    return end.timestamp() if (end - now) <= dt.timedelta(hours=4) else 0.0


def _private_host(h: str) -> bool:
    h = (h or '').split(':')[0].strip('[]')
    if h in ('localhost', '') or h.endswith('.local') or h.endswith('.internal'):
        return True
    try:
        ip = ipaddress.ip_address(h)
    except ValueError:
        return False
    return ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast


def excluded(url: str, cfg) -> bool:
    try:
        h = host_of(url)
        urllib.parse.urlsplit(url).port
    except ValueError:
        return True
    if _private_host(h):
        return True                                   # never fetch box-local / private addresses (redirect or link)
    ex = tuple(cfg.get('exclude_hosts') or ())
    if any(h == b or h.endswith('.' + b) for b in ex) or any(h == b or h.endswith('.' + b) or b in h for b in BANNED):
        return True
    low = url.lower()
    path = urllib.parse.urlsplit(low).path
    return path.endswith(proxynet.SKIP_EXT) or any(t in low for t in TRAP)


class Terminated(Exception):
    """SIGTERM (cron timeout): unwind the loop so the run and traffic records are still written."""


class HeavyCrawler:
    def __init__(self, queue_path, service_conn, cfg=None, transport=None, clock=time.time, sleep=time.sleep, egress=None, data_dir=None,
                 traffic=None, log_path=None):
        self.cfg = cfg or load_cfg()
        self.caps = self.cfg['caps']
        self.q = sqlite3.connect(queue_path, timeout=30, check_same_thread=False)
        self.q.row_factory = sqlite3.Row
        self.q.executescript(SCHEMA)
        self.svc = service_conn
        self.clock, self.sleep = clock, sleep
        self.traffic = traffic or proxynet.Traffic()
        self.transport = transport or (lambda url, headers, timeout: proxynet.request(url, headers, timeout=timeout, traffic=self.traffic))
        self.egress = egress or proxynet.egress_ok
        self.data_dir = data_dir or os.path.dirname(os.path.abspath(queue_path))
        self.raw = RawStore(os.path.join(self.data_dir, 'raw'))
        self.log_path = log_path or os.path.join(self.data_dir, 'crawl_traffic.jsonl')
        self.gaz = Gazetteer(service_conn)
        self.robots: dict = {}
        self.stats = {'requests': 0, 'robots_requests': 0, 'ok_pages': 0, 'items_seen': 0, 'candidates_new': 0, 'content_handoff': 0,
                      'unchanged': 0, 'not_modified': 0, 'blocked_hosts': 0, 'robots_denied': 0, 'errors': 0, 'skipped_size_or_type': 0}

    # ── day / month counters ──
    def day(self) -> str:
        return kst_now(self.clock).date().isoformat()

    def daily(self) -> dict:
        d = self.day()
        self.q.execute('INSERT OR IGNORE INTO daily(day) VALUES (?)', (d,))
        return dict(self.q.execute('SELECT * FROM daily WHERE day=?', (d,)).fetchone())

    def bump(self, **kw):
        d = self.day()
        self.q.execute('INSERT OR IGNORE INTO daily(day) VALUES (?)', (d,))
        self.q.execute('UPDATE daily SET ' + ', '.join(f'{k}={k}+?' for k in kw) + ' WHERE day=?', (*kw.values(), d))

    def flush_traffic(self):
        snap = self.traffic.snapshot()
        prev = getattr(self, '_flushed', {'bytes_in': 0, 'bytes_out': 0})
        di, do = snap['bytes_in'] - prev['bytes_in'], snap['bytes_out'] - prev['bytes_out']
        if di or do:
            self.bump(bytes_in=di, bytes_out=do)
        self._flushed = {'bytes_in': snap['bytes_in'], 'bytes_out': snap['bytes_out']}

    def month_bytes(self) -> int:
        return self.q.execute("SELECT COALESCE(SUM(bytes_in+bytes_out),0) FROM daily WHERE substr(day,1,7)=?", (self.day()[:7],)).fetchone()[0]

    def cap_reason(self) -> str | None:
        d = self.daily()
        if d['requests'] >= self.caps['daily_requests']:
            return 'daily_request_cap'
        if d['bytes_in'] + d['bytes_out'] >= self.caps['daily_bytes']:
            return 'daily_bytes_cap'
        if self.month_bytes() >= self.caps['monthly_bytes']:
            return 'monthly_bytes_cap'
        return None

    # ── frontier ──
    def add(self, url, kind, depth=0, score=0.5, query=None, target_id=None, parent=None, revisit_hours=None, count=True) -> bool:
        try:
            url = to_uri(url)
        except ValueError:
            return False
        if not url.startswith(('http://', 'https://')) or excluded(url, self.cfg) or depth > self.caps['max_depth']:
            return False
        if count and self.daily()['candidates'] >= self.caps['daily_candidates']:
            return False
        cur = self.q.execute("""INSERT OR IGNORE INTO frontier(url,host,kind,depth,score,query,target_id,parent,revisit_hours,next_due,added_day,added_at)
                                VALUES (?,?,?,?,?,?,?,?,?,0,?,?)""", (url, host_of(url), kind, depth, round(score, 3), query, target_id, parent, revisit_hours,
                                                                     self.day(), self.clock()))
        if cur.rowcount and count:
            self.bump(candidates=1)
            self.stats['candidates_new'] += 1
        return bool(cur.rowcount)

    def queries(self) -> list[dict]:
        mods, out = self.cfg['query_modifiers'], []
        for t in self.svc.execute("SELECT target_id, kind, name_ko, origin FROM target WHERE kind IN ('chef','restaurant') AND status='active' AND name_ko IS NOT NULL ORDER BY origin DESC, name_ko"):
            for i, m in enumerate(mods.get(t['kind'], [])):
                base = 0.75 if t['kind'] == 'chef' else 0.7
                out.append({'q': f"{t['name_ko']} {m}", 'target_id': t['target_id'], 'score': base - 0.04 * i - (0.05 if t['origin'] != 'seed' else 0)})
        for g in mods.get('general', []):
            out.append({'q': g, 'target_id': None, 'score': 0.6})
        out.sort(key=lambda x: -x['score'])
        return out[:int(self.caps['max_queries'])]

    def seed(self) -> dict:
        n = {'queries': 0, 'feeds': 0, 'guides': 0, 'official': 0}
        for s in self.cfg.get('search', []):
            for x in self.queries():
                n['queries'] += self.add(s['endpoint'].format(q=urllib.parse.quote(x['q'])), 'search', 0, x['score'], query=x['q'], target_id=x['target_id'],
                                         revisit_hours=s.get('revisit_hours', 24), count=False)
        for f in self.cfg.get('feeds', []):
            n['feeds'] += self.add(f['url'], 'feed', 0, 0.8, revisit_hours=f.get('revisit_hours', 6), count=False)
        for g in self.cfg.get('guides', []):
            n['guides'] += self.add(g['url'], 'guide', 0, 0.7, revisit_hours=g.get('revisit_hours', 168), count=False)
        try:
            rows = self.svc.execute('SELECT url, target_id FROM official_site WHERE verified=1').fetchall()
        except sqlite3.Error:
            rows = []
        for r in rows:
            n['official'] += self.add(r['url'], 'guide', 0, 0.85, target_id=r['target_id'], revisit_hours=168, count=False)
        self.q.commit()
        return n

    def backlog(self) -> int:
        return self.q.execute("SELECT COUNT(*) FROM frontier WHERE state='queued' AND next_due<=?", (self.clock(),)).fetchone()[0]

    # ── hosts ──
    def host_paused(self, host) -> bool:
        r = self.q.execute('SELECT paused_until FROM host_state WHERE host=?', (host,)).fetchone()
        return bool(r and r['paused_until'] and r['paused_until'] > self.clock())

    def pause_host(self, host, reason):
        until = self.clock() + 86400 * float(self.caps.get('block_pause_days', 7))
        self.q.execute('INSERT INTO host_state(host,paused_until,reason) VALUES (?,?,?) ON CONFLICT(host) DO UPDATE SET paused_until=excluded.paused_until, reason=excluded.reason',
                       (host, until, reason))
        self.stats['blocked_hosts'] += 1

    def _robots_cached(self, base):
        if base in self.robots:
            return self.robots[base]
        r = self.q.execute('SELECT robots, robots_status, robots_at FROM host_state WHERE host=?', (base,)).fetchone()
        if r and r['robots_at'] and self.clock() - r['robots_at'] < 86400:
            self.robots[base] = self._parse_robots(r['robots_status'], r['robots'] or '')
            return self.robots[base]
        return None

    @staticmethod
    def _parse_robots(status, text):
        rp = urllib.robotparser.RobotFileParser()
        if status == 200:
            rp.parse(text.splitlines())
        elif status in (401, 403, 429) or status == 0 or (status or 0) >= 500:
            rp.disallow_all = True                  # forbidden, or unreachable/5xx (RFC 9309 2.3.1.4): do not crawl now
        else:
            rp.allow_all = True                     # missing robots.txt (404/410 etc. 4xx) = allowed (RFC 9309)
        return rp

    # ── worker (thread): robots (if needed) + one GET; no DB access here ──
    def work(self, task) -> dict:
        url, base = task['url'], task['base']
        out = {'task': task, 'robots_fetch': None}
        try:
            rp = task.get('rp')
            if rp is None:
                st, _, body, err = self.transport(base + '/robots.txt', {'User-Agent': UA}, 20)
                txt = body.decode('utf-8', 'ignore') if st == 200 else ''
                out['robots_fetch'] = (st, txt)
                if st == 0 and err in ('proxy_down',):
                    raise proxynet.ProxyDown(err)
                rp = self._parse_robots(st, txt)
                if st in (403, 429, 503):
                    out['blocked'] = f'robots_{st}'
                    return out
                if st == 0 or st >= 500:
                    out['robots_unavailable'] = st
                    return out
                d = max(self.caps['per_domain_delay_sec'], rp.crawl_delay(UA) or rp.crawl_delay('*') or 0)
                if d > MAX_CRAWL_DELAY or self.clock() + d + 20 > task.get('stop_at', float('inf')):
                    out['deferred'] = 'crawl_delay' if d > MAX_CRAWL_DELAY else 'block_end'
                    return out
                self.sleep(d)
            out['delay'] = max(self.caps['per_domain_delay_sec'], rp.crawl_delay(UA) or rp.crawl_delay('*') or 0)
            if out['delay'] > MAX_CRAWL_DELAY:
                out['deferred'] = 'crawl_delay'
                return out
            if not rp.can_fetch(UA, url):
                out['robots_denied'] = True
                return out
            h = {'User-Agent': UA, 'Accept': 'text/html,application/xhtml+xml,application/rss+xml,application/xml;q=0.9,*/*;q=0.5', 'Accept-Language': 'ko-KR,ko;q=0.9'}
            if task.get('etag'):
                h['If-None-Match'] = task['etag']
            if task.get('last_modified'):
                h['If-Modified-Since'] = task['last_modified']
            out['resp'] = self.transport(url, h, 20)
        except proxynet.ProxyDown as e:
            out['proxy_down'] = str(e)
        except Exception as e:  # noqa: BLE001
            out['resp'] = (0, {}, b'', type(e).__name__)
        return out

    # ── result handling (main thread, single DB writer) ──
    def _finish(self, url, kind, revisit_hours, **cols):
        if kind in REVISIT_KINDS and cols.get('state', 'done') in ('done',):
            cols['state'], cols['next_due'] = 'queued', self.clock() + 3600 * float(revisit_hours or 24)
        cols['fetched_at'] = self.clock()
        self.q.execute('UPDATE frontier SET ' + ', '.join(f'{k}=?' for k in cols) + ' WHERE url=?', (*cols.values(), url))

    def handle(self, res) -> str | None:
        t = res['task']
        url, host, base = t['url'], t['host'], t['base']
        if res.get('robots_fetch') is not None:
            st, txt = res['robots_fetch']
            self.stats['robots_requests'] += 1
            self.stats['requests'] += 1
            self.bump(requests=1)
            if st and st < 500:                     # transient robots failures are not cached
                self.q.execute("""INSERT INTO host_state(host,robots,robots_status,robots_at) VALUES (?,?,?,?) ON CONFLICT(host) DO UPDATE SET robots=excluded.robots,
                                  robots_status=excluded.robots_status, robots_at=excluded.robots_at""", (base, txt[:200_000], st, self.clock()))
                self.robots[base] = self._parse_robots(st, txt)
        if res.get('proxy_down'):
            return 'proxy_lost'
        if res.get('blocked'):
            self.pause_host(host, res['blocked'])
            self._finish(url, t['kind'], t['revisit_hours'], state='queued', next_due=self.clock() + 86400 * float(self.caps.get('block_pause_days', 7)), error=res['blocked'])
            return None
        if res.get('robots_unavailable') is not None or res.get('deferred'):
            why = res.get('deferred') or f"robots_unavailable:{res['robots_unavailable']}"
            if why == 'block_end':
                return None                          # stays queued for the next block
            att = t['attempts'] + 1
            self._finish(url, 'article', None, state='queued' if att < 3 else 'failed', attempts=att, next_due=self.clock() + 3600 * att, error=why)
            return None
        if res.get('robots_denied'):
            self.stats['robots_denied'] += 1
            self._finish(url, 'article', None, state='skipped', error='robots')
            return None
        st, hd, body, err = res['resp']
        self.stats['requests'] += 1
        self.bump(requests=1)
        if err and str(err).startswith(('skipped_', 'truncated')):
            self.stats['skipped_size_or_type'] += 1
            self._finish(url, 'article', None, state='skipped', status=st, error=err)
            return None
        if st == 304:
            self.stats['not_modified'] += 1
            self.bump(not_modified=1)
            self._finish(url, t['kind'], t['revisit_hours'], state='done', status=304, error=None)
            return None
        if st in (403, 429, 503) or (st == 200 and self._challenge(hd, body)):
            self.pause_host(host, f'blocked:{st}')
            self._finish(url, t['kind'], t['revisit_hours'], state='queued' if t['kind'] in REVISIT_KINDS else 'blocked',
                         next_due=self.clock() + 86400 * float(self.caps.get('block_pause_days', 7)), status=st, error=f'blocked:{st}')
            return None
        if st in (301, 302, 303, 307, 308) and hd.get('location'):
            loc = urllib.parse.urljoin(url, hd['location'])
            self.add(loc, t['kind'], t['depth'], t['score'], query=t['query'], target_id=t['target_id'], parent=url, revisit_hours=t['revisit_hours'], count=t['kind'] not in REVISIT_KINDS)
            self._finish(url, 'article', None, state='done', status=st, error='redirect')
            return None
        if st != 200:
            self.stats['errors'] += 1
            att = t['attempts'] + 1
            gone = st in (404, 410) or att >= 3
            self._finish(url, 'article' if gone else t['kind'], t['revisit_hours'], state='failed' if gone else 'queued', attempts=att,
                         next_due=self.clock() + 3600 * att, status=st, error=str(err or st))
            return None
        self.stats['ok_pages'] += 1
        etag, lm = hd.get('etag'), hd.get('last-modified')
        ctype = (hd.get('content-type') or '').split(';')[0].strip().lower()
        h = RawStore.sha(body)
        if t['content_hash'] == h:
            self.stats['unchanged'] += 1
            self._finish(url, t['kind'], t['revisit_hours'], state='done', status=200, etag=etag, last_modified=lm)
            return None
        if 'xml' in ctype or 'rss' in ctype or 'atom' in ctype or body.lstrip()[:5] in (b'<?xml', b'<rss ', b'<feed'):
            self.process_feed(t, body)
        elif ctype.startswith('text/html') or ctype in ('application/xhtml+xml', ''):
            self.process_html(t, body, hd)
        self._finish(url, t['kind'], t['revisit_hours'], state='done', status=200, etag=etag, last_modified=lm, content_hash=h, error=None)
        return None

    @staticmethod
    def _challenge(hd, body) -> bool:
        if not (hd.get('content-type') or '').startswith('text/html'):
            return False
        t = body[:6000].decode('utf-8', 'ignore').lower()
        return any(x in t for x in CHALLENGE_HINTS) and len(re.sub(r'<[^>]+>', '', t)) < 1500

    def process_feed(self, t, body):
        items = parse_feed(body.decode('utf-8', 'ignore') if isinstance(body, bytes) else body, t['url'])
        cutoff = (kst_now(self.clock).date() - dt.timedelta(days=int(self.caps['lookback_days']))).isoformat()
        self.stats['items_seen'] += len(items)
        self.bump(items=len(items))
        for it in items[:40]:
            if it.get('date') and it['date'] < cutoff:
                continue
            rel = item_relevance(self.gaz, it.get('title', ''), it.get('summary', ''), it['url'])
            if t['target_id'] and t['query'] and t['query'].split()[0] in f"{it.get('title', '')} {it.get('summary', '')}":
                rel = min(1.0, rel + 0.1)
            if rel < self.caps['relevance_floor']:
                continue
            self.add(it['url'], 'article', t['depth'] + 1, rel, query=t['query'], target_id=t['target_id'], parent=t['url'])

    def process_html(self, t, body, hd):
        html = body.decode('utf-8', 'ignore')
        m = re.search(r'charset=([\w-]+)', hd.get('content-type') or '', re.I) or re.search(r'<meta[^>]+charset=["\']?([\w-]+)', html[:4096], re.I)
        if m and m.group(1).lower() not in ('utf-8', 'utf8'):
            try:
                html = body.decode(m.group(1))
            except (LookupError, UnicodeDecodeError):
                pass
        doc = parse_html(html, t['url'])
        text = doc['text'] or ''
        if looks_like_login(t['url'], html, len(text)):
            return
        if len(text) >= MIN_TEXT_CHARS:
            el = eligibility(self.gaz, f"{doc.get('title') or ''}\n{text}")
            if el['target_ids']:
                self.handoff(t, html, doc, el)
        if t['depth'] + 1 > self.caps['max_depth'] or not doc['links']:
            return
        cands = []
        for u, anchor, rel in doc['links']:
            if excluded(u, self.cfg) or 'nofollow' in (rel or []):
                continue
            s = 0.0
            if anchor and self.gaz.find(anchor):
                s = 0.8
            elif anchor and FOOD_CUE.search(anchor):
                s = 0.45 + (0.05 if host_of(u) == t['host'] else 0)
            if s >= self.caps['relevance_floor']:
                cands.append((s, u))
        for s, u in sorted(cands, reverse=True)[:int(self.caps['max_links_per_page'])]:
            self.add(u, 'article', t['depth'] + 1, s, target_id=t['target_id'], parent=t['url'])

    def handoff(self, t, html, doc, el):
        """Ordinary fetch_record (+raw) for the 07:00 extract stage, with a priority score for the DeepSeek order."""
        if self.daily()['content'] >= self.caps['daily_content']:
            return
        url = t['url']
        canon = canonicalize_url(url)
        raw_ref, h = self.raw.put(host_of(url), html.encode('utf-8'), 'html')
        if self.svc.execute("SELECT 1 FROM fetch_record WHERE canonical_url=? AND content_hash=? AND outcome='ok' LIMIT 1", (canon, h)).fetchone():
            self.stats['unchanged'] += 1
            return
        fetched = utcnow()
        fid = stable_id('fch', url, fetched, 'html', 'heavy', uuid.uuid4().hex[:6])
        text = doc['text']
        text_ref = self.raw.put_text(host_of(url), text)
        n = len(el['target_ids'])
        recent = bool(doc.get('published_at') and doc['published_at'] >= (kst_now(self.clock).date() - dt.timedelta(days=45)).isoformat())
        score = round(min(1.0, 0.45 + 0.15 * min(n, 3) + (0.1 if re.search(r'셰프|쉐프|chef', text[:3000], re.I) else 0) + (0.1 if recent else 0)), 3)
        self.svc.execute("""INSERT INTO fetch_record(fetch_id,job_id,url,canonical_url,host,method,http_status,outcome,content_type,bytes,content_hash,raw_ref,text_ref,text_chars,
                            page_no,parent_fetch_id,fetcher_version,extractor_version,published_at,fetched_at,error) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                         (fid, None, url, canon, host_of(url), 'html', 200, 'ok', 'text/html', len(html), h, raw_ref, text_ref, len(text), None, None, HEAVY_FETCHER,
                          EXTRACTOR_VERSION, doc.get('published_at'), fetched, None))
        self.svc.execute('INSERT OR REPLACE INTO fetch_priority(fetch_id,score,source,created_at) VALUES (?,?,?,?)', (fid, score, 'heavy', fetched))
        self.svc.commit()
        self.bump(content=1)
        self.stats['content_handoff'] += 1

    # ── scheduling loop ──
    def pick(self, busy: set, limit: int) -> list[dict]:
        rows = self.q.execute("""SELECT * FROM frontier WHERE state='queued' AND next_due<=? ORDER BY score DESC, depth ASC, added_at ASC LIMIT 400""", (self.clock(),)).fetchall()
        out, hosts = [], set(busy)
        for r in rows:
            if len(out) >= limit:
                break
            h = r['host']
            if h in hosts or self.next_ok.get(h, 0) > self.clock():
                continue
            if self.host_paused(h):
                continue
            p = urllib.parse.urlsplit(r['url'])
            base = f'{p.scheme}://{p.netloc}'
            task = dict(r)
            task['base'] = base
            task['rp'] = self._robots_cached(base)
            task['stop_at'] = self.stop_at
            hosts.add(h)
            out.append(task)
        return out

    def run(self, deadline: float, block: str = 'manual', max_requests: int | None = None, optional: bool = False) -> dict:
        run_id = 'hv_' + uuid.uuid4().hex[:10]
        started = self.clock()
        self.started = started
        rep = {'run_id': run_id, 'block': block, 'started_kst': kst_now(self.clock).isoformat(timespec='seconds')}
        if deadline <= started + 60:
            return self._end(rep, 'skipped', 'block_over')
        ok, info = self.egress()
        rep['egress'] = info
        if not ok:
            return self._end(rep, 'skipped', info)
        cap = self.cap_reason()
        if cap:
            return self._end(rep, 'skipped', cap)
        rep['seeded'] = self.seed()
        if optional and self.backlog() < int(self.caps.get('backlog_for_optional_block', 800)):
            return self._end(rep, 'skipped', 'backlog_small')
        self.next_ok: dict = {}
        self.stop_at = stop_at = deadline - 25                       # leave room for the 20 s request timeout before the hard stop
        halt, inflight = None, {}
        try:
            last_ip_check = self.clock()
            with cf.ThreadPoolExecutor(max_workers=self.caps['max_concurrency']) as ex:
                while True:
                    now = self.clock()
                    self.flush_traffic()
                    if halt is None:
                        if now >= stop_at:
                            halt = 'deadline'
                        elif max_requests is not None and self.stats['requests'] >= max_requests:
                            halt = 'max_requests'
                        elif (c := self.cap_reason()):
                            halt = c
                        elif now - last_ip_check > 900:
                            last_ip_check = now
                            ok, info = self.egress()
                            if not ok:
                                halt = info
                    if halt is None:
                        free = self.caps['max_concurrency'] - len(inflight)
                        if free > 0:
                            busy = {t['host'] for t in inflight.values()}
                            for task in self.pick(busy, free):
                                inflight[ex.submit(self.work, task)] = task
                    if not inflight:
                        if halt:
                            break
                        if self.backlog() == 0:
                            halt = 'queue_empty'
                            break
                        if not any(v > self.clock() for v in self.next_ok.values()):
                            halt = 'nothing_ready'          # only paused / robots-cached-denied hosts left
                            break
                        self.sleep(1)
                        continue
                    done, _ = cf.wait(list(inflight), timeout=1, return_when=cf.FIRST_COMPLETED)
                    for f in done:
                        task = inflight.pop(f)
                        res = f.result()
                        self.next_ok[task['host']] = self.clock() + res.get('delay', self.caps['per_domain_delay_sec'])
                        lost = self.handle(res)
                        if lost:
                            halt = lost                     # tunnel gone: start nothing new, let in-flight requests fail on their own
                    self.q.commit()
        except Exception as e:  # noqa: BLE001
            halt = f'crash:{type(e).__name__}'
            import traceback
            traceback.print_exc(file=sys.stderr)
        reason = halt or 'idle'
        if halt and halt.startswith('crash:'):
            return self._end(rep, 'halted', halt)
        return self._end(rep, 'stopped' if reason in ('deadline', 'queue_empty', 'nothing_ready', 'max_requests', 'idle') else 'halted', reason)

    def _end(self, rep, outcome, reason) -> dict:
        self.flush_traffic()
        snap = self.traffic.snapshot()
        self.q.commit()
        d = self.daily()
        rep.update({'outcome': outcome, 'reason': reason, 'ended_kst': kst_now(self.clock).isoformat(timespec='seconds'),
                    'stats': dict(self.stats), 'traffic': snap, 'day_totals': d, 'month_bytes': self.month_bytes(),
                    'queue': {r['state']: r['n'] for r in self.q.execute('SELECT state, COUNT(*) n FROM frontier GROUP BY state')},
                    'max_rss_mb': round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024, 1)})
        self.q.execute('INSERT OR REPLACE INTO run(run_id,started,ended,block,outcome,stats) VALUES (?,?,?,?,?,?)',
                       (rep['run_id'], getattr(self, 'started', None), self.clock(), rep['block'], f'{outcome}:{reason}', json.dumps(rep, ensure_ascii=False)))
        self.q.commit()
        line = {k: rep.get(k) for k in ('run_id', 'block', 'started_kst', 'ended_kst', 'outcome', 'reason', 'egress', 'traffic', 'stats', 'day_totals', 'month_bytes', 'max_rss_mb')}
        line['day'] = self.day()
        os.makedirs(os.path.dirname(os.path.abspath(self.log_path)), exist_ok=True)
        with open(self.log_path, 'a', encoding='utf-8') as f:
            f.write(json.dumps(line, ensure_ascii=False) + '\n')
        return rep


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog='kg-heavy')
    ap.add_argument('--db', default=os.environ.get('KG_SERVICE_DB') or DEFAULT_DB)
    ap.add_argument('--queue')
    sub = ap.add_subparsers(dest='cmd', required=True)
    r = sub.add_parser('run')
    r.add_argument('--until', required=True, help='KST HH:MM hard stop (end of the block)')
    r.add_argument('--block', default='manual')
    r.add_argument('--optional', action='store_true', help='run only when the backlog is large (22:00 block)')
    r.add_argument('--max-requests', type=int)
    sub.add_parser('egress')
    sub.add_parser('status')
    a = ap.parse_args(argv)
    if a.cmd == 'egress':
        ok, info = proxynet.egress_ok()
        print(f"EGRESS={'ok' if ok else 'skip'} {'ip' if ok else 'reason'}={info}")
        return 0 if ok else 3
    conn = connect(a.db)
    migrate(conn)
    queue = a.queue or os.path.join(os.path.dirname(os.path.abspath(a.db)), 'heavy_queue.db')
    hc = HeavyCrawler(queue, conn)
    if a.cmd == 'status':
        print(json.dumps({'backlog_due': hc.backlog(), 'today': hc.daily(), 'month_bytes': hc.month_bytes(),
                          'queue': {x['state']: x['n'] for x in hc.q.execute('SELECT state, COUNT(*) n FROM frontier GROUP BY state')}}, ensure_ascii=False))
        return 0
    import signal

    def _term(*_):
        raise Terminated()
    signal.signal(signal.SIGTERM, _term)
    rep = hc.run(deadline_for(a.until), block=a.block, max_requests=a.max_requests, optional=a.optional)
    s, tr = rep.get('stats') or {}, rep.get('traffic') or {}
    print(f"HEAVY={rep['outcome']} reason={rep['reason']} block={a.block} egress={rep.get('egress')} requests={s.get('requests', 0)} items={s.get('items_seen', 0)} "
          f"candidates={s.get('candidates_new', 0)} content={s.get('content_handoff', 0)} bytes_in={tr.get('bytes_in', 0)} bytes_out={tr.get('bytes_out', 0)} rss_mb={rep.get('max_rss_mb')}")
    return 0


if __name__ == '__main__':
    sys.exit(main())
