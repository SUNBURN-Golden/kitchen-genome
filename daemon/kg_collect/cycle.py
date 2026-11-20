"""The collection cycle as a staged, checkpointed, resumable Daemon run (lock 'kg-collect'):
   plan -> fetch -> extract -> evaluate -> gaps -> apply -> quality
raw (fetch_record + data_store/raw, outside git) -> extraction labels -> claims/service data (apply). Each layer is a separate stage, so a
broken extractor never corrupts raw, and re-extraction with a new extractor_version re-labels the same raw."""
import datetime as dt
import json
import os
import re
import urllib.parse
from pathlib import Path

from kg_daemon import runner
from kg_daemon.db import DEFAULT_DB, ROOT, connect, migrate, new_id, utcnow

from . import EXTRACTOR_VERSION, apply as apply_mod, booking, discover, evaluate, gaps, official, review, scheduler, recheck, site_publish
from .fetch import Doc, Fetcher, parse_feed
from .htmlx import parse_html
from . import net as net_mod
from .limits import DailyLimit
from .net import PoliteNet, host_of
from .pipeline import check_extractor_health, fetch_regression_alert, official_hosts, process_doc, reconcile_host_yield_alerts
from .rawstore import RawStore
from .resolve import promote_candidates
from .targets import Gazetteer, load_seed_targets, tid

STAGES = ('plan', 'fetch', 'extract', 'evaluate', 'gaps', 'apply', 'quality')
UNIT_COSTS = {'http': 1.0, 'ocr_page': 2.0, 'browser': 4.0, 'llm_tokens_k': 0.5}


class Bot:
    """Collaborators. Everything the network touches is injectable (tests use a fixture server / fake clock)."""

    def __init__(self, conn, data_dir=None, net=None, browser=None, seeds=None, model_hook=None, today=None, ocr=True,
                 units=60, registry_path=None, seeds_path=None, deepseek=False, seed_path=None, clock=None):
        self.conn = conn
        self.data_dir = data_dir or os.path.join(ROOT, 'data_store')
        self.raw = RawStore(os.path.join(self.data_dir, 'raw'))
        self.net = net or PoliteNet(conn=conn)
        self.browser = browser
        self.seeds = seeds or discover.load_seeds(seeds_path or discover.SEEDS_PATH)
        self.deepseek = deepseek
        self.ai = None
        self.model_hook = model_hook
        self.clock = clock or (lambda: dt.datetime.now(dt.timezone.utc))
        self.today = today or self.clock().astimezone(site_publish.KST).date()
        self.seed_path = Path(seed_path or Path(ROOT) / 'data' / 'seed.json')
        self.ocr = ocr
        self.units = units
        self.registry_path = registry_path or evaluate.DISCOVERED_REGISTRY
        self.initial = discover.initial_hosts()
        net_mod.SEARCH_HOSTS.add(host_of(self.seeds['keyword_search']['endpoint'].format(q='x')))
        self.job_hint_dirs = {d['url']: d for d in self.seeds.get('directories', [])}

    def gaz(self) -> Gazetteer:
        return Gazetteer(self.conn)


def init_db(conn, seed: dict | None = None, service_seed: bool = True) -> dict:
    """migrate + (optional) Phase-1 seed migration + target list from the seed + required-field gaps."""
    applied = migrate(conn)
    seed = seed or json.load(open(os.path.join(ROOT, 'data', 'seed.json'), encoding='utf-8'))
    out = {'migrations': applied}
    if service_seed and conn.execute('SELECT COUNT(*) FROM chef').fetchone()[0] == 0:
        from kg_daemon.seed_migration import migrate_seed
        migrate_seed(conn, seed)
    out['targets'] = load_seed_targets(conn, seed)
    out['gaps_opened'] = gaps.open_gaps(conn)
    return out


def _tier_of(conn):
    cache: dict = {}
    off = official_hosts(conn)

    def f(host):
        if host not in cache:
            if host in off:
                cache[host] = 'official'
            else:
                r = conn.execute('SELECT tier_guess, state FROM domain_profile WHERE host=?', (host,)).fetchone()
                if discover.is_search_host(host) or (r and r['state'] in ('deprioritized', 'blocked')):
                    cache[host] = 'search_lead'                      # a lead generator, or a source the evaluator already distrusts
                else:                                                  # an article page on a host we are still trialling is still a 'claim' (lowest tier; never verified by itself)
                    cache[host] = r['tier_guess'] if r and r['tier_guess'] in ('official', 'press', 'specialist_blog', 'event_notice') else 'press'
        return cache[host]
    return f


