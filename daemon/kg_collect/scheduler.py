"""Rank and rotate runnable collection work across pools and query families."""
import datetime as dt
import json

from collections import deque

from .guard import threshold

POOLS = ('discovery', 'backfill', 'refresh')
COST = {'http': 1.0, 'ocr_page': 2.0, 'browser': 4.0}
WEIGHTS = {'novelty': 1.0, 'gap': 0.8, 'urgency': 0.7, 'quality': 0.8, 'cost': 0.5, 'dup': 1.0, 'fail': 0.7}
CADENCE_DAYS = {'volatile': 1, 'stable': 14, 'unlabelled': 7, 'feed': 1}
VOLATILE_LABELS = ('closure', 'relocation', 'rename', 'employment_claim', 'opening')
# Structured / entity-driven paths beat blind keyword search when their other terms tie: otherwise hundreds of cheap, equally scored keyword queries
# starve directories, sitemaps, official sites and the article follow-ups that actually discover new hosts (seen in the first real run).
PATH_BONUS = {'directory': 1.2, 'sitemap': 1.0, 'official_chain': 0.6, 'entity_followup': 0.8, 'feed': 0.8, 'keyword_search': 0.0, 'reverse_gap': 0.3}
def _canon(u: str) -> str:
    try:
        from .fetch import canonicalize_url
        return canonicalize_url(u)
    except Exception:  # noqa: BLE001
        return (u or '').strip()


TIER_QUALITY = {'promoted': 1.0, 'trial': 0.6, 'deprioritized': 0.2, 'blocked': 0.0}


def est_cost(job) -> float:
    u = (job['url'] or '').lower().split('?')[0]
    if u.endswith(('.jpg', '.jpeg', '.png', '.webp')):
        return COST['http'] + COST['ocr_page']
    if u.endswith('.pdf'):
        return COST['http'] + COST['ocr_page']
    return COST['http']


def job_priority(conn, job, now: dt.datetime) -> tuple[float, dict]:
    from .net import host_of, is_search_host
    host = host_of(job['url'] or '') if job['url'] else None
    prof = conn.execute('SELECT * FROM domain_profile WHERE host=?', (host,)).fetchone() if host and not is_search_host(host) else None
    seen_url = bool(job['url'] and conn.execute("SELECT 1 FROM fetch_record WHERE canonical_url=? AND outcome='ok' LIMIT 1", (job['url'],)).fetchone())
    novelty = 1.0 if (prof is None and host and not is_search_host(host)) else (0.2 if seen_url else 0.6)
    if job['path'] == 'keyword_search':
        past = conn.execute("SELECT COUNT(*) n FROM crawl_job WHERE path='keyword_search' AND state='done' AND query=?", (job['query'],)).fetchone()['n']
        novelty = 1.0 if past == 0 else 0.35
    gap = 0.0
    if job['gap_id']:
        g = conn.execute('SELECT field,attempts FROM gap WHERE gap_id=?', (job['gap_id'],)).fetchone()
        gap = 1.0 if g and g['field'] in ('status_recent', 'current_employer', 'chef') else 0.7
        gap *= 0.6 ** (g['attempts'] if g else 0)
    elif job['target_id']:
        gap = 0.3 if conn.execute("SELECT 1 FROM gap WHERE target_id=? AND state='open' LIMIT 1", (job['target_id'],)).fetchone() else 0.0
    urgency = 0.0
    if job['pool'] == 'refresh':
        last = job['finished_at']
        if last:
            age = (now - dt.datetime.fromisoformat(last.replace('Z', '+00:00'))).total_seconds() / 86400
            urgency = min(1.0, age / CADENCE_DAYS['volatile'] / 2)
        else:
            urgency = 0.8
    if job['query'] and any(k in job['query'] for k in ('폐업', '이전')):
        urgency = max(urgency, 0.8)
    quality = job['relevance'] * (TIER_QUALITY.get(prof['state'], 0.6) if prof else 0.7)
    cost = est_cost(job) / 5.0
    dup = 1.0 if (seen_url and job['pool'] != 'refresh') else 0.0
    hostfail = (prof['fail_streak'] if prof else 0)
    fail = min(1.0, 0.4 * job['attempts'] + 0.2 * hostfail)
    comp = {'novelty': novelty, 'gap': gap, 'urgency': urgency, 'quality': quality, 'cost': cost, 'dup': dup, 'fail': fail}
    p = sum(WEIGHTS[k] * comp[k] * (-1 if k in ('cost', 'dup', 'fail') else 1) for k in comp) + PATH_BONUS.get(job['path'], 0.0)
    return round(p, 4), comp


