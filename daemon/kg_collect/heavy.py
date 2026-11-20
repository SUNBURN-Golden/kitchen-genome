"""Resumable HTTP collection within the existing heavy-crawl time blocks.

Korean press uses robots.txt and at least five seconds between requests. All hosts
are serialized and access refusals stop the host for the run. Egress uses zecbot
when healthy and falls back to the box on a confirmed local tunnel failure.

The Michelin frontier shares the service DB with the 07:00 collector. Eligible
Korean article snapshots use the existing extractor and Qwen-first guarded model
chain. The block writes private collection state for subsequent integration."""
import argparse
import contextlib
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
import uuid
from zoneinfo import ZoneInfo

import yaml

from kg_daemon.db import DEFAULT_DB, ROOT, connect, migrate, stable_id, utcnow
from kg_daemon.runner import CostMeter

from . import EXTRACTOR_VERSION, FETCHER_VERSION, global_queue, michelin, proxynet
from .cycle import UNIT_COSTS, extract_roster
from .discover import FOOD_CUE, item_relevance
from .fetch import MIN_TEXT_CHARS, canonicalize_url, parse_feed, unreadable_js_shell
from .htmlx import parse_html
from .guard import Guard, threshold
from kg_daemon.claims import queue_review
from .net import PoliteNet, Response, host_of, is_banned, matches_domain, to_uri, valid_destination
from .rawstore import RawStore
from .targets import Gazetteer, eligibility

KST = ZoneInfo('Asia/Seoul')
SOURCES_PATH = os.path.join(ROOT, 'sources', 'heavy_sources.yaml')
HEAVY_FETCHER = FETCHER_VERSION + '+heavy1'
REVISIT_KINDS = ('search', 'feed', 'guide')
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
    caps['max_concurrency'] = max(1, int(caps.get('max_concurrency', 6)))
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


def excluded(url: str, cfg) -> bool:
    if not valid_destination(url) or is_banned(url) or matches_domain(host_of(url), cfg.get('exclude_hosts', ())):
        return True
    low = url.lower()
    path = urllib.parse.urlsplit(low).path
    return path.endswith(proxynet.SKIP_EXT) or michelin.trapped(url)