# ───────────────────────── stage: plan ─────────────────────────
def stage_plan(ctx):
    bot: Bot = ctx.env['bot']
    conn, seeds = bot.conn, bot.seeds
    if bot.deepseek and bot.ai is None:
        from .deepseek import Adapters
        bot.ai = Adapters.for_meter(ctx.cost)
    if bot.ai:
        bot.ai.client.budget.meter = ctx.cost
    caps = {**seeds['caps']}
    now_dt = bot.clock()
    seed = json.loads(bot.seed_path.read_text(encoding='utf-8'))
    load_seed_targets(conn, seed)
    for kitchen in seed.get('kitchens', []):
        names = seeds.get('mother_kitchens', {}).get(kitchen['id'], {})
        for lang in ('ko', 'en'):
            conn.executemany('INSERT OR IGNORE INTO target_alias(target_id,alias,lang) VALUES (?,?,?)',
                             [(tid('restaurant', kitchen['name']), alias, lang) for alias in names.get(lang, [])])
    corpus = site_publish.Corpus(conn, bot.raw, seeds, now_dt)
    qualified = corpus.qualified_targets()
    entity_targets = discover.eligible_entity_targets(conn, qualified)
    mother_counts, due_mother, mother_count = discover.reconcile_mother_jobs(conn, seed, bot.today, seeds, caps)
    new = {'directory_feed': 0, 'keyword': 0, 'entity_status': 0, 'official_roots': 0, 'gap': 0, 'mother_kitchen': mother_count}
    for j in discover.directory_and_feed_jobs(seeds):
        meta = j.pop('meta', None) or {}
        if meta.get('policy') == 'manual_only':
            discover.enqueue(conn, **j, caps=caps, state='skipped', error='manual_only: robots/ToS forbid automated access (inaccessible, tracked for a human)')
        else:
            new['directory_feed'] += discover.enqueue(conn, **j, caps=caps)
    for j in discover.entity_status_jobs(conn, seeds, qualified, contexts=corpus.entity_contexts()):
        new['entity_status'] += discover.enqueue(conn, **j, caps=caps)
    for j in discover.keyword_jobs(conn, seeds, today=bot.today):
        new['keyword'] += discover.enqueue(conn, **j, caps=caps)
    for j in discover.seed_official_site_jobs(conn):
        new['official_roots'] += discover.enqueue(conn, **j, caps=caps)
    new['official_probes'] = sum(discover.enqueue(conn, **j, caps=caps) for j in discover.official_probe_jobs(conn, limit=caps.get('max_official_probes_per_cycle', 40)))
    gaps.classify_gaps(conn)
    for j in gaps.gap_jobs(conn, limit=60):
        g = {k: v for k, v in j.items() if k not in ('field', 'absent', 'attempts')}
        g['relevance'] = 0.7
        g['url'] = discover.search_url(seeds, g['query'])
        new['gap'] += discover.enqueue(conn, **g, caps=caps)
    if bot.ai:
        new['deepseek_gap'] = 0
        for j in bot.ai.plan(conn):
            j['url'] = discover.search_url(seeds, j['query'])
            new['deepseek_gap'] += discover.enqueue(conn, **j, caps=caps)
    conn.commit()
    recheck.ensure_schedule(conn, seed)
    recheck.reconcile(conn)
    if bot.ai and bot.ai.client.status == 'enabled':
        if not bot.ai.client.check_balance():
            new['monthly_rechecks'] = recheck.admit(conn, seeds, bot.ai.client.budget)
    requeued = scheduler.requeue_due(conn, now_dt, mother_counts=mother_counts)
    model_allowance = 0
    if bot.ai and bot.ai.client.status == 'enabled':
        budget = bot.ai.client.budget
        daily_used = conn.execute('SELECT COALESCE(SUM(units),0) FROM ai_request WHERE day=?', (budget.day(),)).fetchone()[0]
        model_allowance = max(0, budget.daily_cap - daily_used)
    available_units = min(bot.units, max(0, ctx.cost.cap - ctx.cost.used - model_allowance)) if bot.ai and bot.ai.client.status == 'enabled' else bot.units
    pl = scheduler.plan(conn, available_units, now_dt, mother_counts=mother_counts, due_mother=due_mother,
                        query_caps=seeds['keyword_search'].get('daily_allowances'), entity_targets=entity_targets)
    chosen = [jid for p in ('discovery', 'backfill', 'refresh') for jid in pl['chosen'][p]]
    pri = {r['job_id']: r['priority'] for r in conn.execute('SELECT job_id, priority FROM crawl_job')}
    reserved = {jid: i for i, jid in enumerate(pl['reserved_queries'])}
    chosen.sort(key=lambda j: (reserved.get(j, len(reserved)), -pri.get(j, 0)))
    return runner.StageResult({'new_jobs': new, 'requeued': requeued, 'budgets': pl['budgets'], 'queued_by_pool': pl['queued_by_pool'],
                               'chosen_counts': {p: len(v) for p, v in pl['chosen'].items()}, 'order': chosen, 'mother_counts': mother_counts,
                               'pool_of': {j: next(p for p in pl['chosen'] if j in pl['chosen'][p]) for j in chosen}})


