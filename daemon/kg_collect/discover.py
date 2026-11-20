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
import unicodedata
from zoneinfo import ZoneInfo

import yaml
from kg_ai import pending
from kg_daemon.db import ROOT, stable_id, utcnow

from .net import CFG, SEARCH_HOSTS, host_of, is_banned, is_search_host  # noqa: F401  (re-exported)
from .targets import Gazetteer, norm, tid

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
        if depth > min(2, caps.get('max_depth', 2)):
            return False
        if relevance < caps.get('relevance_floor', 0.30):
            return False
        if url and is_banned(url):
            state, error = 'skipped', 'banned_host(robots/ToS): manual only'
        else:
            cap = caps.get('max_queued_per_pool', {}).get(pool, 400)
            if conn.execute("SELECT COUNT(*) FROM crawl_job WHERE pool=? AND state='queued'", (pool,)).fetchone()[0] >= cap:
                return False
    # Candidate-URL budget is spent when a request is actually dispatched (PoliteNet.get), not
    # when a URL is enumerated or queued: a feed-heavy day must not exhaust the quota with links
    # that are never fetched.
    jid = job_id(path, url or query or '')
    cur = conn.execute("""INSERT OR IGNORE INTO crawl_job(job_id,path,pool,url,query,target_id,gap_id,depth,parent_job_id,relevance,state,last_error,created_at,finished_at)
                          VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                       (jid, path, pool, url, query, target_id, gap_id, depth, parent, relevance, state, error, now or utcnow(), (now or utcnow()) if state != 'queued' else None))
    return cur.rowcount == 1


def search_url(seeds, q: str, lang='ko') -> str:
    template = seeds['keyword_search']['endpoint']
    if lang == 'en':
        template = template.replace('setlang=ko', 'setlang=en')
    return template.format(q=urllib.parse.quote(q))


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


def eligible_entity_targets(conn, qualified=()):
    """Previously active discoveries must still pass the source and name checks."""
    return {t['target_id'] for t in conn.execute("SELECT * FROM target WHERE kind IN ('chef','restaurant') AND status='active'")
            if t['origin'] == 'seed' or t['target_id'] in qualified or json.loads(t['attrs_json']).get('published_to_site')}


def entity_status_jobs(conn, seeds, qualified=(), *, contexts=None) -> list[dict]:
    """Rotate status and career searches for seed and promoted discovered entities."""
    allowed = eligible_entity_targets(conn, qualified)
    ks = seeds['keyword_search']
    jobs = []
    for t in conn.execute("SELECT * FROM target WHERE kind IN ('chef','restaurant') AND status='active'"):
        if t['target_id'] not in allowed:
            continue
        name = t['name_ko'] or t['name_en']
        attrs = {**(contexts or {}).get(t['target_id'], {}), **{k: v for k, v in json.loads(t['attrs_json']).items() if v}}
        if t['kind'] == 'chef':
            queries = [(f'"{name}" 셰프 경력 인터뷰', 'backfill', .65), (f'{name} 셰프 출신', 'backfill', .6)]
        else:
            queries = [(f'{name} {kw}', 'refresh', round(.4 + .5 * ks['keyword_weight'][kw], 3)) for kw in ('폐업', '이전', '오픈')]
            if attrs.get('chef'):
                location = f' {attrs["city"]}' if attrs.get('city') else ''
                queries.append((f'"{attrs["chef"]}" "{name}"{location} 오픈', 'refresh', .8))
        for q, pool, rel in queries:
            jobs.append({'path': 'keyword_search', 'pool': pool, 'query': q, 'url': search_url(seeds, q),
                         'target_id': t['target_id'], 'relevance': rel})
    return jobs


MOTHER_LOOKBACK = 365
QUERY_CAPS = {'mother': 8, 'entity': 8, 'general': 8}
KST = ZoneInfo('Asia/Seoul')
MOTHER_TEMPLATES = {
    'ko': ('{name} 출신 셰프 서울', '{name} 수셰프 독립', '{name} 출신 셰프 오픈 인터뷰'),
    'en': ('{name} former chef Seoul restaurant', '{name} former sous chef Seoul opening', '{name} alumni chef Seoul interview'),
}


def mother_kitchen_jobs(seed, today, seeds=None):
    from .site_publish import kitchen_counts

    seeds = seeds or load_seeds()
    counts = kitchen_counts(seed)
    jobs = []
    for k in sorted(seed.get('kitchens', []), key=lambda k: (counts[k['id']], k['id'])):
        count = counts[k['id']]
        if count >= 3:
            continue
        spec = seeds.get('mother_kitchens', {}).get(k['id'], {})
        for lang in ('ko', 'en'):
            canonical = k['name'] if lang == 'ko' else k.get('en') or k.get('name_en') or k['name']
            names = list(dict.fromkeys([canonical, *spec.get(lang, [])]))
            rotation = today.toordinal()
            name = names[(rotation + 2) % len(names)]
            context = spec.get(f'context_{lang}')
            phrase = f'"{name}"' + (f' "{context}"' if context else '')
            query = MOTHER_TEMPLATES[lang][rotation % 3].format(name=phrase)
            jobs.append({'path': 'keyword_search', 'pool': 'backfill', 'query': query,
                         'url': search_url(seeds, query, lang), 'target_id': tid('restaurant', k['name']), 'relevance': .8})
    return jobs


def reconcile_mother_jobs(conn, seed, today, seeds, caps):
    from .site_publish import kitchen_counts

    counts = kitchen_counts(seed)
    by_target = {tid('restaurant', k['name']): counts[k['id']] for k in seed.get('kitchens', [])}
    active = [j for j in mother_kitchen_jobs(seed, today, seeds) if j['target_id'] in by_target]
    current_due = {job_id(j['path'], j['url']) for j in active}
    for row in conn.execute("SELECT * FROM crawl_job WHERE path='keyword_search' AND state IN ('queued','dropped') AND query LIKE ?", ('"%',)):
        if query_family(row, by_target) != 'mother':
            continue
        filled = by_target[row['target_id']] >= 3
        if filled and row['state'] == 'queued':
            conn.execute("UPDATE crawl_job SET state='dropped',last_error='mother_kitchen_filled' WHERE job_id=?", (row['job_id'],))
        elif not filled and row['job_id'] in current_due and row['last_error'] == 'mother_kitchen_filled':
            conn.execute("UPDATE crawl_job SET state='queued',last_error=NULL WHERE job_id=?", (row['job_id'],))
    count = sum(enqueue(conn, **job, caps=caps) for job in active)
    return by_target, current_due, count


def canonical_query(text):
    return re.sub(r'\s+', ' ', unicodedata.normalize('NFKC', text).translate(str.maketrans('“”', '""')).strip()).casefold()


def query_family(job, mother_targets):
    query = job['query']
    if not query or not is_search_host(host_of(job['url'] or '')) or job['path'] not in ('keyword_search', 'reverse_gap', 'entity_followup'):
        return None
    if (job['path'] == 'keyword_search' and job['target_id'] in mother_targets and query.startswith('"')
            and re.search(r' 출신 셰프 | 수셰프 독립$| (?:former |alumni )', query)):
        return 'mother'
    if job['path'] == 'entity_followup' or job['path'] == 'keyword_search' and job['target_id']:
        return 'entity'
    return 'general'


def mother_language(query):
    return 'en' if re.search(r'" (?:former|alumni) ', query) else 'ko'


class QuerySlots:
    """In-memory planning view and durable, transactional dispatch claims."""

    def __init__(self, conn, now, caps=None, *, history=True):
        self.day = now.astimezone(KST).date().isoformat()
        self.caps = caps or QUERY_CAPS
        self.used = dict.fromkeys(QUERY_CAPS, 0)
        self.targets = {}
        self.languages = set()
        self.queries = set()
        self.last_target = {}
        self.last_query = {}
        rows = (conn.execute('SELECT * FROM discovery_query_dispatch') if history else
                conn.execute('SELECT * FROM discovery_query_dispatch WHERE day=?', (self.day,)))
        for row in rows:
            family, target, query, day = row['family'], row['target_id'], row['canonical_query'], row['day']
            self.last_query[query] = max(day, self.last_query.get(query, ''))
            if family == 'entity' and target:
                self.last_target[target] = max(day, self.last_target.get(target, ''))
            if day == self.day:
                self.consume(family, target, query)

    def consume(self, family, target, query):
        self.used[family] += 1
        self.queries.add(query)
        if family == 'entity' and target:
            self.targets[target] = self.targets.get(target, 0) + 1
        if family == 'mother':
            self.languages.add((target, mother_language(query)))

    def allows(self, family, target, query, reserve=0):
        if query in self.queries:
            return False
        if family == 'mother':
            return (self.used['mother'] < self.caps['mother'] and (target, mother_language(query)) not in self.languages
                    and self.used['mother'] + self.used['entity'] < self.caps['mother'] + self.caps['entity'])
        if family == 'entity':
            spare = max(0, self.caps['mother'] - self.used['mother'] - reserve)
            return self.used['entity'] < self.caps['entity'] + spare and self.targets.get(target, 0) < 2
        return self.used['general'] < self.caps['general']


def claim_query(conn, job, family, now, caps=None, reserve=0):
    """Claim one canonical query before dispatch, including across processes and restarts."""
    query, target = canonical_query(job['query']), job['target_id']
    conn.commit()
    with conn:
        conn.execute('BEGIN IMMEDIATE')
        slots = QuerySlots(conn, now, caps, history=False)
        if not slots.allows(family, target, query, reserve):
            return False
        conn.execute("INSERT INTO discovery_query_dispatch(day,canonical_query,family,target_id,job_id) VALUES (?,?,?,?,?)",
                     (slots.day, query, family, target, job['job_id']))
    return True


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
def followup_jobs(conn, seeds, ext_rows, parent_depth: int = 0, gaz=None) -> list[dict]:
    """career(A came from B) -> verify A's official bio, B's past pages, and A's tenure at B. employment_claim -> A's bio + the employer's official site."""
    jobs = []
    cap = seeds['caps']['max_followups_per_extraction']
    from .extract import clean_venue, publication_assertions
    from .review import reject_reason
    gaz = gaz or Gazetteer(conn)
    for row in ext_rows:
        e = dict(row)
        a = e['chef_text']
        if (not a or e['epistemic'] not in ('claim', 'fact') or e['ambiguity']
                or e.get('asserted_by') != 'source' or pending(e) or not e['extractor'].startswith('rule:')):
            continue
        assertions = publication_assertions(e['quote'], gaz)
        mine = []
        if e['label'] == 'career' and e['org_text']:
            b = clean_venue(e['org_text'])
            if not b or not any(x['label'] == 'career' and norm(x['chef']) == norm(a) and norm(x['org']) == norm(b) for x in assertions):
                continue
            venues = {r['target_id']: r for r in gaz.lookup(b) if r['kind'] == 'restaurant'}
            if len(venues) > 1 or not venues and reject_reason('restaurant', b):
                continue
            if venues:
                b = next(iter(venues.values()))['name_ko'] or b
            mine = [(f'"{b}" 출신 셰프', .75), (f'"{b}" 수셰프 인터뷰', .7),
                    (f'"{a}" 셰프 경력 인터뷰', .65)]
        elif e['label'] == 'employment_claim' and e['restaurant_text']:
            r = e['restaurant_text']
            if not any(x['label'] == 'employment_claim' and norm(x['chef']) == norm(a) and norm(x['rest']) == norm(r) for x in assertions):
                continue
            mine = [(f'"{a}" "{r}" 셰프 소개', .7)]
        # A source-derived search can start its own root when the article is already deep in a link chain.
        depth = 0 if parent_depth >= 1 else 1
        for q, rel in mine[:cap]:
            jobs.append({'path': 'entity_followup', 'pool': 'backfill', 'query': q, 'url': search_url(seeds, q),
                         'relevance': rel, 'depth': depth,
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
