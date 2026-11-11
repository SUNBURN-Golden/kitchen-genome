"""The collection cycle as a staged, checkpointed, resumable Daemon run (lock 'kg-collect'):
   plan -> fetch -> extract -> evaluate -> gaps -> apply -> quality
raw (fetch_record + data_store/raw, outside git) -> extraction labels -> claims/service data (apply). Each layer is a separate stage, so a
broken extractor never corrupts raw, and re-extraction with a new extractor_version re-labels the same raw."""
import datetime as dt
import json
import os
import re
import time
import urllib.parse
from pathlib import Path

from kg_daemon import runner
from kg_daemon.db import DEFAULT_DB, ROOT, connect, migrate, new_id, utcnow

from . import EXTRACTOR_VERSION, apply as apply_mod, booking, discover, evaluate, gaps, official, review, scheduler, recheck, site_publish
from .fetch import Doc, Fetcher, canonicalize_url, parse_feed
from .htmlx import parse_html
from . import global_queue, michelin, net as net_mod
from .guard import Guard
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
                 registry_path=None, seeds_path=None, deepseek=False, seed_path=None, clock=None, deadline=None, fetch_deadline=None,
                 roster_config=None, extract_deadline=None):
        self.conn = conn
        self.data_dir = data_dir or os.path.join(ROOT, 'data_store')
        self.raw = RawStore(os.path.join(self.data_dir, 'raw'))
        self.net = net or PoliteNet(conn=conn)
        self.seeds = seeds or discover.load_seeds(seeds_path or discover.SEEDS_PATH)
        self.deepseek = deepseek
        self.ai = None
        self.model_hook = model_hook
        self.roster_config = roster_config
        self.clock = clock or (lambda: dt.datetime.now(dt.timezone.utc))
        self.today = today or self.clock().astimezone(site_publish.KST).date()
        self.seed_path = Path(seed_path or Path(ROOT) / 'data' / 'seed.json')
        self.ocr = ocr
        self.deadline, self.fetch_deadline = deadline, fetch_deadline or deadline
        # Extraction stops here so evaluate/gaps/apply/quality still fit before the hard deadline (2026-11-26: extract
        # ran to the deadline and the unbounded evaluate stage was killed by the collect timeout).
        self.extract_deadline = extract_deadline or deadline
        self.registry_path = registry_path or evaluate.DISCOVERED_REGISTRY
        self.initial = discover.initial_hosts()
        net_mod.SEARCH_HOSTS.add(host_of(self.seeds['keyword_search']['endpoint'].format(q='x')))

    def expired(self, deadline=None):
        deadline = deadline if deadline is not None else self.deadline
        return deadline is not None and time.monotonic() >= deadline

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
        bot.ai.client.deadline = bot.fetch_deadline
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
    mother_counts, _, mother_count = discover.reconcile_mother_jobs(conn, seed, bot.today, seeds, caps)
    now_iso = now_dt.strftime('%Y-%m-%dT%H:%M:%SZ')
    conn.execute("""UPDATE crawl_job SET state='queued', not_before=NULL
                    WHERE state='blocked' AND not_before<=? AND
                          (last_error LIKE 'fail_blocked: %' OR last_error LIKE 'fail_login_page: %')""", (now_iso,))
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
    new['official_probes'] = sum(discover.enqueue(conn, **j, caps=caps) for j in discover.official_probe_jobs(conn))
    gaps.classify_gaps(conn)
    for j in gaps.gap_jobs(conn):
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
    new['monthly_rechecks'] = recheck.admit(conn, seeds, now=now_dt)
    requeued = scheduler.requeue_due(conn, now_dt, mother_counts=mother_counts)
    pl = scheduler.plan(conn, now_dt, mother_counts=mother_counts, entity_targets=entity_targets)
    return runner.StageResult({'new_jobs': new, 'requeued': requeued, 'queued_by_pool': pl['queued_by_pool'],
                               'chosen_counts': {p: len(v) for p, v in pl['chosen'].items()}, 'order': pl['order'],
                               'mother_counts': mother_counts, 'entity_targets': sorted(entity_targets)})