# ───────────────────────── stage: fetch ─────────────────────────
class PoolSkip(Exception):
    pass


def _finish_job(conn, jid, state, err=None):
    conn.execute('UPDATE crawl_job SET state=?, last_error=?, finished_at=? WHERE job_id=?', (state, err, utcnow(), jid))


def _host_feed_jobs(conn, bot, d, job, caps) -> int:
    """A NEW domain: look for its own feed (<link rel=alternate>) and robots 'Sitemap:' lines -> more articles from the same host = a real trial sample."""
    from .htmlx import feed_links
    urls = []
    try:
        urls += feed_links(bot.raw.read(conn.execute('SELECT raw_ref FROM fetch_record WHERE fetch_id=?', (d.fetch_id,)).fetchone()['raw_ref']).decode('utf-8', 'ignore'), d.url)
        urls += bot.net.site_maps(d.url)[:2]
    except Exception:  # noqa: BLE001
        pass
    n = 0
    for u in urls[:3]:
        if host_of(u) == d.host and discover.enqueue(conn, path='sitemap' if 'sitemap' in u.lower() else 'feed', pool='discovery', url=u, depth=job['depth'], parent=job['job_id'], relevance=0.7, caps=caps):
            n += 1
    return n


def _article_site_leads(conn, d, job, gaz, seeds) -> int:
    """A press article mentions restaurant X and links a host containing X's slug -> probe that root as X's own site (basis article_link)."""
    n = 0
    press = seeds.get('publication', {}).get('press_hosts', {})
    rest_ids = {m['target_id'] for m in gaz.find(d.text or '') if m.get('kind') == 'restaurant' and m.get('target_id')}
    for tid in rest_ids:
        t = conn.execute('SELECT * FROM target WHERE target_id=?', (tid,)).fetchone()
        if not t:
            continue
        if t['origin'] == 'discovered' and not any(
            (d.host == h or d.host.endswith('.' + h)) and p['scope'] in ('restaurant_reporting', 'original_food_reporting')
            for h, p in press.items()
        ):
            continue
        for u in official.article_link_candidates(d.links, d.text, dict(t), banned=net_mod.BANNED):
            n += discover.enqueue(conn, path='official_chain', pool='discovery', url=u, query='probe:article_link', target_id=tid, depth=job['depth'] + 1,
                                  parent=job['job_id'], relevance=0.7, caps=seeds['caps'])
    return n


