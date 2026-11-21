"""Scoreboard: the numbers the owner asked for. Every figure is computed from the DB (no hand-entered values)."""
import json
import os
import statistics

from kg_daemon.db import ROOT

from . import gaps
from .discover import initial_hosts

GOLD_SYNTH = os.path.join(ROOT, 'tests', 'fixtures', 'gold_synthetic.json')


def scoreboard(conn, bot=None, gold_paths=None) -> dict:
    init = bot.initial if bot else initial_hosts()
    promoted = [r['host'] for r in conn.execute("SELECT host FROM domain_profile WHERE state='promoted' AND in_initial_registry=0 ORDER BY host")]
    seen_new = conn.execute("SELECT COUNT(*) FROM domain_profile WHERE in_initial_registry=0").fetchone()[0]
    tgt = {r['status']: r['n'] for r in conn.execute("SELECT status, COUNT(*) n FROM target WHERE origin='discovered' GROUP BY status")}
    cov = gaps.coverage(conn)
    g_tot = conn.execute('SELECT COUNT(*) FROM gap').fetchone()[0]
    g_res = conn.execute("SELECT COUNT(*) FROM gap WHERE state='resolved'").fetchone()[0]
    g_cls = {r['klass']: r['n'] for r in conn.execute("SELECT klass, COUNT(*) n FROM gap WHERE state='open' GROUP BY klass")}
    lat = [r['latency_hours'] for r in conn.execute("SELECT latency_hours FROM field_history WHERE latency_hours IS NOT NULL")]
    changes = conn.execute('SELECT COUNT(*) FROM field_history').fetchone()[0]
    valid_items = conn.execute("""SELECT COUNT(*) FROM extraction WHERE epistemic IN ('claim','fact') AND ambiguity IS NULL
                                  AND (resolved_chef_target IS NOT NULL OR resolved_restaurant_target IS NOT NULL)""").fetchone()[0]
    spent = conn.execute('SELECT COALESCE(SUM(units),0) FROM budget_ledger').fetchone()[0]
    by_pool = {r['pool']: round(r['u'], 1) for r in conn.execute('SELECT pool, SUM(units) u FROM budget_ledger GROUP BY pool')}
    fetch = {r['outcome']: r['n'] for r in conn.execute('SELECT outcome, COUNT(*) n FROM fetch_record GROUP BY outcome')}
    sb = {
        'new_valid_sources': {'count': len(promoted), 'hosts': promoted, 'new_hosts_seen': seen_new},
        'new_targets': {'candidate': tgt.get('candidate', 0), 'promoted_active': tgt.get('active', 0), 'total_discovered': sum(tgt.values())},
        'required_field_coverage': {k: cov[k] for k in ('required_fields', 'filled_initial', 'coverage_initial', 'gaps_resolved_with_evidence', 'coverage_now')},
        'gap_resolution': {'gaps_total': g_tot, 'resolved_with_evidence': g_res, 'rate': round(g_res / g_tot, 4) if g_tot else None, 'open_by_class': g_cls},
        'change_latency_hours': {'changes_detected': changes, 'with_latency': len(lat), 'median': statistics.median(lat) if lat else None, 'max': max(lat) if lat else None},
        'cost': {'units_spent': round(spent, 1), 'by_pool': by_pool, 'valid_items': valid_items, 'units_per_valid_item': round(spent / valid_items, 3) if valid_items else None,
                 'units_per_new_valid_source': round(spent / len(promoted), 1) if promoted else None},
        'fetch_outcomes': fetch,
        'official_profile': _official(conn),
        'extractions': {r['epistemic']: r['n'] for r in conn.execute('SELECT epistemic, COUNT(*) n FROM extraction GROUP BY epistemic')},
    }
    gp = gold_paths if gold_paths is not None else [p for p in (GOLD_SYNTH, os.path.join(ROOT, 'data_store', 'gold', 'gold_real.json'), os.path.join(ROOT, 'data_store', 'gold', 'gold_heldout.json')) if os.path.exists(p)]
    if gp:
        from .gold import run_file
        sb['accuracy'] = {os.path.basename(p): {k: v for k, v in run_file(p).items() if k not in ('errors', 'by_label')} for p in gp}
    return sb