# ───────────────────────── stage: fetch ─────────────────────────
def _finish_job(conn, jid, state, err=None):
    conn.execute('UPDATE crawl_job SET state=?, last_error=?, finished_at=? WHERE job_id=?', (state, err, utcnow(), jid))


def _host_feed_jobs(conn, bot, d, job, caps) -> int:
    """A NEW domain: look for its own feed (<link rel=alternate>) and robots 'Sitemap:' lines -> more articles from the same host = a real trial sample."""
    from .htmlx import feed_links
    urls = []
    try:
        urls += feed_links(bot.raw.read(conn.execute('SELECT raw_ref FROM fetch_record WHERE fetch_id=?', (d.fetch_id,)).fetchone()['raw_ref']).decode('utf-8', 'ignore'), d.url)
        urls += bot.net.site_maps(d.url)
    except Exception:  # noqa: BLE001
        pass
    n = 0
    for u in urls:
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
    guard = Guard(conn, ctx.run_id, 'collector:fetch')
    lanes = {}
    visited = set()

    def work():
        # Replan after each batch so newly queued children drain in the same run.
        order = plan['order']
        while order and not bot.expired(bot.fetch_deadline):
            yield from (jid for jid in order if jid not in done)
            order = [jid for jid in scheduler.plan(conn, bot.clock(), mother_counts=plan.get('mother_counts'),
                                                  entity_targets=plan.get('entity_targets'))['order'] if jid not in done]
            if order and guard.runnable(order):
                conn.commit()
                break


    def charge(kind, units=1.0):
        ctx.cost.charge(kind, units)
        conn.execute('INSERT INTO budget_ledger(cycle_id,pool,kind,units,job_id,at) VALUES (?,?,?,?,?,?)', (ctx.run_id, state['pool'], kind, UNIT_COSTS.get(kind, 1.0) * units, state['job'], utcnow()))
        conn.commit()

    directory_hints = {d['url']: d for d in bot.seeds.get('directories', [])}
    fetcher = Fetcher(conn, bot.net, bot.raw, charge=charge, ocr=bot.ocr)
    gaz = bot.gaz()
    caps = bot.seeds['caps']
    stats = {'jobs_run': 0, 'ok_docs': 0, 'failed_docs': 0, 'blocked': 0, 'children_queued': 0, 'chain': {'subpages': 0, 'sns_leads': 0, 'booking_leads': 0, 'attachments': 0}}
    if bot.roster_config and not bot.expired(bot.fetch_deadline):
        stats['michelin'] = refresh_roster(bot, ctx.cost)
    dispatches = stats['rss_queries'] = dict.fromkeys(discover.QUERY_FAMILIES, 0)
    def quarantine(job, attempts):
        from kg_daemon.claims import queue_review
        jid = job['job_id']
        _finish_job(conn, jid, 'failed', 'attempts_exhausted')
        queue_review(conn, 'guard_attempts_exhausted', 'crawl_job', jid,
                     {'stage': 'fetch', 'attempts': attempts}, dedupe_key='guard-fetch:' + jid)

    for jid in work():
        if bot.expired(bot.fetch_deadline):
            stats['deferred_deadline'] = True
            break
        done.add(jid)
        job = conn.execute('SELECT * FROM crawl_job WHERE job_id=?', (jid,)).fetchone()
        if job is None or job['state'] != 'queued':
            continue
        state['pool'], state['job'] = job['pool'], jid
        host = host_of(job['url'] or '')
        if host not in lanes:
            lanes[host] = Guard(conn, ctx.run_id, 'collector:fetch:' + host)
        lane = lanes[host]
        if lane.reason:
            continue
        if host and not discover.is_search_host(host):
            evaluate.ensure_profile(conn, host, in_registry=host in bot.initial)
        if not job['url']:
            _finish_job(conn, jid, 'dropped', 'no url')
            continue
        item = lane.item(jid)
        if item and item['reason']:
            quarantine(job, item['attempts'])
            conn.commit()
            continue

        canonical = canonicalize_url(job['url'])
        if canonical in visited:
            continue
        dispatched = False

        def before_send():
            nonlocal dispatched
            if not dispatched:
                dispatched = bool(lane.attempt(jid, previous=job['attempts']))
                conn.commit()
            return dispatched

        family = discover.query_family(job, plan.get('mother_counts', {}))
        if family:
            if not discover.claim_query(conn, job, family, bot.clock()):
                continue
            dispatches[family] += 1
        docs = fetcher.fetch(job['url'], job_id=jid,
                             allow_browser=bool(directory_hints.get(job['url'], {}).get('browser')), before_send=before_send)
        if dispatched:
            visited.add(canonical)
        if docs[-1].error in ('block_end', 'robots_unavailable', 'js_renderer_unavailable') or docs[-1].outcome in (
            'fail_blocked', 'fail_login_page', 'fail_robots', 'fail_banned',
        ):
            if dispatched:
                lane.defer(jid)
        if docs[-1].error == 'block_end':
            if family:
                discover.release_query(conn, job, bot.clock())
            conn.commit()
            if bot.expired(bot.fetch_deadline):
                break
            continue
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
            elif not good:
                stats['probes']['rejected'] += 1
                _finish_job(conn, jid, 'dropped', f'probe_rejected: {why}')
                lane.complete(jid)
                lane.progress(jid)
                conn.commit()
                continue
            else:
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
                if job['path'] == 'directory' and d.host == host:
                    if d.next_url and host_of(d.next_url) == host and not d.unchanged:
                        stats['children_queued'] += discover.enqueue(conn, path='directory', pool=job['pool'], url=d.next_url,
                            target_id=job['target_id'], depth=job['depth'] + 1, parent=jid, relevance=job['relevance'], caps=caps)
                    if d.links:
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
            lane.progress(jid)
            lane.complete(jid)
            guard.progress(jid)
        else:
            stats['failed_docs'] += 1
            last = docs[-1]
            reg = fetch_regression_alert(conn, last)
            if reg:
                stats.setdefault('alerts', []).append(reg)
            fatal = last.outcome in ('fail_blocked', 'fail_robots', 'fail_banned', 'fail_login_page')
            if job['path'] in ('keyword_search', 'reverse_gap', 'entity_followup') and last.error == 'feed_without_items':
                lane.progress(jid)
                lane.complete(jid)
                _finish_job(conn, jid, 'done', 'no_results')       # zero search hits is an ANSWER (counts toward absent_in_source), not a failure
            elif last.error in ('robots_unavailable', 'js_renderer_unavailable'):
                # The job remains available for a later attempt once policy or rendering is available.
                conn.execute('UPDATE crawl_job SET last_error=?, not_before=? WHERE job_id=?',
                             (last.error, (bot.clock() + dt.timedelta(days=1)).strftime('%Y-%m-%dT%H:%M:%SZ'), jid))
            else:
                if host and not discover.is_search_host(host):
                    conn.execute('UPDATE domain_profile SET fail_streak=fail_streak+1 WHERE host=?', (host,))
                if fatal:
                    stats['blocked'] += 1
                    _finish_job(conn, jid, 'blocked', f'{last.outcome}: {last.error}')
                    if last.outcome in ('fail_blocked', 'fail_login_page'):
                        retry = conn.execute('SELECT retry_after_at FROM web_host_guard WHERE host=?', (last.host,)).fetchone()
                        due = bot.clock()
                        if retry and retry[0] is not None and retry[0] > due.timestamp():
                            # Cap conversion at a representable UTC timestamp. The guard keeps the full hold.
                            due = dt.datetime.fromtimestamp(min(retry[0], 253402300799), dt.timezone.utc)
                        conn.execute('UPDATE crawl_job SET not_before=? WHERE job_id=?', (due.strftime('%Y-%m-%dT%H:%M:%SZ'), jid))
                else:
                    lane.failure(last.error or last.outcome)
                    terminal = last.error in ('http_404', 'http_410', 'unsafe_destination')
                    if terminal:
                        lane.complete(jid)
                        _finish_job(conn, jid, 'failed', last.error)
                    else:
                        item = lane.item(jid)
                        if item and item['reason']:
                            quarantine(job, item['attempts'])
                        else:
                            if family:
                                discover.release_query(conn, job, bot.clock())
                            due = (bot.clock() + dt.timedelta(minutes=2)).strftime('%Y-%m-%dT%H:%M:%SZ')
                            conn.execute('UPDATE crawl_job SET attempts=?,last_error=?,not_before=? WHERE job_id=?',
                                         (item['attempts'] if item else job['attempts'], last.error, due, jid))
        recheck.link_children(conn, jid)
        if job['gap_id']:
            gaps.bump_attempts(conn, job['gap_id'])
        conn.commit()
        if hasattr(ctx, 'save_cursor'):
            ctx.save_cursor({'last_committed': jid})
    stats['requests'] = bot.net.request_count
    stats['guard_stops'] = {host: lane.reason for host, lane in lanes.items() if lane.reason}
    if guard.reason:
        stats['guard_stops']['fetch'] = guard.reason
    return runner.StageResult(stats)