def stage_fetch(ctx):
    bot: Bot = ctx.env['bot']
    conn = bot.conn
    plan = ctx.prev['plan']
    done = set(ctx.cursor.get('done', []))
    state = {'pool': None, 'job': None}

    def charge(kind, units=1.0):
        ctx.cost.charge(kind, units)                        # may raise CostCapExceeded -> daemon pauses this run (resumable)
        conn.execute('INSERT INTO budget_ledger(cycle_id,pool,kind,units,job_id,at) VALUES (?,?,?,?,?,?)', (ctx.run_id, state['pool'], kind, UNIT_COSTS.get(kind, 1.0) * units, state['job'], utcnow()))
        conn.commit()

    fetcher = Fetcher(conn, bot.net, bot.raw, charge=charge, browser=None if bot.net.conn is not None else bot.browser, ocr=bot.ocr)
    gaz = bot.gaz()
    caps = bot.seeds['caps']
    stats = {'jobs_run': 0, 'ok_docs': 0, 'failed_docs': 0, 'blocked': 0, 'children_queued': 0, 'chain': {'subpages': 0, 'sns_leads': 0, 'booking_leads': 0, 'attachments': 0}, 'deferred_trial': 0}
    dispatches = stats['rss_queries'] = dict.fromkeys(discover.QUERY_CAPS, 0)
    for jid in plan['order']:
        if jid in done:
            continue
        job = conn.execute('SELECT * FROM crawl_job WHERE job_id=?', (jid,)).fetchone()
        if job is None or job['state'] != 'queued':
            continue
        if job['depth'] > min(2, caps.get('max_depth', 2)):
            _finish_job(conn, jid, 'dropped', 'owner_depth_cap')
            conn.commit()
            continue
        state['pool'], state['job'] = plan['pool_of'][jid], jid
        host = host_of(job['url'] or '')
        prof = conn.execute('SELECT * FROM domain_profile WHERE host=?', (host,)).fetchone() if host else None
        if prof and prof['state'] == 'blocked':
            _finish_job(conn, jid, 'blocked', 'domain blocked by evaluator')
            continue
        if prof and not prof['in_initial_registry'] and prof['state'] == 'trial' and conn.execute('SELECT COUNT(*) FROM fetch_record WHERE host=?', (host,)).fetchone()[0] >= evaluate.TRIAL_SAMPLE * 2:
            stats['deferred_trial'] += 1
            continue
        if host and not discover.is_search_host(host):
            evaluate.ensure_profile(conn, host, in_registry=host in bot.initial)
        if job['path'] == 'attachment' and not job['url']:
            _finish_job(conn, jid, 'dropped', 'no url')
            continue
        allow_browser = True if bot.job_hint_dirs.get(job['url'], {}).get('browser') else None
        max_pages = caps['pagination_max_pages'] if job['path'] in ('directory',) else 1
        family = discover.query_family(job, plan.get('mother_counts', {}))
        if family:
            if isinstance(ctx.cost, runner.CostMeter) and ctx.cost.used + UNIT_COSTS['http'] > ctx.cost.cap:
                ctx.cost.charge('http')
            if not discover.claim_query(conn, job, family, bot.clock(), bot.seeds['keyword_search'].get('daily_allowances')):
                continue
            dispatches[family] += 1
        try:
            docs = fetcher.fetch(job['url'], job_id=jid, allow_browser=allow_browser, max_pages=max_pages)
        except DailyLimit:
            stats['deferred_daily_cap'] = True
            break
        stats['jobs_run'] += 1
        oks = [d for d in docs if d.ok]
        probe = (job['query'] or '').startswith('probe:')
        if probe:
            stats.setdefault('probes', {'tried': 0, 'verified': 0, 'rejected': 0, 'unreachable': 0})['tried'] += 1
            t = conn.execute('SELECT * FROM target WHERE target_id=?', (job['target_id'],)).fetchone()
            good = None
            for d in oks:
                ok_v, why = official.verify_site(d.text, getattr(d, 'title', '') or '', d.url, dict(t))
                if ok_v:
                    good = (d, why)
                    break
            if not oks:
                stats['probes']['unreachable'] += 1
                conn.execute("UPDATE crawl_job SET state='failed', attempts=3, last_error=?, finished_at=? WHERE job_id=?", (f'probe_unreachable: {docs[-1].outcome}', utcnow(), jid))
                conn.commit(); done.add(jid); ctx.save_cursor({'done': sorted(done)})
                continue
            if not good:
                stats['probes']['rejected'] += 1
                _finish_job(conn, jid, 'dropped', f'probe_rejected: {why}')
                conn.commit(); done.add(jid); ctx.save_cursor({'done': sorted(done)})
                continue
            stats['probes']['verified'] += 1
            official.register_site(conn, job['target_id'], good[0].url, job['query'].split(':', 1)[1], True, good[1], utcnow())
        if oks:
            stats['ok_docs'] += len(oks)
            if host and not discover.is_search_host(host):
                conn.execute('UPDATE domain_profile SET fail_streak=0 WHERE host=?', (host,))
            jd = dict(job)
            for d in oks:
                if d.items:
                    lookback = discover.MOTHER_LOOKBACK if family == 'mother' else 120
                    r = discover.expand_items(conn, d, jd, gaz, caps, lookback_days=lookback, today=bot.today)
                    stats['children_queued'] += r['queued']
                if d.method in ('html', 'jsonld', 'browser') and d.host not in bot.initial and not discover.is_search_host(d.host):
                    stats['host_feeds'] = stats.get('host_feeds', 0) + _host_feed_jobs(conn, bot, d, jd, caps)
                if job['path'] == 'directory' and d.links:
                    stats['children_queued'] += discover.directory_link_jobs(conn, d, jd, bot.seeds, caps)
                if d.links and d.method in ('html', 'jsonld', 'browser') and job['path'] != 'official_chain':
                    stats['article_site_leads'] = stats.get('article_site_leads', 0) + _article_site_leads(conn, d, jd, gaz, bot.seeds)
                if d.links or d.images:
                    off = official_hosts(conn) | {r['host'] for r in conn.execute('SELECT host FROM official_site WHERE verified=1')}
                    if job['path'] == 'official_chain' or d.host in off:
                        r = discover.official_chain_jobs(conn, d, jd, bot.seeds, caps)
                        for k, v in r.items():
                            stats['chain'][k] += v
            _finish_job(conn, jid, 'done')
        else:
            stats['failed_docs'] += 1
            last = docs[-1]
            reg = fetch_regression_alert(conn, last)
            if reg:
                stats.setdefault('alerts', []).append(reg)
            fatal = last.outcome in ('fail_blocked', 'fail_robots', 'fail_banned', 'fail_login_page')
            if job['path'] in ('keyword_search', 'reverse_gap', 'entity_followup') and last.error == 'feed_without_items':
                _finish_job(conn, jid, 'done', 'no_results')       # zero search hits is an ANSWER (counts toward absent_in_source), not a failure
            else:
                if host and not discover.is_search_host(host):
                    conn.execute('UPDATE domain_profile SET fail_streak=fail_streak+1 WHERE host=?', (host,))
                if fatal:
                    stats['blocked'] += 1
                    _finish_job(conn, jid, 'blocked', f'{last.outcome}: {last.error}')
                else:
                    att = job['attempts'] + 1
                    conn.execute('UPDATE crawl_job SET attempts=?, last_error=?, state=? WHERE job_id=?', (att, f'{last.outcome}: {last.error}', 'failed' if att >= 3 else 'queued', jid))
        recheck.link_children(conn, jid)
        if job['gap_id']:
            gaps.bump_attempts(conn, job['gap_id'])
        conn.commit()
        done.add(jid)
        ctx.save_cursor({'done': sorted(done)})
    stats['requests'] = bot.net.request_count
    return runner.StageResult(stats)