def _official(conn) -> dict:
    try:
        sites = {r['basis']: r['n'] for r in conn.execute('SELECT basis, COUNT(DISTINCT target_id) n FROM official_site WHERE verified=1 GROUP BY basis')}
        facts = {f"{r['field']}:{r['epistemic']}": r['n'] for r in conn.execute('SELECT field, epistemic, COUNT(*) n FROM profile_fact GROUP BY field, epistemic')}
        by_fact = conn.execute('SELECT COUNT(*) FROM gap WHERE resolved_by_fact IS NOT NULL').fetchone()[0]
        probes = {r['s']: r['n'] for r in conn.execute("""SELECT CASE WHEN last_error LIKE 'probe_rejected%' THEN 'rejected' WHEN last_error LIKE 'probe_unreachable%' THEN 'unreachable'
                                                           WHEN state='done' THEN 'verified' ELSE state END s, COUNT(*) n FROM crawl_job WHERE query LIKE 'probe:%' GROUP BY s""")}
        booking = {r['outcome']: r['n'] for r in conn.execute("""SELECT f.outcome, COUNT(*) n FROM fetch_record f JOIN crawl_job j ON j.job_id=f.job_id WHERE j.query='booking' GROUP BY f.outcome""")}
    except Exception:  # noqa: BLE001  (DB before migration 0004)
        return {}
    return {'verified_sites_by_basis': sites, 'profile_facts': facts, 'gaps_resolved_by_profile_fact': by_fact, 'probe_jobs': probes, 'booking_fetches': booking}


# USD per million input, output and cached-input tokens. Qwen uses the G1 design
# estimate for the Token Plan route. Other routes are unpriced until configured.
RATE_VERSION = 'global-genome-2026-11-20'
MODEL_RATES = {('qwen', 'qwen4-max-preview'): (1.50, 4.50, 1.50)}


def record_first(conn, kind, entity_id, at):
    conn.execute("""INSERT INTO outcome_first(kind,entity_id,first_at) VALUES (?,?,?)
        ON CONFLICT(kind,entity_id) DO UPDATE SET first_at=min(first_at,excluded.first_at)""",
        (kind, entity_id, at))


def record_career_outcomes(conn, fetch_id, version, stamp):
    from .lineage_identity import canonical, redirects
    from .site_projection import _career_rows, _stint_groups, _verified

    chef_redirects = redirects(conn, 'chef')
    jobs = {}
    for row in conn.execute("""SELECT DISTINCT chef_id,branch_id FROM career_assertion
        WHERE fetch_id=? AND extractor_version=? AND state='source_claim'""", (fetch_id, version)):
        chef = canonical(chef_redirects, row['chef_id'])
        record_first(conn, 'resolved_chef', chef, stamp)
        jobs.setdefault(row['branch_id'], set()).add(chef)
    for branch, chefs in jobs.items():
        candidates = {chef: [] for chef in chefs}
        for row in _career_rows(conn, branch):
            chef = canonical(chef_redirects, row['chef_id'])
            if chef in candidates:
                candidates[chef].append(row)
        for chef, rows in candidates.items():
            if any(_verified(stint) for stint in _stint_groups(rows)):
                record_first(conn, 'verified_link', chef + ':' + branch, stamp)


def record_publication(conn, seed):
    """Recover immutable first transitions from the committed seed."""
    from kg_daemon.ontology import link_resolved_chef, sync_seed_geography
    from .targets import tid

    sync_seed_geography(conn, seed)

    for row in seed.get('restaurants', []):
        record = row.get('publication')
        if not record:
            continue
        chef = link_resolved_chef(conn, record['chef_target_id'], record['published_at'])
        record_first(conn, 'resolved_chef', chef, record['published_at'])
        if row.get('lineage_status') != 'verified':
            continue
        verified = record.get('verified_at')
        if not verified and record.get('upgrade_review_id'):
            existing = conn.execute('SELECT resolved_at FROM review_queue WHERE review_id=?',
                                    (record['upgrade_review_id'],)).fetchone()
            verified = existing[0] if existing else None
        if not verified and record.get('creation_gate') == 'verified':
            verified = record['published_at']
        if verified:
            origin = next((k for k in seed.get('kitchens', []) if k['id'] == row['kitchen']), None)
            target = tid('restaurant', origin['name']) if origin else None
            branch = conn.execute('SELECT branch_id FROM target_entity WHERE target_id=?', (target,)).fetchone()
            employer = branch[0] if branch and branch[0] else row['kitchen']
            record_first(conn, 'verified_link', chef + ':' + employer, verified)
    for link in seed.get('ai_promotion', {}).get('links', {}).values():
        day = link.get('created') or link.get('upgraded')
        if not day:
            continue
        stamp = day + 'T00:00:00+09:00'
        record_first(conn, 'resolved_chef', link['chef'], stamp)
        if link['gate'] == 'verified':
            verified = (link.get('upgraded') or day) + 'T00:00:00+09:00'
            record_first(conn, 'verified_link', link['chef'] + ':' + link['branch'], verified)
    conn.commit()