# ───────────────────────── stage: extract ─────────────────────────
def load_doc(conn, raw: RawStore, f) -> Doc:
    text = raw.read_text(f['text_ref']) if f['text_ref'] else ''
    items, links, images = [], [], []
    html, jsonld, title, lang = '', [], '', ''
    if f['method'] in ('feed', 'sitemap') and f['raw_ref']:
        items = parse_feed(raw.read(f['raw_ref']), f['url'])
    elif f['method'] in ('html', 'jsonld', 'browser') and f['raw_ref'] and f['raw_ref'].endswith('.html'):
        html = raw.read(f['raw_ref']).decode('utf-8', 'ignore')
        p = parse_html(html, f['url'])
        links, images = p['links'], p['images']
        jsonld, title, lang = p['jsonld'], p['title'] or '', p['language']
    d = Doc(f['fetch_id'], f['url'], f['canonical_url'], f['host'], f['method'], f['outcome'], text=text, published_at=f['published_at'], links=links, items=items, images=images,
            content_hash=f['content_hash'], error=f['error'], page_no=f['page_no'] or 1)
    d.html, d.jsonld, d.title, d.language = html, jsonld, title, lang
    return d


def extract_roster(conn, raw, fetch, gaz, adapter, deadline, focus=()):
    """Shared article extraction. The caller checkpoints completion in its lease."""
    if deadline is not None and time.monotonic() >= deadline:
        return False
    doc = load_doc(conn, raw, fetch)
    if adapter:
        adapter.client.deadline = deadline
        adapter.defer_extract = False
    result = process_doc(conn, doc, gaz, tier=_tier_of(conn)(doc.host), via=fetch['url'], model_adapter=adapter,
                         lineage_focus=focus, lineage_required=True)
    return result['lineage_complete'] and (not adapter or not adapter.defer_extract)