# ───────────────────────── stage: extract ─────────────────────────
def load_doc(conn, raw: RawStore, f) -> Doc:
    text = raw.read_text(f['text_ref']) if f['text_ref'] else ''
    items, links, images = [], [], []
    html, jsonld, title = '', [], ''
    if f['method'] in ('feed', 'sitemap') and f['raw_ref']:
        items = parse_feed(raw.read(f['raw_ref']), f['url'])
    elif f['method'] in ('html', 'jsonld', 'browser') and f['raw_ref'] and f['raw_ref'].endswith('.html'):
        html = raw.read(f['raw_ref']).decode('utf-8', 'ignore')
        p = parse_html(html, f['url'])
        links, images = p['links'], p['images']
        jsonld, title = p['jsonld'], p['title'] or ''
    d = Doc(f['fetch_id'], f['url'], f['canonical_url'], f['host'], f['method'], f['outcome'], text=text, published_at=f['published_at'], links=links, items=items, images=images,
            content_hash=f['content_hash'], error=f['error'], page_no=f['page_no'] or 1)
    d.html, d.jsonld, d.title = html, jsonld, title
    return d


def profile_facts_for(conn, doc: Doc, target_row, now: str) -> int:
    """Restaurant's own page / allowed booking page -> field-level profile facts (official.py / booking.py)."""
    if not target_row or target_row['kind'] != 'restaurant' or doc.method in ('feed', 'sitemap'):
        return 0
    tgt = {'target_id': target_row['target_id'], 'name_ko': target_row['name_ko'], 'name_en': target_row['name_en']}
    if booking.is_booking_host(doc.host):
        facts = booking.extract_booking(getattr(doc, 'html', ''), doc.text, getattr(doc, 'jsonld', []), target=tgt, host=doc.host, published_at=doc.published_at,
                                        content_hash=doc.content_hash or '', title=getattr(doc, 'title', ''))
    else:
        basis = official.site_basis_for(conn, tgt['target_id'], doc.host, now)
        if not basis:
            return 0
        facts = official.extract_profile(doc.text, getattr(doc, 'jsonld', []), target=tgt, host=doc.host, source_kind='official_site', site_basis=basis,
                                         published_at=doc.published_at, content_hash=doc.content_hash or '', title=getattr(doc, 'title', ''))
    return official.store(conn, facts, fetch_id=doc.fetch_id, canonical_url=doc.canonical_url, host=doc.host, now=now)