def plan(conn, now: dt.datetime | None = None, *, mother_counts=None, entity_targets=None) -> dict:
    from .discover import KST, canonical_query, query_family
    from .recheck import active_for_job

    now = now or dt.datetime.now(dt.timezone.utc)
    mother_counts = mother_counts or {}
    day = now.astimezone(KST).date().isoformat()
    last_query = dict(conn.execute("SELECT canonical_query,MAX(day) FROM discovery_query_dispatch GROUP BY canonical_query"))
    iso = now.strftime('%Y-%m-%dT%H:%M:%SZ')
    queued = conn.execute("""SELECT * FROM crawl_job WHERE state='queued' AND (not_before IS NULL OR not_before<=?)
        AND attempts<? ORDER BY created_at,job_id""", (iso, threshold('MAX_ATTEMPTS', 3))).fetchall()
    groups = {}
    chosen = {p: [] for p in POOLS}
    counts = dict.fromkeys(POOLS, 0)
    for job in queued:
        jid, pool = job['job_id'], job['pool']
        family = query_family(job, mother_counts)
        query = canonical_query(job['query']) if family else None
        counts[pool] += 1
        if query and last_query.get(query) == day:
            continue
        if family == 'entity' and job['path'] == 'keyword_search' and entity_targets is not None and job['target_id'] not in entity_targets:
            continue
        priority, _ = job_priority(conn, job, now)
        conn.execute('UPDATE crawl_job SET priority=? WHERE job_id=?', (priority, jid))
        rank = (mother_counts.get(job['target_id'], 0) >= 3 if family == 'mother' else False,
                last_query.get(query, ''), bool(active_for_job(conn, jid)), -priority, job['created_at'], jid)
        groups.setdefault((pool, family or 'web'), []).append((rank, job))
    rings = [deque(j for _, j in sorted(rows, key=lambda r: r[0])) for rows in groups.values()]
    order, urls = [], set()
    skipped = 0
    while rings:
        for ring in rings:
            while ring:
                job = ring.popleft()
                url = _canon(job['url']) if job['url'] else None
                if url and url in urls:
                    skipped += 1
                    continue
                if url:
                    urls.add(url)
                chosen[job['pool']].append(job['job_id'])
                order.append(job['job_id'])
                break
        rings = [ring for ring in rings if ring]
    conn.commit()
    return {'chosen': chosen, 'order': order, 'queued_by_pool': counts, 'skipped_same_url': skipped}


def requeue_due(conn, now: dt.datetime | None = None, *, mother_counts=None) -> int:
    """Cadences: volatile-field sources (feeds, status/employment pages) daily; stable-field pages every 14 days; pages that never yielded 7 days;
    deprioritised hosts x4. Done jobs whose last fetch is older than the cadence go back to 'queued'."""
    now = now or dt.datetime.now(dt.timezone.utc)
    from .discover import query_family
    mother_counts = mother_counts or {}
    n = 0
    for j in conn.execute("SELECT * FROM crawl_job WHERE state='done' AND url IS NOT NULL AND path IN ('feed','sitemap','official_chain','directory','keyword_search','entity_followup','reverse_gap')").fetchall():
        from .recheck import active_for_job
        if active_for_job(conn, j['job_id']) or conn.execute('SELECT 1 FROM restaurant_recheck_job WHERE job_id=? AND dedicated_root=1', (j['job_id'],)).fetchone():
            continue
        family = query_family(j, mother_counts)
        if family:
            if not j['finished_at']:
                continue
            completed = dt.datetime.fromisoformat(j['finished_at'].replace('Z', '+00:00'))
            if now - completed >= dt.timedelta(hours=72 if j['last_error'] == 'no_results' or family == 'mother' and mother_counts.get(j['target_id'], 0) >= 3 else 24):
                conn.execute("UPDATE crawl_job SET state='queued',attempts=0 WHERE job_id=?", (j['job_id'],))
                n += 1
            continue
        f = conn.execute("SELECT fetch_id, fetched_at, method, host FROM fetch_record WHERE canonical_url=? AND outcome='ok' ORDER BY fetched_at DESC LIMIT 1", (j['url'],)).fetchone()
        if j['path'] in ('keyword_search', 'entity_followup', 'reverse_gap') and not f:
            f = conn.execute('SELECT fetch_id, fetched_at, method, host FROM fetch_record WHERE job_id=? ORDER BY fetched_at DESC LIMIT 1', (j['job_id'],)).fetchone()
        if not f:
            continue
        labels = {r['label'] for r in conn.execute('SELECT DISTINCT label FROM extraction WHERE fetch_id=?', (f['fetch_id'],))}
        if f['method'] in ('feed', 'sitemap') or j['path'] in ('keyword_search',):
            days = CADENCE_DAYS['feed']
        elif labels & set(VOLATILE_LABELS):
            days = CADENCE_DAYS['volatile']
        elif labels:
            days = CADENCE_DAYS['stable']
        else:
            days = CADENCE_DAYS['unlabelled']
        prof = conn.execute('SELECT state FROM domain_profile WHERE host=?', (f['host'],)).fetchone()
        if prof and prof['state'] == 'deprioritized':
            days *= 4
        age = (now - dt.datetime.fromisoformat(f['fetched_at'].replace('Z', '+00:00'))).total_seconds() / 86400
        if age >= days:
            conn.execute("UPDATE crawl_job SET state='queued', pool=CASE WHEN pool='discovery' THEN 'refresh' ELSE pool END, attempts=0 WHERE job_id=?", (j['job_id'],))
            n += 1
    conn.commit()
    return n