def refresh_roster(bot, meter):
    net, conn = bot.net, bot.conn
    previous_deadline = net.deadline
    remaining = max(0, bot.fetch_deadline - time.monotonic()) if bot.fetch_deadline is not None else 0
    soft_stop = time.monotonic() + remaining / 3
    net.deadline = net.clock() + remaining / 3
    stats = {}
    try:
        michelin.bootstrap(conn, net.clock(), bot.roster_config, bot.seeds)
        roster = michelin.Roster(conn, net, bot.raw, bot.roster_config, bot.seeds)

        def extract(fetch, gaz, focus):
            if bot.deepseek and not bot.ai:
                from .deepseek import Adapters

                bot.ai = Adapters.for_meter(meter)
            return extract_roster(conn, bot.raw, fetch, gaz, bot.ai, soft_stop, focus)

        while time.monotonic() < soft_stop:
            job = global_queue.claim(conn, net.clock(), daily=True)
            if not job:
                break
            try:
                result = roster.work(job, extractor=extract)
            except Exception as exc:
                michelin.record_exception(conn, job, net.clock, exc)
                result = type(exc).__name__
            stats[result] = stats.get(result, 0) + 1
    finally:
        net.deadline = previous_deadline
    return stats


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
        bot.ai.client.deadline = bot.extract_deadline
        bot.ai.client.use_task('web-extract-2')
    tier_of = _tier_of(conn)
    from .lineage import VERSION as lineage_version

    # Highest priority first (heavy collector scores; unscored rows weigh 0.75).
    rows = conn.execute("""SELECT f.* FROM fetch_record f LEFT JOIN fetch_priority p ON p.fetch_id=f.fetch_id
                           WHERE f.outcome='ok' AND (f.extracted_at IS NULL OR f.extractor_version<>? OR
                             (? AND f.text_ref IS NOT NULL AND f.method IN ('html','jsonld','browser','pdf','ocr')
                              AND NOT EXISTS (SELECT 1 FROM career_document d WHERE d.fetch_id=f.fetch_id AND d.extractor_version=?)))
                             AND f.fetcher_version<>?
                           ORDER BY COALESCE(p.score, 0.75) DESC, f.fetched_at, f.rowid""", (EXTRACTOR_VERSION,
                               bool(bot.ai and hasattr(bot.ai, 'extract_lineage') and bot.ai.client.configured),
                               lineage_version, michelin.VERSION)).fetchall()
    stats = {'docs': 0, 'new_extractions': 0, 'labels': {}, 'alerts': [], 'skipped_entertainment': 0}
    for f in rows:
        if bot.expired(bot.extract_deadline):
            stats['deferred_deadline'] = True
            break
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
        stats['skipped_entertainment'] += r.get('skipped_entertainment', 0)
        if doc.links and doc.method in ('html', 'browser') and not (r.get('skipped_entertainment') and not doc.items):
            original_job = conn.execute('SELECT * FROM crawl_job WHERE job_id=?', (f['job_id'],)).fetchone()
            if original_job and original_job['path'] != 'official_chain':
                _article_site_leads(conn, doc, original_job, bot.gaz() if r['new'] else gaz, bot.seeds)
        # A fully skipped entertainment prose doc must not fire per-URL yield_drop/structure_change alerts;
        # feeds keep their checks (partial skips still extract real items). Host coverage stays via reconcile.
        if not (r.get('skipped_entertainment') and not doc.items):
            n_all = conn.execute('SELECT COUNT(*) FROM extraction WHERE fetch_id=?', (f['fetch_id'],)).fetchone()[0]
            for a in check_extractor_health(conn, doc, n_all, bot.raw, gaz=gaz):
                stats['alerts'].append(a)
        # A temporarily unavailable model can resume after deterministic rows commit.
        if not r['lineage_complete'] or bot.ai and bot.ai.defer_extract:
            continue
        conn.execute('UPDATE fetch_record SET extracted_at=?, extractor_version=? WHERE fetch_id=?', (utcnow(), EXTRACTOR_VERSION, f['fetch_id']))
        conn.commit()
        stats['docs'] += 1
        stats['new_extractions'] += r['new']
        for k, v in r['labels'].items():
            stats['labels'][k] = stats['labels'].get(k, 0) + v
    stats['host_alerts'] = reconcile_host_yield_alerts(conn, bot.raw, gaz)
    stats['profile'] = profile_scan(conn, bot.raw, expired=lambda: bot.expired(bot.extract_deadline))
    recheck.reconcile(conn)
    return runner.StageResult(stats)