def stage_extract(ctx):
    bot: Bot = ctx.env['bot']
    conn = bot.conn
    gaz = bot.gaz()
    if bot.deepseek:
        from .deepseek import Adapters
        bot.ai = bot.ai or Adapters.for_meter(ctx.cost)
        bot.ai.client.budget.meter = ctx.cost
    tier_of = _tier_of(conn)
    # Highest priority first (heavy collector scores; unscored rows weigh 0.75) so the fixed DeepSeek budget reaches the most promising pages.
    rows = conn.execute("""SELECT f.* FROM fetch_record f LEFT JOIN fetch_priority p ON p.fetch_id=f.fetch_id
                           WHERE f.outcome='ok' AND (f.extracted_at IS NULL OR f.extractor_version<>?)
                           ORDER BY COALESCE(p.score, 0.75) DESC, f.fetched_at, f.rowid""", (EXTRACTOR_VERSION,)).fetchall()
    stats = {'docs': 0, 'new_extractions': 0, 'labels': {}, 'alerts': []}
    for f in rows:
        attempt = recheck.active_for_fetch(conn, f)
        if attempt and (not bot.ai or bot.ai.client.status != 'enabled'):
            continue
        try:
            doc = load_doc(conn, bot.raw, f)
        except (FileNotFoundError, UnicodeError):
            stats['missing_snapshots'] = stats.get('missing_snapshots', 0) + 1
            continue
        job = conn.execute('SELECT target_id, url FROM crawl_job WHERE job_id=?', (f['job_id'],)).fetchone() if f['job_id'] else None
        jctx = None
        if job and job['target_id']:
            t = conn.execute('SELECT * FROM target WHERE target_id=?', (job['target_id'],)).fetchone()
            jctx = {t['kind']: {'name_ko': t['name_ko'], 'target_id': t['target_id']}} if t and t['kind'] in ('chef', 'restaurant') else None
        if bot.ai:
            bot.ai.defer_extract = False
        r = process_doc(conn, doc, gaz, tier=tier_of(doc.host), ctx=jctx, via=f['url'], model_hook=bot.model_hook, model_adapter=bot.ai)
        if doc.links and doc.method in ('html', 'browser'):
            original_job = conn.execute('SELECT * FROM crawl_job WHERE job_id=?', (f['job_id'],)).fetchone()
            if original_job and original_job['path'] != 'official_chain' and original_job['depth'] < 2:
                _article_site_leads(conn, doc, original_job, bot.gaz() if r['new'] else gaz, bot.seeds)
        n_all = conn.execute('SELECT COUNT(*) FROM extraction WHERE fetch_id=?', (f['fetch_id'],)).fetchone()[0]
        for a in check_extractor_health(conn, doc, n_all, bot.raw, gaz=gaz):
            stats['alerts'].append(a)
        # A document the model could not afford stays unextracted so a later run can retry it.
        # Deterministic rows already committed are idempotent. Other documents still finish.
        if bot.ai and bot.ai.defer_extract:
            continue
        conn.execute('UPDATE fetch_record SET extracted_at=?, extractor_version=? WHERE fetch_id=?', (utcnow(), EXTRACTOR_VERSION, f['fetch_id']))
        conn.commit()
        stats['docs'] += 1
        stats['new_extractions'] += r['new']
        for k, v in r['labels'].items():
            stats['labels'][k] = stats['labels'].get(k, 0) + v
    stats['host_alerts'] = reconcile_host_yield_alerts(conn, bot.raw, gaz)
    stats['profile'] = profile_scan(conn, bot.raw)
    recheck.reconcile(conn)
    return runner.StageResult(stats)