class HeavyCrawler:
    def __init__(self, queue_path, service_conn, cfg=None, transport=None, clock=time.time, sleep=time.sleep, egress=None, data_dir=None,
                 traffic=None, log_path=None, roster_config=None):
        self.cfg = cfg or load_cfg()
        self.roster_config = roster_config
        self.terminating = False
        self.svc_busy_timeout = 5000
        service_conn.execute(f'PRAGMA busy_timeout={self.svc_busy_timeout}')
        self.caps = self.cfg['caps']
        self.q = sqlite3.connect(queue_path, timeout=30, check_same_thread=False)
        self.q.row_factory = sqlite3.Row
        self.q.execute('PRAGMA journal_mode=WAL')     # status readers never wait on the running block
        self.q.executescript(SCHEMA)
        with self.q:
            if 'canonical_url' not in {r['name'] for r in self.q.execute('PRAGMA table_info(frontier)')}:
                self.q.execute('ALTER TABLE frontier ADD COLUMN canonical_url TEXT')
                self.q.executemany('UPDATE frontier SET canonical_url=? WHERE url=?',
                                   ((canonicalize_url(r[0]), r[0]) for r in self.q.execute('SELECT url FROM frontier')))
            self.q.execute('CREATE INDEX IF NOT EXISTS frontier_canonical ON frontier(canonical_url)')
        self.svc = service_conn
        self.clock, self.sleep = clock, sleep
        self.traffic = traffic or proxynet.Traffic()
        self.transport = transport
        self.egress = egress
        self.service_db = service_conn.execute('PRAGMA database_list').fetchone()[2]
        self.data_dir = data_dir or os.path.dirname(os.path.abspath(queue_path))
        self.raw = RawStore(os.path.join(self.data_dir, 'raw'))
        self.log_path = log_path or os.path.join(self.data_dir, 'crawl_traffic.jsonl')
        self.gaz = Gazetteer(service_conn)
        self.stats = {'requests': 0, 'robots_requests': 0, 'ok_pages': 0, 'items_seen': 0, 'candidates_new': 0, 'content_handoff': 0,
                      'unchanged': 0, 'not_modified': 0, 'blocked_hosts': 0, 'robots_denied': 0, 'errors': 0, 'skipped_size_or_type': 0}

    # ── day / month counters ──
    def day(self) -> str:
        return kst_now(self.clock).date().isoformat()

    def daily(self) -> dict:
        d = self.day()
        r = self.q.execute('SELECT * FROM daily WHERE day=?', (d,)).fetchone()
        if r is None:
            return {'day': d, 'requests': 0, 'bytes_in': 0, 'bytes_out': 0, 'candidates': 0, 'content': 0, 'items': 0, 'not_modified': 0}
        return dict(r)

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

    # ── frontier ──
    def add(self, url, kind, depth=0, score=0.5, query=None, target_id=None, parent=None, revisit_hours=None, count=True) -> bool:
        try:
            url = to_uri(url)
        except ValueError:
            return False
        if not url.startswith(('http://', 'https://')) or excluded(url, self.cfg):
            return False
        canonical = canonicalize_url(url)
        if self.q.execute('SELECT 1 FROM frontier WHERE canonical_url=?', (canonical,)).fetchone():
            return False
        cur = self.q.execute("""INSERT OR IGNORE INTO frontier(url,host,kind,depth,score,query,target_id,parent,revisit_hours,next_due,added_day,added_at,canonical_url)
                                VALUES (?,?,?,?,?,?,?,?,?,0,?,?,?)""", (url, host_of(url), kind, depth, round(score, 3), query, target_id, parent, revisit_hours,
                                                                     self.day(), self.clock(), canonical))
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
        return out

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

    def _global_work(self, task):
        with contextlib.closing(connect(self.service_db)) as conn:
            conn.execute(f'PRAGMA busy_timeout={self.svc_busy_timeout}')
            net = self._net(conn, run_id=self.access.run_id, deadline=self.stop_at)
            roster = michelin.Roster(conn, net, self.raw, self.roster_config, self.roster_seeds)

            def extract(fetch, gaz):
                from .deepseek import Adapters

                meter = CostMeter(conn, self.access.run_id, UNIT_COSTS)
                meter.stage = 'extract'
                adapter = Adapters.for_meter(meter)
                deadline = time.monotonic() + max(0, min(global_queue.LEASE_SECONDS - 20,
                                                          task['lease_expires_at'] - self.clock() - 10,
                                                          self.stop_at - self.clock() - 5))
                return extract_roster(conn, self.raw, fetch, gaz, adapter, deadline)

            state = roster.work(task, extractor=extract)
            return state, net.request_count, net.robots_count

    def _net(self, conn, **options):
        def record(route, traffic):
            proxynet.record_traffic(conn, self.day(), route, traffic)

        def transport(url, headers, timeout):
            send = self.transport or (lambda url, headers, timeout: self.route.request(url, headers, timeout=timeout, traffic=self.traffic, on_traffic=record))
            status, headers, body, error = send(url, headers, timeout)
            return Response(url, status, headers, body, error=error)

        return PoliteNet(transport, self.sleep, self.clock, conn=conn,
                         extra_banned=self.cfg.get('exclude_hosts', ()),
                         korean_press_hosts=(host_of(f['url']) for f in self.cfg.get('feeds', ()) if f.get('country') == 'KR'),
                         **options)

    def work(self, task):
        headers = {'Accept': 'text/html,application/xhtml+xml,application/rss+xml,application/xml;q=0.9,*/*;q=0.5',
                   'Accept-Language': 'ko-KR,ko;q=0.9'}
        if task['etag']:
            headers['If-None-Match'] = task['etag']
        if task['last_modified']:
            headers['If-Modified-Since'] = task['last_modified']
        with contextlib.closing(connect(self.service_db)) as conn:
            guard = Guard(conn, self.access.run_id, 'heavy:attempt')
            key = stable_id('url', task['canonical_url'] or task['url'])
            dispatched = False

            def before_send():
                nonlocal dispatched
                if not dispatched:
                    dispatched = bool(guard.attempt(key, previous=task['attempts']))
                    conn.commit()
                return dispatched

            net = self._net(conn, run_id=self.access.run_id, deadline=self.stop_at)
            r = net.get(task['url'], headers, before_send=before_send)
            if dispatched and (r.blocked or r.error in (
                'block_end', 'robots', 'robots_unavailable', 'banned', 'unsafe_destination',
            )):
                guard.defer(key)
                conn.commit()
        return task, r, net.request_count, net.robots_count

    # ── result handling (main thread, single DB writer) ──
    def _finish(self, url, kind, revisit_hours, **cols):
        if kind in REVISIT_KINDS and cols.get('state', 'done') in ('done',):
            cols['state'], cols['next_due'] = 'queued', self.clock() + 3600 * float(revisit_hours or 24)
        cols['fetched_at'] = self.clock()
        self.q.execute('UPDATE frontier SET ' + ', '.join(f'{k}=?' for k in cols) + ' WHERE url=?', (*cols.values(), url))

    def handle(self, res):
        t, response, requests, robots = res
        url = t['url']
        st, hd, body, err = response.status, response.headers, response.body, response.error
        self.stats['requests'] += requests
        self.stats['robots_requests'] += robots
        self.bump(requests=requests)
        if response.blocked:
            self.stats['blocked_hosts'] += 1
            self._finish(url, t['kind'], t['revisit_hours'], state='queued', next_due=self.stop_at,
                         status=st, error='fail_login_page' if err == 'login_page' else 'fail_blocked')
            return
        if err == 'block_end':
            self._finish(url, t['kind'], t['revisit_hours'], state='queued', next_due=self.stop_at, error=err)
            return
        if err in ('robots', 'banned', 'unsafe_destination'):
            self.stats['robots_denied'] += err == 'robots'
            self._finish(url, 'article', None, state='skipped', error=err)
            return
        if err == 'robots_unavailable':
            self._finish(url, t['kind'], t['revisit_hours'], state='queued', next_due=self.clock() + 3600, error=err)
            return
        if err and str(err).startswith(('skipped_', 'truncated')):
            self.stats['skipped_size_or_type'] += 1
            self._finish(url, 'article', None, state='skipped', status=st, error=err)
            return None
        if st == 304:
            self.stats['not_modified'] += 1
            self.bump(not_modified=1)
            self._finish(url, t['kind'], t['revisit_hours'], state='done', status=304, error=None, attempts=0)
            return None
        if st != 200:
            self.stats['errors'] += 1
            key = stable_id('url', t['canonical_url'] or url)
            item = Guard(self.svc, self.access.run_id, 'heavy:attempt').item(key)
            att = item['attempts'] if item else t['attempts'] + 1
            exhausted = err == 'attempts_exhausted' or att >= threshold('MAX_ATTEMPTS', 3)
            gone = st in (404, 410) or exhausted
            if exhausted and st not in (404, 410):
                queue_review(self.svc, 'guard_attempts_exhausted', 'heavy_frontier', stable_id('url', url),
                             {'stage': 'heavy_fetch', 'attempts': att}, dedupe_key=stable_id('guard-heavy', url))
            self._finish(url, 'article' if gone else t['kind'], t['revisit_hours'], state='failed' if gone else 'queued', attempts=att,
                         next_due=self.clock() + 3600 * att, status=st, error='attempts_exhausted' if exhausted else str(err or st))
            return None
        self.stats['ok_pages'] += 1
        etag, lm = hd.get('etag'), hd.get('last-modified')
        ctype = (hd.get('content-type') or '').split(';')[0].strip().lower()
        h = RawStore.sha(body)
        if t['content_hash'] == h:
            self.stats['unchanged'] += 1
            self._finish(url, t['kind'], t['revisit_hours'], state='done', status=200, attempts=0, etag=etag, last_modified=lm)
            return None
        if 'xml' in ctype or 'rss' in ctype or 'atom' in ctype or body.lstrip()[:5] in (b'<?xml', b'<rss ', b'<feed'):
            self.process_feed({**t, 'url': response.final_url}, body)
        elif ctype.startswith('text/html') or ctype in ('application/xhtml+xml', ''):
            if not self.process_html({**t, 'url': response.final_url, 'host': host_of(response.final_url)}, body, hd):
                self._finish(url, t['kind'], t['revisit_hours'], state='queued', next_due=self.clock() + 86400,
                             error='js_renderer_unavailable')
                return
        self._finish(url, t['kind'], t['revisit_hours'], state='done', status=200, attempts=0, etag=etag, last_modified=lm, content_hash=h, error=None)
        return None

    def process_feed(self, t, body):
        items = parse_feed(body.decode('utf-8', 'ignore') if isinstance(body, bytes) else body, t['url'])
        cutoff = (kst_now(self.clock).date() - dt.timedelta(days=int(self.caps['lookback_days']))).isoformat()
        self.stats['items_seen'] += len(items)
        self.bump(items=len(items))
        for it in items:
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
        if unreadable_js_shell(t['url'], html, text):
            return False
        if len(text) >= MIN_TEXT_CHARS:
            el = eligibility(self.gaz, f"{doc.get('title') or ''}\n{text}")
            if el['target_ids']:
                self.handoff(t, html, doc, el)
        if not doc['links']:
            return True
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
        for s, u in sorted(cands, reverse=True):
            self.add(u, 'article', t['depth'] + 1, s, target_id=t['target_id'], parent=t['url'])

        return True

    def handoff(self, t, html, doc, el):
        """Ordinary fetch_record (+raw) for the 07:00 extract stage, with a priority score for the DeepSeek order."""
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
        rows = self.q.execute("""SELECT * FROM frontier WHERE state='queued' AND next_due<=?
            ORDER BY score DESC, depth ASC, added_at ASC, url""", (self.clock(),))
        out, hosts = [], set(busy)
        for r in rows:
            if len(out) >= limit:
                break
            h = host_of(r['url'])
            if h in hosts or h in self.stopped_hosts or self.access.blocked_response(r['url']):
                continue
            task = {**r, 'host': h}
            hosts.add(h)
            out.append(task)
        return out

    def run(self, deadline: float, block: str = 'manual', optional: bool = False) -> dict:
        run_id = 'hv_' + uuid.uuid4().hex[:10]
        started = self.clock()
        self.started = started
        rep = {'run_id': run_id, 'block': block, 'started_kst': kst_now(self.clock).isoformat(timespec='seconds'), 'egress': None, 'egress_reason': 'no_dispatch'}
        if deadline <= started + 60:
            return self._end(rep, 'skipped', 'block_over')
        rep['seeded'] = self.seed()
        if self.roster_config:
            from .discover import load_seeds

            self.roster_seeds = load_seeds()
            michelin.bootstrap(self.svc, started, self.roster_config, self.roster_seeds)
            rep['michelin'] = {'jobs': 0, 'results': {}}
        if optional and self.backlog() + self.global_backlog() < int(self.caps.get('backlog_for_optional_block', 1)):
            return self._end(rep, 'skipped', 'backlog_small')
        stop_at = deadline - 25
        self.stop_at = deadline                       # leave room for the 20 s request timeout before the hard stop
        self.route = proxynet.Route()
        self.access = self._net(self.svc, deadline=deadline)
        self.access.start_run(run_id=run_id, expires_at=deadline)
        halt, inflight = None, {}
        self.stopped_hosts = set()
        try:
            if self.roster_config:
                with self.svc:
                    self.svc.execute("""INSERT INTO daemon_run(run_id,daemon,status,trigger,cost_cap_units,started_at)
                        VALUES (?,'kg-heavy','running',?,0,?)""", (run_id, block, michelin.iso_stamp(started)))
            if self.egress:
                _, rep['egress_reason'] = self.egress()
            elif self.transport is None:
                self.route.egress_ok(probe=lambda url, timeout: self.access.get(url, diagnostic=True, timeout=timeout))
            with cf.ThreadPoolExecutor(max_workers=self.caps['max_concurrency']) as ex:
                while True:
                    now = self.clock()
                    self.flush_traffic()
                    if halt is None and self.terminating:
                        halt = 'sigterm'
                    if halt is None and now >= stop_at:
                        halt = 'deadline'
                    if halt is None:
                        free = self.caps['max_concurrency'] - len(inflight)
                        if free > 0:
                            busy = {t['host'] for t in inflight.values()}
                            if self.roster_config:
                                while free > 0 and not self.terminating and self.clock() < stop_at:
                                    task = global_queue.claim(self.svc, self.clock(), busy=busy)
                                    if not task:
                                        break
                                    busy.add(task['host'])
                                    inflight[ex.submit(self._global_work, task)] = task
                                    free -= 1
                            if not self.terminating:
                                for task in self.pick(busy, free):
                                    inflight[ex.submit(self.work, task)] = task
                    if not inflight:
                        if halt:
                            break
                        if self.backlog() + self.global_backlog() == 0:
                            halt = 'queue_empty'
                            break
                        halt = 'nothing_ready'
                        break
                    self.q.commit()                         # never hold the write lock while waiting on the network
                    done, _ = cf.wait(list(inflight), timeout=1, return_when=cf.FIRST_COMPLETED)
                    for f in done:
                        task = inflight.pop(f)
                        if 'job_key' in task:
                            try:
                                state, requests, robots = f.result()
                            except Exception as exc:
                                michelin.record_exception(self.svc, task, self.clock, exc)
                                state, requests, robots = type(exc).__name__, 0, 0
                            rep['michelin']['jobs'] += 1
                            results = rep['michelin']['results']
                            results[state] = results.get(state, 0) + 1
                            self.stats['requests'] += requests
                            self.stats['robots_requests'] += robots
                            self.bump(requests=requests)
                            continue
                        lane = Guard(self.svc, run_id, 'heavy:' + task['host'])
                        try:
                            result = f.result()
                            self.handle(result)
                            current = self.q.execute('SELECT * FROM frontier WHERE url=?', (task['url'],)).fetchone()
                        except Exception as exc:
                            # A faulty worker yields its current item so other hosts can proceed.
                            lane.failure(type(exc).__name__)
                            self._finish(task['url'], task['kind'], task['revisit_hours'], state='queued',
                                         next_due=self.clock() + 3600, error=type(exc).__name__)
                            self.stats['errors'] += 1
                        else:
                            response = result[1]
                            key = stable_id('url', task['canonical_url'] or task['url'])
                            fields = ('state', 'next_due', 'status', 'attempts', 'error', 'content_hash')
                            before, after = ([row[k] for k in fields] for row in (task, current))
                            if before == after and not response.blocked and response.error not in (
                                'block_end', 'robots_unavailable',
                            ):
                                lane.runnable([key, after])
                            elif response.status in (200, 304) and not response.error:
                                lane.progress(key)
                                Guard(self.svc, run_id, 'heavy:attempt').complete(key)
                            elif not response.blocked and response.error not in (
                                'block_end', 'robots', 'robots_unavailable', 'banned',
                            ):
                                lane.failure(response.error or response.status)
                        if lane.reason:
                            self.stopped_hosts.add(task['host'])
                        self.svc.commit()
                    self.q.commit()
        except Exception as e:  # noqa: BLE001
            halt = f'crash:{type(e).__name__}'
        finally:
            self.access.finish_run()
            if self.roster_config:
                rep['michelin']['tier'] = global_queue.cursor(self.svc)
                rep['michelin']['due'] = self.global_backlog()
                rep['michelin']['coverage'] = [dict(row) for row in self.svc.execute("""
                    SELECT tier,disposition,COUNT(*) AS scopes FROM global_tier_pass
                    WHERE sweep_id=? AND scope<>'worldwide' GROUP BY tier,disposition ORDER BY tier DESC,disposition""",
                    (global_queue.SWEEP,))]
                with self.svc:
                    self.svc.execute("UPDATE daemon_run SET status=?,finished_at=? WHERE run_id=?",
                                     ('failed' if halt and halt.startswith('crash:') else 'succeeded',
                                      michelin.iso_stamp(self.clock()), run_id))
            if self.route.name:
                rep.update(egress=self.route.name, egress_reason=self.route.reason)
        rep['guard_stops'] = {r['lane']: r['reason'] for r in self.svc.execute(
            'SELECT lane,reason FROM guard_lane WHERE scope=? AND reason IS NOT NULL', (run_id,))}
        reason = halt or 'idle'
        if halt and halt.startswith('crash:'):
            return self._end(rep, 'halted', halt)
        return self._end(rep, 'stopped' if reason in ('deadline', 'queue_empty', 'nothing_ready', 'idle', 'sigterm') else 'halted', reason)

    def global_backlog(self):
        return global_queue.due(self.svc, self.clock()) if self.roster_config else 0

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
        line = {k: rep.get(k) for k in ('run_id', 'block', 'started_kst', 'ended_kst', 'outcome', 'reason', 'egress', 'egress_reason', 'traffic', 'stats', 'day_totals', 'month_bytes', 'guard_stops', 'michelin', 'max_rss_mb')}
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
    r.add_argument('--optional', action='store_true', help='run when work is due (22:00 block)')
    sub.add_parser('egress')
    sub.add_parser('status')
    a = ap.parse_args(argv)
    if a.cmd == 'egress':
        conn = connect(a.db)
        migrate(conn)
        net = PoliteNet(conn=conn)
        try:
            _, reason = proxynet.egress_ok(probe=lambda url, timeout: net.get(url, diagnostic=True, timeout=timeout))
            print(f"egress={proxynet.DEFAULT_ROUTE.name} egress_reason={reason}")
        finally:
            net.finish_run()
        return 0
    conn = connect(a.db)
    migrate(conn)
    queue = a.queue or os.path.join(os.path.dirname(os.path.abspath(a.db)), 'heavy_queue.db')
    hc = HeavyCrawler(queue, conn, roster_config=michelin.load_config())
    if a.cmd == 'status':
        print(json.dumps({'backlog_due': hc.backlog(), 'michelin_due': hc.global_backlog(), 'today': hc.daily(), 'month_bytes': hc.month_bytes(),
                          'queue': {x['state']: x['n'] for x in hc.q.execute('SELECT state, COUNT(*) n FROM frontier GROUP BY state')}}, ensure_ascii=False))
        return 0
    import signal

    def _term(*_):
        hc.terminating = True
    signal.signal(signal.SIGTERM, _term)
    rep = hc.run(deadline_for(a.until), block=a.block, optional=a.optional)
    s, tr = rep.get('stats') or {}, rep.get('traffic') or {}
    print(f"HEAVY={rep['outcome']} reason={rep['reason']} block={a.block} egress={rep.get('egress')} egress_reason={rep['egress_reason']} requests={s.get('requests', 0)} items={s.get('items_seen', 0)} "
          f"candidates={s.get('candidates_new', 0)} content={s.get('content_handoff', 0)} bytes_in={tr.get('bytes_in', 0)} bytes_out={tr.get('bytes_out', 0)} rss_mb={rep.get('max_rss_mb')}")
    return 0


if __name__ == '__main__':
    sys.exit(main())