def profile_scan(conn, raw, expired=lambda: False) -> dict:
    """Read every ok fetch made for a restaurant target with the profile extractor once per OFFICIAL_VERSION."""
    out = {'scanned': 0, 'facts_new': 0}
    rows = conn.execute("""SELECT f.*, j.target_id AS tid FROM fetch_record f JOIN crawl_job j ON j.job_id=f.job_id JOIN target t ON t.target_id=j.target_id
                            WHERE f.outcome='ok' AND t.kind='restaurant' AND f.method NOT IN ('feed','sitemap')
                            AND NOT EXISTS (SELECT 1 FROM restaurant_recheck_job l WHERE l.job_id=f.job_id)
                            AND NOT EXISTS (SELECT 1 FROM profile_scan s WHERE s.fetch_id=f.fetch_id AND s.version=?) ORDER BY f.fetched_at""", (official.OFFICIAL_VERSION,)).fetchall()
    for f in rows:
        if expired():
            break
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
    """Bounded by bot.deadline. A host not reached (or whose robots.txt could not be read in time) keeps its
    previous decision and stays due, so the next cycle picks it up."""
    bot: Bot = ctx.env['bot']
    conn = bot.conn
    gaz, off = bot.gaz(), official_hosts(conn)
    res, deferred = [], 0
    refs = evaluate.RepostRefs(conn, bot.raw)
    previous_net_deadline = getattr(bot.net, 'deadline', None)
    if bot.deadline is not None and previous_net_deadline is not None and hasattr(bot.net, 'clock'):
        # Fetching ended at fetch_deadline; robots.txt reads for scoring may use the evaluate window.
        bot.net.deadline = max(previous_net_deadline, bot.net.clock() + max(0, bot.deadline - time.monotonic()))
    try:
        for r in conn.execute("""SELECT DISTINCT host FROM fetch_record
            WHERE fetcher_version<>? OR extracted_at IS NOT NULL""", (michelin.VERSION,)).fetchall():
            h = r['host']
            if discover.is_search_host(h):
                continue
            if bot.expired():
                deferred += 1
                continue
            out = evaluate.score_host(conn, h, bot.net, bot.raw, gaz, off, in_initial=h in bot.initial, refs=refs, expired=bot.expired)
            if out.get('deferred'):
                deferred += 1
                continue
            res.append(out)
    finally:
        if hasattr(bot.net, 'deadline'):
            bot.net.deadline = previous_net_deadline
    promoted = evaluate.grow_registry(conn, bot.registry_path)
    conn.commit()
    new = [x for x in res if x.get('state') in ('promoted', 'deprioritized', 'blocked') and x.get('host') not in bot.initial]
    out = {'evaluated': len(res), 'decisions': {x['host']: [x['state'], x.get('score')] for x in new}, 'registry_discovered': promoted}
    if deferred:
        out['deferred_hosts'] = deferred
    return runner.StageResult(out)


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