def profile_scan(conn, raw) -> dict:
    """Read every ok fetch made for a restaurant target with the profile extractor once per OFFICIAL_VERSION."""
    out = {'scanned': 0, 'facts_new': 0}
    rows = conn.execute("""SELECT f.*, j.target_id AS tid FROM fetch_record f JOIN crawl_job j ON j.job_id=f.job_id JOIN target t ON t.target_id=j.target_id
                            WHERE f.outcome='ok' AND t.kind='restaurant' AND f.method NOT IN ('feed','sitemap')
                            AND NOT EXISTS (SELECT 1 FROM restaurant_recheck_job l WHERE l.job_id=f.job_id)
                            AND NOT EXISTS (SELECT 1 FROM profile_scan s WHERE s.fetch_id=f.fetch_id AND s.version=?) ORDER BY f.fetched_at""", (official.OFFICIAL_VERSION,)).fetchall()
    for f in rows:
        t = conn.execute('SELECT * FROM target WHERE target_id=?', (f['tid'],)).fetchone()
        try:
            doc = load_doc(conn, raw, f)
        except (FileNotFoundError, UnicodeError):
            continue
        n = profile_facts_for(conn, doc, t, utcnow())
        conn.execute('INSERT OR IGNORE INTO profile_scan(fetch_id,version,facts,scanned_at) VALUES (?,?,?,?)', (f['fetch_id'], official.OFFICIAL_VERSION, n, utcnow()))
        out['scanned'] += 1
        out['facts_new'] += n
    conn.commit()
    return out


# ───────────────────────── stage: evaluate ─────────────────────────
def stage_evaluate(ctx):
    bot: Bot = ctx.env['bot']
    conn = bot.conn
    gaz, off = bot.gaz(), official_hosts(conn)
    res = []
    for r in conn.execute("SELECT DISTINCT host FROM fetch_record").fetchall():
        h = r['host']
        if discover.is_search_host(h):
            continue
        res.append(evaluate.score_host(conn, h, bot.net, bot.raw, gaz, off, in_initial=h in bot.initial))
    # deprioritised / blocked hosts: drop their queued discovery work; blocked also stops everything
    for h in conn.execute("SELECT host, state FROM domain_profile WHERE state IN ('blocked')").fetchall():
        conn.execute("UPDATE crawl_job SET state='blocked', last_error='domain blocked', finished_at=? WHERE state='queued' AND url LIKE ?", (utcnow(), f"%{h['host']}%"))
    promoted = evaluate.grow_registry(conn, bot.registry_path)
    conn.commit()
    new = [x for x in res if x.get('state') in ('promoted', 'deprioritized', 'blocked') and x.get('host') not in bot.initial]
    return runner.StageResult({'evaluated': len(res), 'decisions': {x['host']: [x['state'], x.get('score')] for x in new}, 'registry_discovered': promoted})


