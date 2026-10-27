"""Scheduler: separate budgets for backfill / discovery / refresh with a RESERVED discovery share; priority = novelty + gaps + urgency + quality
minus cost, duplicates and repeated failures; different cadences for volatile vs stable fields."""
import datetime as dt
import json

from kg_daemon.db import utcnow

SHARES = {'backfill': 0.35, 'discovery': 0.40, 'refresh': 0.25}
DISCOVERY_RESERVED = 0.30              # discovery can never be squeezed below this share by a big backfill backlog
COST = {'http': 1.0, 'ocr_page': 2.0, 'browser': 4.0}
WEIGHTS = {'novelty': 1.0, 'gap': 0.8, 'urgency': 0.7, 'quality': 0.8, 'cost': 0.5, 'dup': 1.0, 'fail': 0.7}
CADENCE_DAYS = {'volatile': 1, 'stable': 14, 'unlabelled': 7, 'feed': 1}
VOLATILE_LABELS = ('closure', 'relocation', 'rename', 'employment_claim', 'opening')
# Structured / entity-driven paths beat blind keyword search when their other terms tie: otherwise hundreds of cheap, equally scored keyword queries
# starve directories, sitemaps, official sites and the article follow-ups that actually discover new hosts (seen in the first real run).
PATH_BONUS = {'directory': 1.2, 'sitemap': 1.0, 'official_chain': 0.6, 'entity_followup': 0.8, 'feed': 0.8, 'keyword_search': 0.0, 'reverse_gap': 0.3}
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


def plan(conn, total_units: float, now: dt.datetime | None = None, max_attempts: int = 3) -> dict:
    """Pick jobs per pool within that pool's budget. Discovery budget is reserved; unspent discovery budget spills to the other pools only at the end."""
    now = now or dt.datetime.now(dt.timezone.utc)
    iso = now.strftime('%Y-%m-%dT%H:%M:%SZ')
    budgets = {p: total_units * max(SHARES[p], DISCOVERY_RESERVED if p == 'discovery' else 0) for p in SHARES}
    scale = total_units / sum(budgets.values())
    budgets = {p: b * scale for p, b in budgets.items()}
    queued = conn.execute("SELECT * FROM crawl_job WHERE state='queued' AND (not_before IS NULL OR not_before<=?) AND attempts<?", (iso, max_attempts)).fetchall()
    ranked: dict = {p: [] for p in SHARES}
    for j in queued:
        pr, comp = job_priority(conn, j, now)
        conn.execute('UPDATE crawl_job SET priority=? WHERE job_id=?', (pr, j['job_id']))
        ranked[j['pool']].append((pr, j, comp))
    for p in ranked:
        ranked[p].sort(key=lambda x: -x[0])
    chosen: dict = {p: [] for p in SHARES}
    left = dict(budgets)
    for p in ('discovery', 'backfill', 'refresh'):
        for pr, j, comp in ranked[p]:
            c = est_cost(j)
            if left[p] >= c:
                chosen[p].append(j['job_id'])
                left[p] -= c
    spill = left['discovery']                       # nobody could use the reserved share -> others may
    for p in ('backfill', 'refresh'):
        for pr, j, comp in ranked[p]:
            if j['job_id'] in chosen[p]:
                continue
            c = est_cost(j)
            if spill >= c:
                chosen[p].append(j['job_id'])
                spill -= c
    conn.commit()
    return {'budgets': {k: round(v, 2) for k, v in budgets.items()}, 'chosen': chosen, 'queued_by_pool': {p: len(ranked[p]) for p in ranked},
            'discovery_reserved_share': DISCOVERY_RESERVED}


def requeue_due(conn, now: dt.datetime | None = None) -> int:
    """Cadences: volatile-field sources (feeds, status/employment pages) daily; stable-field pages every 14 days; pages that never yielded 7 days;
    deprioritised hosts x4. Done jobs whose last fetch is older than the cadence go back to 'queued'."""
    now = now or dt.datetime.now(dt.timezone.utc)
    n = 0
    for j in conn.execute("SELECT * FROM crawl_job WHERE state='done' AND url IS NOT NULL AND path IN ('feed','sitemap','official_chain','directory','keyword_search','entity_followup','reverse_gap')").fetchall():
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