def daily_summary(conn, *, now=None, rule_path=GOLD_SYNTH, heldout_path=None, seed_path=None):
    import datetime as dt
    from zoneinfo import ZoneInfo
    from .gold import daily_accuracy

    kst = ZoneInfo('Asia/Seoul')
    end = (now or dt.datetime.now(kst)).astimezone(kst).replace(hour=0, minute=0, second=0, microsecond=0)
    day = (end - dt.timedelta(days=1)).date().isoformat()
    start_utc, end_utc = ((value.astimezone(dt.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'))
                          for value in (end - dt.timedelta(days=1), end))
    if seed_path:
        with open(seed_path, encoding='utf-8') as f:
            record_publication(conn, json.load(f))
    # Employment and site publication share the same chef-to-branch identity.
    conn.execute('''INSERT INTO outcome_first(kind,entity_id,first_at)
        SELECT 'verified_link', e.chef_id || ':' || e.branch_id, MIN(h.changed_at)
        FROM employment e JOIN claim_status_history h USING(claim_id)
        WHERE e.branch_id IS NOT NULL AND h.to_status='verified'
        GROUP BY e.chef_id,e.branch_id
        ON CONFLICT(kind,entity_id) DO UPDATE SET first_at=MIN(first_at,excluded.first_at)''')
    conn.commit()
    counts = dict(conn.execute("""SELECT kind,COUNT(*) FROM outcome_first
        WHERE julianday(first_at)>=julianday(?) AND julianday(first_at)<julianday(?) GROUP BY kind""",
        (start_utc, end_utc)))
    models = []
    for r in conn.execute("""WITH attribution AS (
          SELECT *, COALESCE(provider,CASE WHEN model LIKE 'qwen%' THEN 'qwen'
             WHEN model LIKE 'deepseek%' THEN 'deepseek' ELSE 'unknown' END) AS route
          FROM ai_request
        )
        SELECT route AS provider,a.model,
          SUM(a.day=?) AS requests,
          SUM(a.day=? AND (prompt_tokens IS NULL OR completion_tokens IS NULL)) AS missing_usage,
          SUM(CASE WHEN a.day=? THEN COALESCE(prompt_tokens,0) ELSE 0 END) AS input_tokens,
          SUM(CASE WHEN a.day=? THEN COALESCE(completion_tokens,0) ELSE 0 END) AS output_tokens,
          SUM(CASE WHEN a.day=? THEN COALESCE(cache_tokens,0) ELSE 0 END) AS cache_input_tokens,
          SUM(COALESCE(c.hits,0)) AS cache_hits
        FROM attribution a LEFT JOIN ai_cache_day c ON a.request_key=c.request_key AND c.day=?
        WHERE a.day=? OR c.hits IS NOT NULL GROUP BY route,a.model ORDER BY route,a.model""", (day,) * 7):
        row = dict(r)
        rates = MODEL_RATES.get((r['provider'], r['model']))
        if not r['requests']:
            dollars = 0
        elif not rates or r['missing_usage']:
            dollars = 'unknown'
        else:
            uncached = r['input_tokens'] - r['cache_input_tokens']
            dollars = round((uncached * rates[0] + r['output_tokens'] * rates[1] + r['cache_input_tokens'] * rates[2]) / 1_000_000, 8)
        models.append({**row, 'estimated_usd': dollars})
    total = 'unknown' if any(r['estimated_usd'] == 'unknown' for r in models) else sum(r['estimated_usd'] for r in models)

    def per(kind):
        n = counts.get(kind, 0)
        return 'n/a' if n == 0 else total if total == 'unknown' else round(total / n, 8)

    docs = conn.execute("""SELECT COUNT(*) FROM fetch_record WHERE outcome='ok' AND method IN ('html','jsonld','browser')
        AND fetched_at>=? AND fetched_at<?""", (start_utc, end_utc)).fetchone()[0]
    traffic = {r['route']: {k: r[k] for k in ('requests', 'bytes_in', 'bytes_out')}
               for r in conn.execute('SELECT * FROM web_traffic_day WHERE day=? ORDER BY route', (day,))}
    requests = conn.execute('SELECT attempts,robots FROM web_daily_fetch WHERE day=?', (day,)).fetchone()
    candidates = conn.execute('SELECT COUNT(*) FROM web_daily_url WHERE day=?', (day,)).fetchone()[0]
    return {'day_kst': day, 'rates': RATE_VERSION, 'models': models, 'estimated_usd': total,
            'newly_resolved_chefs': counts.get('resolved_chef', 0), 'verified_career_links': counts.get('verified_link', 0),
            'usd_per_newly_resolved_chef': per('resolved_chef'), 'usd_per_verified_career_link': per('verified_link'),
            'successful_html_documents': docs, 'transport': traffic,
            'web_dispatches': {'content': requests[0] if requests else 0, 'robots': requests[1] if requests else 0,
                               'distinct_candidates': candidates},
            'accuracy': daily_accuracy(conn, rule_path, heldout_path)}