# ───────────────────────── stage: gaps ─────────────────────────
def stage_gaps(ctx):
    bot: Bot = ctx.env['bot']
    conn = bot.conn
    seed = json.loads(bot.seed_path.read_text(encoding='utf-8'))
    reviewed = review.review_candidates(conn)
    corpus = site_publish.Corpus(conn, bot.raw, bot.seeds, utcnow())
    promoted = promote_candidates(conn, corpus.qualified_targets())
    publication = site_publish.prepare(conn, seed, bot.raw, corpus=corpus)
    res = gaps.resolve_gaps(conn)
    cls = gaps.classify_gaps(conn)
    # entity-driven follow-ups from extractions that have no follow-up yet
    depths = {r['fetch_id']: r['depth'] for r in conn.execute("""SELECT f.fetch_id,j.depth FROM fetch_record f
                                                             JOIN crawl_job j ON j.job_id=f.job_id""")}
    made = 0
    seen = set()
    for e, _, ref in corpus.rows():
        if not ref or e['label'] not in ('career', 'employment_claim'):
            continue
        for j in discover.followup_jobs(conn, bot.seeds, [e], parent_depth=depths.get(e['fetch_id'], 0), gaz=corpus.gaz):
            j.pop('source_extraction', None)
            if j['query'] in seen:
                continue
            seen.add(j['query'])
            made += discover.enqueue(conn, **j, caps=bot.seeds['caps'])
    conn.commit()
    return runner.StageResult({'candidates_rejected': [r['name'] for r in reviewed['rejected']], 'candidates_for_review': len(reviewed['queued_for_review']), 'candidates_promoted': promoted, 'gap_resolution': res, 'gap_classes': cls, 'followup_jobs': made, 'publication': publication})


# ───────────────────────── stage: apply / quality ─────────────────────────
def stage_apply(ctx):
    bot: Bot = ctx.env['bot']
    conn = bot.conn
    t = _tier_of(conn)
    return runner.StageResult({'affiliation': apply_mod.apply_affiliation(conn, tier_of=t), 'status': apply_mod.apply_status(conn, tier_of=t)})


def stage_quality(ctx):
    from .scoreboard import scoreboard
    bot: Bot = ctx.env['bot']
    conn = bot.conn
    bad = conn.execute("SELECT COUNT(*) FROM claim WHERE created_by LIKE 'collect:%' AND status IN ('verified','ai_inference')").fetchone()[0]
    if bad:
        raise runner.NonRetryable(f'{bad} bot-created claims carry verified/ai_inference status: bot may only write source_claim/unverified')
    open_alerts = conn.execute("SELECT COUNT(*) FROM extractor_alert WHERE state='open'").fetchone()[0]
    return runner.StageResult({'open_extractor_alerts': open_alerts, 'scoreboard': scoreboard(conn, bot)})


def _logged(fn):
    def w(ctx):
        try:
            return fn(ctx)
        except (runner.CostCapExceeded, runner.NonRetryable):
            raise
        except Exception:
            import sys
            import traceback
            traceback.print_exc(file=sys.stderr)       # the Daemon records the short error; the traceback goes to the log
            raise
    w.__name__ = fn.__name__
    return w


_H = {'plan': stage_plan, 'fetch': stage_fetch, 'extract': stage_extract, 'evaluate': stage_evaluate, 'gaps': stage_gaps, 'apply': stage_apply, 'quality': stage_quality}
HANDLERS = {k: _logged(v) for k, v in _H.items()}


def run_cycle(bot: Bot, resume: bool = True, cost_cap: float | None = None, trigger: str = 'manual') -> dict:
    cfg = {'lock_name': 'kg-collect', 'daemon_name': 'kg-collect', 'cost_cap_units': (bot.units * 1.3 if cost_cap is None else cost_cap), 'unit_costs': UNIT_COSTS, 'max_attempts': 2, 'backoff_base_sec': 0.0, 'backoff_max_sec': 0.0}
    d = runner.Daemon(bot.conn, HANDLERS, cfg, env={'bot': bot}, stages=STAGES, sleep=lambda s: None)
    rep = d.run(trigger=trigger, resume=resume)
    rep['ai'] = bot.ai.client.status if bot.ai else 'disabled'
    if bot.ai:
        rep['ai_stats'] = dict(bot.ai.stats)
    if bot.ai and not bot.ai.client.key:
        rep['ai_reason'] = 'missing_key'
    elif bot.ai and bot.ai.client.stopped:
        rep['ai_reason'] = bot.ai.client.stopped
    if rep.get('run_id'):
        rep['outputs'] = {r['stage']: json.loads(r['output_json']) for r in bot.conn.execute('SELECT stage, output_json FROM stage_checkpoint WHERE run_id=? AND output_json IS NOT NULL', (rep['run_id'],))}
    return rep
