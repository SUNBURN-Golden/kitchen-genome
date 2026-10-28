"""Source discoverer. Six paths, all driven by the TARGET LIST (regions x cuisines x restaurants x chefs, KR/EN names + aliases):
 1 keyword_search   combined keywords (오픈/이전/폐업/출신/수셰프/헤드셰프) x region x year (+ x cuisine, + x restaurant)
 2 directory        official/reference directories (hotel & restaurant-group pages, Wikipedia rosters; MICHELIN is manual-only: robots forbids bots)
 3 feed/sitemap     RSS, sitemaps, pagination -> article jobs
 4 entity_followup  'A came from B' spawns jobs to verify A's official bio, B's past pages, and the tenure
 5 official_chain   official site -> subpages -> SNS (lead only) -> booking (lead only) -> attachments (PDF/menu images)
 6 reverse_gap      empty fields (see gaps.py)
Every job is capped on relevance (floor), depth (max_depth) and pool queue size, and is idempotent by job_id."""
import datetime as dt
import json
import os
import re
import urllib.parse

import yaml
from kg_daemon.db import ROOT, stable_id, utcnow

from .net import CFG, SEARCH_HOSTS, host_of, is_banned, is_search_host  # noqa: F401  (re-exported)
from .targets import Gazetteer, norm

SEEDS_PATH = os.path.join(ROOT, 'sources', 'discovery_seeds.yaml')
FOOD_CUE = re.compile(r'셰프|쉐프|레스토랑|오마카세|다이닝|미쉐린|미슐랭|수셰프|오픈|폐업|이전|출신|주방')


def load_seeds(path=SEEDS_PATH) -> dict:
    return yaml.safe_load(open(path, encoding='utf-8'))


def initial_hosts() -> set:
    """Domains known before the bot ran: vault + seed + crawler config + curated registry. 'New source' = anything outside this set."""
    hosts = set()
    p = os.path.join(ROOT, 'crawler', 'vault.json')
    if os.path.exists(p):
        for r in json.load(open(p, encoding='utf-8')).get('records', []):
            hosts.add(host_of(r.get('url', '')))
    seed = json.load(open(os.path.join(ROOT, 'data', 'seed.json'), encoding='utf-8'))
    def walk(o):
        if isinstance(o, str) and o.startswith('http'):
            hosts.add(host_of(o))
        elif isinstance(o, dict):
            for k, v in o.items():
                hosts.add(host_of(k)) if isinstance(k, str) and k.startswith('http') else None
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)
    walk(seed)
    for k in ('allowed_browser_hosts', 'unreliable_hosts', 'fulltext_allowed_hosts', 'banned_hosts'):
        hosts.update(h.removeprefix('www.') for h in CFG.get(k, []))
    reg = yaml.safe_load(open(os.path.join(ROOT, 'sources', 'registry.yaml'), encoding='utf-8'))
    for s in reg.get('sources', {}).values():
        for k in ('host', 'url'):
            if s.get(k):
                hosts.add(host_of(s[k]) or s[k])
    hosts.discard('')
    return hosts


def job_id(path: str, key: str) -> str:
    return stable_id('job', path, key)