HANDLERS = {'plan': stage_plan, 'fetch': stage_fetch, 'extract': stage_extract, 'evaluate': stage_evaluate,
            'gaps': stage_gaps, 'apply': stage_apply, 'quality': stage_quality}


def run_cycle(bot: Bot, resume: bool = True, trigger: str = 'manual') -> dict:
    cfg = {'lock_name': 'kg-collect', 'daemon_name': 'kg-collect', 'unit_costs': UNIT_COSTS, 'backoff_base_sec': 0.0, 'backoff_max_sec': 0.0}
    d = runner.Daemon(bot.conn, HANDLERS, cfg, env={'bot': bot}, stages=STAGES, sleep=lambda s: None)
    owns_scope = bot.net.run_id is None
    if owns_scope:
        bot.net.start_run()
    try:
        rep = d.run(trigger=trigger, resume=resume)
    finally:
        if owns_scope:
            bot.net.finish_run()
    rep['ai'] = bot.ai.client.status if bot.ai else 'disabled'
    if bot.ai:
        rep['ai_stats'] = dict(bot.ai.stats)
    if bot.ai and not bot.ai.client.configured:
        rep['ai_reason'] = 'missing_key'
    elif bot.ai and bot.ai.client.stopped:
        rep['ai_reason'] = bot.ai.client.stopped
    if rep.get('run_id'):
        rep['outputs'] = {r['stage']: json.loads(r['output_json']) for r in bot.conn.execute('SELECT stage, output_json FROM stage_checkpoint WHERE run_id=? AND output_json IS NOT NULL', (rep['run_id'],))}
    return rep