def enqueue(conn, *, path, pool, url=None, query=None, target_id=None, gap_id=None, depth=0, parent=None, relevance=0.5, caps=None, state='queued', error=None, now=None) -> bool:
    """Idempotent. Returns True if a NEW job row was created. Enforces depth cap, relevance floor, queue cap (discovery is never starved by backfill: separate caps)."""
    caps = caps or {}
    if state == 'queued':
        if depth > caps.get('max_depth', 2):
            return False
        if relevance < caps.get('relevance_floor', 0.30):
            return False
        if url and is_banned(url):
            state, error = 'skipped', 'banned_host(robots/ToS): manual only'
        else:
            cap = caps.get('max_queued_per_pool', {}).get(pool, 400)
            if conn.execute("SELECT COUNT(*) FROM crawl_job WHERE pool=? AND state='queued'", (pool,)).fetchone()[0] >= cap:
                return False
    jid = job_id(path, url or query or '')
    cur = conn.execute("""INSERT OR IGNORE INTO crawl_job(job_id,path,pool,url,query,target_id,gap_id,depth,parent_job_id,relevance,state,last_error,created_at,finished_at)
                          VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                       (jid, path, pool, url, query, target_id, gap_id, depth, parent, relevance, state, error, now or utcnow(), (now or utcnow()) if state != 'queued' else None))
    return cur.rowcount == 1


def search_url(seeds, q: str) -> str:
    return seeds['keyword_search']['endpoint'].format(q=urllib.parse.quote(q))


# ── path 1: combined keyword search ──
def keyword_jobs(conn, seeds, today: dt.date | None = None, limit: int | None = None) -> list[dict]:
    today = today or dt.date.today()
    ks = seeds['keyword_search']
    years = [today.year - i for i in range(ks['years_back'] + 1)]
    regions = [(r['name_ko'], json.loads(r['attrs_json'])) for r in conn.execute("SELECT * FROM target WHERE kind='region' AND status='active'")]
    cuisines = [r['name_ko'] for r in conn.execute("SELECT * FROM target WHERE kind='cuisine' AND status='active'")]
    reg_weight = {}
    for r in conn.execute("SELECT attrs_json FROM target WHERE kind='restaurant' AND origin='seed'"):
        a = re.split(r'[ (/]', json.loads(r['attrs_json']).get('area') or '')[0]
        if a:
            reg_weight[a] = reg_weight.get(a, 0) + 1
    top = max(reg_weight.values()) if reg_weight else 1
    jobs = []
    for kw in ks['keywords']:
        w = ks['keyword_weight'].get(kw, 0.6)
        for region, _ in regions:
            rw = 0.5 + 0.5 * reg_weight.get(region, 0) / top
            for y in years:
                q = f'{region} {kw} 셰프 {y}'
                jobs.append({'path': 'keyword_search', 'pool': 'discovery', 'query': q, 'url': search_url(seeds, q), 'relevance': round(min(1.0, w * rw + (0.05 if y == today.year else 0)), 3)})
    for c in cuisines[:12]:
        for kw in ('오픈', '출신'):
            q = f'{c} 셰프 {kw} 서울'
            jobs.append({'path': 'keyword_search', 'pool': 'discovery', 'query': q, 'url': search_url(seeds, q), 'relevance': round(0.35 + 0.3 * ks['keyword_weight'].get(kw, .6), 3)})
    jobs.sort(key=lambda j: -j['relevance'])
    return jobs[:limit] if limit else jobs


def entity_status_jobs(conn, seeds) -> list[dict]:
    """restaurant x {폐업,이전,오픈} and chef x {출신,수셰프}: tied to named targets -> refresh pool (volatile fields)."""
    ks = seeds['keyword_search']
    jobs = []
    for t in conn.execute("SELECT * FROM target WHERE kind='restaurant' AND origin='seed' AND status='active'"):
        for kw in ('폐업', '이전', '오픈'):
            q = f"{t['name_ko']} {kw}"
            jobs.append({'path': 'keyword_search', 'pool': 'refresh', 'query': q, 'url': search_url(seeds, q), 'target_id': t['target_id'], 'relevance': round(0.4 + 0.5 * ks['keyword_weight'][kw], 3)})
    for t in conn.execute("SELECT * FROM target WHERE kind='chef' AND origin='seed' AND status='active'"):
        q = f"{t['name_ko']} 셰프 출신"
        jobs.append({'path': 'keyword_search', 'pool': 'backfill', 'query': q, 'url': search_url(seeds, q), 'target_id': t['target_id'], 'relevance': 0.6})
    return jobs


# ── paths 2-3: directories, feeds, sitemaps ──
def directory_and_feed_jobs(seeds) -> list[dict]:
    jobs = []
    for d in seeds.get('directories', []):
        jobs.append({'path': 'directory', 'pool': 'discovery', 'url': d['url'], 'relevance': 0.9, 'meta': d})
    for f in seeds.get('feeds', []):
        jobs.append({'path': 'feed', 'pool': 'refresh', 'url': f['url'], 'relevance': 0.9})
    for s in seeds.get('sitemaps', []):
        jobs.append({'path': 'sitemap', 'pool': 'discovery', 'url': s['url'], 'relevance': 0.8, 'meta': s})
    return jobs


def item_relevance(gaz: Gazetteer, title: str, summary: str, url: str) -> float:
    text = f'{title} {summary}'
    n = len({m['target_id'] for m in gaz.find(text)})
    cue = len(FOOD_CUE.findall(text))
    rel = min(0.6, 0.3 * n) + min(0.45, 0.15 * cue) + (0.05 if re.search(r'셰프|쉐프', title) else 0)
    if not text.strip():                      # sitemap url without title: judge by URL only (weak)
        rel = 0.32 if re.search(r'news|article|view|read', url) else 0.1
    return round(min(1.0, rel), 3)


def expand_items(conn, doc, parent_job, gaz, caps, lookback_days=120, today=None, unknown_cap=40) -> dict:
    """feed/sitemap/search-result items -> article jobs (depth+1), filtered by relevance and recency."""
    today = today or dt.date.today()
    made = skipped = 0
    for it in doc.items:
        if it.get('kind') == 'sitemap_index':
            if enqueue(conn, path='sitemap', pool='discovery', url=it['url'], depth=parent_job['depth'], parent=parent_job['job_id'], relevance=0.6, caps=caps):
                made += 1
            continue
        d = it.get('date')
        if d:
            try:
                if (today - dt.date.fromisoformat(d)).days > lookback_days:
                    skipped += 1
                    continue
            except ValueError:
                pass
        rel = item_relevance(gaz, it.get('title', ''), it.get('summary', ''), it['url'])
        pool = 'discovery' if not conn.execute('SELECT 1 FROM domain_profile WHERE host=? AND state=?', (host_of(it['url']), 'promoted')).fetchone() else 'backfill'
        if enqueue(conn, path='feed' if parent_job['path'] in ('feed', 'keyword_search', 'reverse_gap', 'sitemap') else 'directory', pool=pool, url=it['url'], target_id=parent_job.get('target_id'),
                   gap_id=parent_job.get('gap_id'), depth=parent_job['depth'] + 1, parent=parent_job['job_id'], relevance=rel, caps=caps):
            made += 1
        else:
            skipped += 1
    conn.commit()
    return {'queued': made, 'skipped': skipped}


# Wiki chrome / interlanguage / infrastructure hosts: a directory page's links to them are navigation, not sources (first real run followed ~40 language wikis).
CHROME_HOSTS = ('wikipedia.org', 'wikimedia.org', 'wikidata.org', 'mediawiki.org', 'wikimediafoundation.org', 'creativecommons.org')


def directory_link_jobs(conn, doc, parent_job, seeds, caps, limit=20) -> int:
    """A directory/roster page is curated: its out-links to articles and to same-host restaurant/dining pages are follow-up jobs (depth+1, capped).
    SNS and booking links are leads only (official_chain_jobs handles those); banned hosts are skipped by enqueue."""
    oc = seeds['official_chain']
    skip_hosts = tuple(oc['sns_hosts']) + tuple(oc['booking_hosts'])
    made = 0
    for url, anchor, _rel in doc.links:
        if made >= limit:
            break
        uh = host_of(url)
        if not uh or any(uh == s or uh.endswith('.' + s) for s in skip_hosts) or is_search_host(uh):
            continue
        if uh != doc.host and any(uh == s or uh.endswith('.' + s) for s in CHROME_HOSTS):
            continue
        if uh == doc.host:
            if not re.search(r'dining|restaurant|chef|menu|다이닝|레스토랑|셰프|메뉴', (url + ' ' + anchor).lower()) or urllib.parse.urlparse(url).fragment:
                continue
            rel = 0.6
        else:
            rel = 0.5
        if enqueue(conn, path='directory', pool='discovery', url=url, depth=parent_job['depth'] + 1, parent=parent_job['job_id'], relevance=rel, caps=caps, target_id=parent_job.get('target_id')):
            made += 1
    conn.commit()
    return made


# ── path 4: entity-driven follow-ups ──
def followup_jobs(conn, seeds, ext_rows, parent_depth: int = 0) -> list[dict]:
    """career(A came from B) -> verify A's official bio, B's past pages, and A's tenure at B. employment_claim -> A's bio + the employer's official site."""
    jobs = []
    cap = seeds['caps']['max_followups_per_extraction']
    for e in ext_rows:
        a = e['chef_text']
        if not a or e['epistemic'] not in ('claim', 'fact') or e['ambiguity']:
            continue
        mine = []
        if e['label'] == 'career' and e['org_text']:
            b = e['org_text']
            mine = [(f'{a} 셰프 {b} 재직 기간', 0.75), (f'{b} {a} 셰프 경력 소개', 0.7), (f'{a} 셰프 프로필 공식', 0.65)]
        elif e['label'] == 'employment_claim' and e['restaurant_text']:
            r = e['restaurant_text']
            mine = [(f'{a} 셰프 {r} 합류 전 경력', 0.7), (f'{r} 공식 홈페이지 셰프 소개 {a}', 0.65)]
        for q, rel in mine[:cap]:
            jobs.append({'path': 'entity_followup', 'pool': 'backfill', 'query': q, 'url': search_url(seeds, q), 'relevance': rel, 'depth': parent_depth + 1,
                         'target_id': e['resolved_chef_target'], 'source_extraction': e['extraction_id']})
    return jobs


# ── path 5: official site -> SNS -> booking -> attachments ──
def official_chain_jobs(conn, doc, parent_job, seeds, caps) -> dict:
    oc = seeds['official_chain']
    sns, booking = tuple(oc['sns_hosts']), tuple(oc['booking_hosts'])
    made = {'subpages': 0, 'sns_leads': 0, 'booking_leads': 0, 'attachments': 0}
    h = doc.host
    subs = 0
    for url, anchor, _rel in doc.links:
        uh = host_of(url)
        low = url.lower().split('?')[0]
        if any(uh == s or uh.endswith('.' + s) for s in sns):
            # a LEAD only: Instagram etc. stay behind their own connector/pause policy; we never fetch them here
            if enqueue(conn, path='official_chain', pool='discovery', url=url, depth=parent_job['depth'] + 1, parent=parent_job['job_id'], relevance=0.5, caps=caps,
                       state='skipped', error='sns_lead_recorded_only(connector policy: pause on checkpoint, no fetch from this path)', target_id=parent_job.get('target_id')):
                made['sns_leads'] += 1
        elif any(uh == b or uh.endswith('.' + b) for b in booking):
            # public booking page for THIS restaurant: fetched only through the polite net (robots decides; we never pose as a search engine)
            if enqueue(conn, path='official_chain', pool='discovery', url=url, query='booking', depth=parent_job['depth'] + 1, parent=parent_job['job_id'], relevance=0.5, caps=caps,
                       target_id=parent_job.get('target_id'), **({} if parent_job.get('target_id') else {'state': 'skipped', 'error': 'booking_lead_without_target'})):
                made['booking_leads'] += 1
        elif low.endswith('.pdf'):
            if enqueue(conn, path='attachment', pool='discovery', url=url, depth=parent_job['depth'] + 1, parent=parent_job['job_id'], relevance=0.7, caps=caps, target_id=parent_job.get('target_id')):
                made['attachments'] += 1
        elif uh == h and any(k in low or k in anchor.lower() for k in oc['subpage_hints']) and subs < caps.get('max_official_subpages', 6):
            if enqueue(conn, path='official_chain', pool='discovery', url=url, depth=parent_job['depth'] + 1, parent=parent_job['job_id'], relevance=0.6, caps=caps, target_id=parent_job.get('target_id')):
                made['subpages'] += 1
                subs += 1
    for src, alt in doc.images:
        if re.search(r'menu|메뉴|poster|포스터|course|코스', (src + ' ' + alt).lower()):
            if enqueue(conn, path='attachment', pool='discovery', url=src, depth=parent_job['depth'] + 1, parent=parent_job['job_id'], relevance=0.6, caps=caps, target_id=parent_job.get('target_id')):
                made['attachments'] += 1
    conn.commit()
    return made


def seed_official_site_jobs(conn) -> list[dict]:
    """Official sites already known for seed restaurants are the roots of the official chain."""
    jobs = []
    for r in conn.execute("SELECT * FROM target WHERE kind='restaurant' AND origin='seed'"):
        site = json.loads(r['attrs_json']).get('site')
        if site and not re.search(r'instagram|catchtable|facebook|naver', site):
            jobs.append({'path': 'official_chain', 'pool': 'refresh', 'url': site, 'target_id': r['target_id'], 'relevance': 0.85})
    return jobs


# ── path 5b: find the restaurant's OWN site (domain probe / press-article link), verified before use ──
def official_probe_jobs(conn, limit: int = 40) -> list[dict]:
    """Restaurants with open gaps and no verified official site -> guessed-domain probe jobs (query='probe:probe_verified').
    Each probe is one polite GET; the page must pass official.verify_site or it is discarded (never used as evidence)."""
    from . import official
    jobs = []
    rows = conn.execute("""SELECT t.* FROM target t WHERE t.kind='restaurant' AND t.status='active'
                           AND EXISTS (SELECT 1 FROM gap g WHERE g.target_id=t.target_id AND g.state='open')
                           AND NOT EXISTS (SELECT 1 FROM official_site o WHERE o.target_id=t.target_id AND o.verified=1)
                           ORDER BY t.name_en""").fetchall()
    for t in rows:
        for u in official.probe_candidates(t):
            jobs.append({'path': 'official_chain', 'pool': 'discovery', 'url': u, 'query': 'probe:probe_verified', 'target_id': t['target_id'], 'relevance': 0.55})
        if len(jobs) >= limit:
            break
    return jobs[:limit]
