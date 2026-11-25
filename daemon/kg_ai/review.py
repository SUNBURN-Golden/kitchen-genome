"""DeepSeek review stage: open ai_inference findings are reviewed and either published or closed.

Owner decision 2026-11-09: AI findings must not pile up as pending. Each open finding is
checked deterministically first (stored source text, verbatim quote, names present), then
reviewed by DeepSeek (deepseek-chat, thinking disabled, temperature 0, JSON). Approved findings
are written to data/seed.json under `ai_reviewed` on the matching restaurant/kitchen (or the
top-level `ai_reviewed_unlinked` list) with the source URL and an 'AI 검토' badge. Every
decision is appended to data/ai_review_log.jsonl; scripts/ai_review_rollback.py undoes it.
Rejected findings are closed (review_queue.state='dismissed') with the reason.

Approved career quotes also supply evidence to the G3 career projection.
"""
import argparse
from copy import deepcopy
import datetime as dt
import json
import os
import re
import sys
import unicodedata

from kg_ai import promotion
from kg_ai.article import article_key, source, digest
from kg_ai.balance import PAUSES
from kg_ai.budget import Budget
from kg_ai.deepseek import RETRYABLE, Client, DeepSeekError, MAX_INPUT_BYTES
from kg_collect.global_queue import write
from kg_collect.site_publish import atomic_json, seed_lock
from kg_collect.targets import Gazetteer, eligibility, load_seed_targets
from kg_daemon.db import ROOT, connect, migrate, new_id, utcnow
from kg_daemon.runner import CostMeter

_CRAWLER = os.path.join(ROOT, 'crawler')
if _CRAWLER not in sys.path:
    sys.path.insert(0, _CRAWLER)
from privacy_filter import sanitize_before_store  # noqa: E402

TASK = 'ai-review-2'
DAEMON = 'kg-ai-review'
BADGE = 'AI 검토'
MAX_BATCH = 8
CONTEXT = 160
MAX_OCC = 3
LABEL_KO = {'career': '경력', 'employment_claim': '근무', 'opening': '오픈', 'closure': '폐업', 'relocation': '이전',
            'rename': '이름 변경', 'menu': '메뉴', 'collab': '협업', 'mentorship_claim': '사사'}
UNIT_COSTS = {'http_request': 1.0, 'llm_tokens_k': 0.5, 'db_write': 0.0}
INSTRUCTION = (
    'You review machine-extracted culinary findings before they are published on a Korean chef-lineage site. '
    'Read the shared article context first. Input: {"url","published_at","findings":[{"id","label","chef_text","restaurant_text","org_text","role_text","date_text","quote","occurrences":[Unicode offsets for every quote occurrence in the shared article text],"site_record"}]}. '
    'For EACH finding return {"id","decision":"approve"|"reject","reason":"short English reason"}. Output exactly {"reviews":[...]} with one entry per input id. '
    'Approve ONLY when ALL hold: (1) the excerpts show the quote states this finding about real people and real restaurants '
    '(reject TV drama, film, novel, game, fictional characters, actors playing roles, or competition-show mock restaurants); '
    '(2) every given field (chef_text, restaurant_text, org_text, role_text, date_text) is supported by the excerpt and attached to the right person and place '
    '(reject when the role, chef, or restaurant belongs to a different sentence, person, or place); '
    '(3) the label fits what the quote says (opening means a restaurant opened, closure means it actually closed, collab is a real collaboration, '
    'menu is a dish/menu/program of that restaurant, employment_claim/career is a real job at that place); planned, rumoured, or "near closure" is not closure; '
    '(4) when the quote occurs more than once, every occurrence supports the same claim; '
    '(5) compare site_record with source dates: an explicit dated later closure, relocation or chef change may supersede an older record. Reject undated, ambiguous, speculative or conflicting changes. Approval annotates rather than overwrites the record. '
    'Shared employment is not mentorship. Judge only from the supplied text; never use outside knowledge to add facts. Reject when unsure.'
)


def kst_now():
    return dt.datetime.now(dt.timezone(dt.timedelta(hours=9))).strftime('%Y-%m-%dT%H:%M:%S+09:00')


def _norm(s):
    return re.sub(r'\s+', '', unicodedata.normalize('NFC', s or '')).casefold()


def source_text(raw_dir, text_ref):
    """The text the extractor validated offsets against: NFC(sanitize(raw[:12000])); also the full raw text."""
    if not text_ref:
        return None, None
    path = os.path.join(raw_dir, text_ref)
    if not os.path.isfile(path):
        return None, None
    raw = open(path, encoding='utf-8').read()
    clean, _ = sanitize_before_store(raw[:12000])
    full, _ = sanitize_before_store(raw)
    return unicodedata.normalize('NFC', clean or ''), unicodedata.normalize('NFC', full or '')


def occurrences(text, quote, limit=None):
    out, pos = [], 0
    while limit is None or len(out) < limit:
        i = text.find(quote, pos)
        if i < 0:
            break
        out.append(i)
        pos = i + 1
    return out


def context_windows(text, intervals):
    merged = []
    for start, end in sorted(intervals):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
        else:
            merged.append((start, end))
    return '\n[…]\n'.join(text[a:b] for a, b in merged), merged


def precheck(f, text):
    """Deterministic gate. Returns (reject_reason | None, [occurrence excerpts])."""
    if text is None:
        return 'source_unavailable: stored source text missing', []
    q = f['quote'] or ''
    if f['label'] not in LABEL_KO:
        return 'label_invalid', []
    occ = occurrences(text, q)
    if not q or not occ:
        return 'quote_not_in_source: quote is not verbatim in the stored source', []
    for k in ('chef_text', 'restaurant_text', 'org_text', 'role_text'):
        if f.get(k) and f[k].casefold() not in text.casefold():
            return f'name_absent: {k} not in source', []
    if f.get('date_text') and f['date_text'] not in text:
        return 'date_not_verbatim: date not in source', []
    return None, [text[max(0, i - CONTEXT): i + len(q) + CONTEXT] for i in occ[:MAX_OCC]]


class Site:
    """Matches a finding to a published seed restaurant/kitchen. Exact (whitespace/case-folded) names only."""

    def __init__(self, seed):
        self.seed = seed
        self.by_name = {}
        for k in seed.get('kitchens', []):
            for n in (k.get('name'), k.get('en'), k.get('name_en')):
                if n:
                    self.by_name.setdefault(_norm(n), []).append(('kitchen', k))
        for r in seed.get('restaurants', []):
            for n in [r.get('name'), r.get('name_en')] + list(r.get('prev_names') or []):
                if n:
                    self.by_name.setdefault(_norm(n), []).append(('restaurant', r))
        self.by_chef = {}
        for r in seed.get('restaurants', []):
            if r.get('chef'):
                self.by_chef.setdefault(_norm(r['chef']), []).append(('restaurant', r))

    def match(self, f):
        for key in ('restaurant_text', 'org_text'):
            hits = {id(x[1]): x for x in self.by_name.get(_norm(f.get(key)), [])} if f.get(key) else {}
            if len(hits) == 1:
                return next(iter(hits.values()))
            rest = [x for x in hits.values() if x[0] == 'restaurant']
            if len(rest) == 1:                    # a kitchen and its own restaurant page share the name: the restaurant page carries it
                return rest[0]
        if f.get('chef_text') and not f.get('restaurant_text'):
            hits = self.by_chef.get(_norm(f['chef_text']), [])
            if len(hits) == 1:
                return hits[0]
        return None

    @staticmethod
    def record(m):
        if not m:
            return None
        kind, x = m
        keys = ('id', 'name', 'name_en', 'en', 'chef', 'role', 'kitchen', 'opened', 'closed_at', 'area', 'cuisine', 'michelin')
        rec = {k: x[k] for k in keys if x.get(k) not in (None, '')}
        rec['kind'] = kind
        if x.get('chef_career'):
            rec['chef_career'] = [c.get('text') for c in x['chef_career']][:6]
        return rec


def validate_reviews(ids):
    def check(obj):
        if not isinstance(obj, dict) or set(obj) != {'reviews'} or not isinstance(obj.get('reviews'), list) or len(obj['reviews']) > MAX_BATCH:
            raise ValueError()
        out = {}
        for r in obj['reviews']:
            if not isinstance(r, dict) or set(r) != {'id','decision','reason'} or r.get('id') not in ids or r['id'] in out:
                raise ValueError()
            if r.get('decision') not in ('approve', 'reject') or not isinstance(r.get('reason'), str):
                raise ValueError()
            out[r['id']] = {'decision': r['decision'], 'reason': re.sub(r'[\x00-\x1f]', ' ', r['reason'])[:200]}
        if set(out) != set(ids):
            raise ValueError()
        return out
    return check


def open_items(conn):
    """Open ai_inference extraction reviews (oldest first) joined with their source fetch."""
    items = [dict(r) for r in conn.execute("""SELECT q.review_id, q.created_at q_created, e.*, f.text_ref, f.url fetch_url FROM review_queue q
                           JOIN extraction e ON e.extraction_id=q.subject_id LEFT JOIN fetch_record f ON f.fetch_id=e.fetch_id
                           WHERE q.reason='ai_inference' AND q.subject_type='extraction' AND q.state='open'
                           ORDER BY CASE e.workload WHEN 'fresh' THEN 0 ELSE 1 END,q.created_at,q.rowid""").fetchall()]
    for row in conn.execute("""SELECT q.review_id,q.created_at q_created,c.*,d.raw_ref,d.canonical_url,d.url,d.published_at
                              FROM review_queue q JOIN candidate_fact c ON c.candidate_id=q.subject_id
                              LEFT JOIN document d USING(document_id)
                              WHERE q.reason='ai_inference' AND q.subject_type='candidate_fact' AND q.state='open'
                              AND NOT EXISTS (SELECT 1 FROM review_queue x WHERE x.subject_type='extraction'
                                AND x.subject_id=json_extract(c.evidence_json,'$.locator.extraction_id'))""").fetchall():
        r = dict(row)
        p, ev = json.loads(r['payload_json']), json.loads(r['evidence_json'])
        f = {k: p.get(k) for k in ('chef_text','restaurant_text','org_text','role_text','date_text')}
        f.update({'review_id':r['review_id'],'q_created':r['q_created'],'candidate_id':r['candidate_id'],
                  'extraction_id':r['candidate_id'],'fetch_id':r['document_id'],'canonical_url':r['canonical_url'] or ev.get('source_url'),
                  'published_at':r['published_at'],'label':p.get('label',r['fact_type']), 'quote':ev.get('quote'),
                  'text_ref':None,'_document_ref':r['raw_ref'],'workload':p.get('workload','fresh')})
        items.append(f)
    return sorted(items, key=lambda f:(f['workload'] != 'fresh',f['q_created'],f['extraction_id']))


def _close(conn, review_id, decision, reason, meta, now, *, delivery=None):
    row = conn.execute('SELECT payload_json FROM review_queue WHERE review_id=?', (review_id,)).fetchone()
    p = json.loads(row['payload_json'] or '{}')
    p['ai_review'] = {'decision': decision, 'reason': reason, 'at': now, **({'model_meta': meta} if meta else {})}
    if delivery:
        p['ai_review']['delivery'] = delivery
    conn.execute('UPDATE review_queue SET state=?, resolved_at=?, payload_json=? WHERE review_id=? AND state=\'open\'',
                 ('resolved' if decision == 'approve' else 'dismissed', now, json.dumps(p, ensure_ascii=False, sort_keys=True), review_id))


def close_linked_candidates(conn, now):
    """candidate_fact ai_inference reviews follow the decision already taken on their source extraction."""
    n = 0
    for r in conn.execute("""SELECT q.review_id, c.candidate_id, json_extract(c.evidence_json,'$.locator.extraction_id') ext FROM review_queue q
                             JOIN candidate_fact c ON c.candidate_id=q.subject_id
                             WHERE q.reason='ai_inference' AND q.subject_type='candidate_fact' AND q.state='open'""").fetchall():
        if not r['ext']:
            continue
        src = conn.execute("""SELECT state, json_extract(payload_json,'$.ai_review.decision') d, json_extract(payload_json,'$.ai_review.reason') why
                              FROM review_queue WHERE reason='ai_inference' AND subject_type='extraction' AND subject_id=?""", (r['ext'],)).fetchone()
        if not src or not src['d']:
            continue
        _close(conn, r['review_id'], src['d'], f"follows extraction {r['ext']}: {src['why']}", None, now)
        if src['d'] == 'reject':
            conn.execute("UPDATE candidate_fact SET resolution='rejected' WHERE candidate_id=?", (r['candidate_id'],))
        n += 1
    conn.commit()
    return n


def annotation(f, review_id, now):
    return {'label': f['label'], 'label_ko': LABEL_KO.get(f['label'], f['label']), 'quote': f['quote'], 'src': f['canonical_url'],
            'date': f['published_at'], 'chef': f['chef_text'], 'restaurant': f['restaurant_text'] or f['org_text'],
            'provenance': 'ai_reviewed', 'badge': BADGE, 'extraction_id': f['extraction_id'], 'review_id': review_id, 'reviewed_at': now}


def publish(seed, site, entry):
    """Append the reviewed quote once per review, preserving independent proofs."""
    for kind, holder, item in promotion.annotations(seed):
        if item.get('review_id') == entry['review_id']:
            return f'{kind[:-1]}:{holder["id"]}' if kind else 'unlinked', False
    venue = entry['restaurant']
    blocked = venue and (promotion._venue_reason(venue) or promotion._venue_tail_reason(
        entry.get('quote') or '', venue, other=entry.get('chef')))
    m = None if blocked else site.match({
        'restaurant_text': venue, 'chef_text': entry['chef']})
    if m:
        target = f'{m[0]}:{m[1]["id"]}'
        lst = m[1].setdefault('ai_reviewed', [])
    else:
        target = 'unlinked'
        lst = seed.setdefault('ai_reviewed_unlinked', [])
    lst.append(entry)
    return target, True


def _finish_delivery(conn, seed, seed_path, log_path, raw_dir, stats, now):
    prior = deepcopy(seed)
    audit = {(row.get('action'), row.get('review_id')): row for row in promotion.audit_rows(log_path)}
    denied = {rid for action, rid in audit if action == 'rollback'}
    denied.update(seed.get(promotion.KEY, {}).get('withdrawn', ()))
    pending = []
    site = Site(seed)
    for row in conn.execute("""SELECT review_id,payload_json FROM review_queue
                              WHERE reason='ai_inference' AND json_type(payload_json,'$.ai_review.delivery')='object'
                              ORDER BY rowid"""):
        delivery = json.loads(row['payload_json'])['ai_review']['delivery']
        review_id = row['review_id']
        rec = delivery['audit']
        entry = delivery.get('entry')
        if entry and review_id not in denied:
            rec['target'], added = publish(seed, site, entry)
            rec['published'] = added or (rec['action'], review_id) not in audit
            stats['published'] += int(added)
        pending.append((rec, review_id))
    with write(conn):
        stats['promoted'] += promotion.backfill(conn, seed, raw_dir, log_path, now)
        stats['promotion_skips'] = promotion.skip_histogram(conn)
    stats['unlinked_remaining'] = len(seed.get('ai_reviewed_unlinked', ()))
    if seed != prior:
        atomic_json(seed_path, seed)
    if pending:
        targets = {rid: f'{kind[:-1]}:{holder["id"]}' if kind else 'unlinked'
                   for kind, holder, entry in promotion.annotations(seed)
                   if (rid := promotion._identifier(entry.get('review_id')))}
    for rec, review_id in pending:
        if (rec['action'], review_id) not in audit and review_id not in denied:
            if rec['action'] == 'approve' and review_id in targets:
                rec['target'] = targets[review_id]
            append_log(log_path, rec)
        conn.execute("UPDATE review_queue SET payload_json=json_remove(payload_json,'$.ai_review.delivery') WHERE review_id=?",
                     (review_id,))
    conn.commit()


def append_log(path, rec):
    with open(path, 'a', encoding='utf-8') as fh:
        fh.write(json.dumps(rec, ensure_ascii=False, sort_keys=True) + '\n')
        fh.flush()
        os.fsync(fh.fileno())


def _start_run(conn):
    rid = new_id('run')
    conn.execute('INSERT INTO daemon_run(run_id,daemon,status,trigger,cost_cap_units,started_at) VALUES (?,?,?,?,?,?)',
                 (rid, DAEMON, 'running', 'daily', 0, utcnow()))
    conn.commit()
    return rid


def review(conn, seed_path, log_path, raw_dir, *, client=None, limit=None):
    now = utcnow()
    stats = {'approved': 0, 'rejected': 0, 'published': 0, 'prechecked_rejects': 0, 'calls': 0, 'stopped': None, 'candidates_closed': 0, 'skipped_no_targets': 0, 'skipped_balance': 0, 'considered': 0, 'eligible': 0}
    stats.update(promoted=0, unlinked_remaining=0)
    with open(seed_path, encoding='utf-8') as stream:
        seed = json.load(stream)
    stats['candidates_closed'] += close_linked_candidates(conn, now)
    items = open_items(conn)
    if limit:
        items = items[:limit]
    _finish_delivery(conn, seed, seed_path, log_path, raw_dir, stats, now)
    if not items:
        stats['remaining'] = conn.execute("SELECT COUNT(*) FROM review_queue WHERE reason='ai_inference' AND state='open'").fetchone()[0]
        stats['unreviewed_candidate_findings'] = conn.execute("SELECT COUNT(*) FROM review_queue WHERE reason='ai_inference' AND subject_type='candidate_fact' AND state='open'").fetchone()[0]
        stats['reason'] = 'none'
        return stats
    site = Site(seed)
    load_seed_targets(conn, seed)
    gaz = Gazetteer(conn)
    run_id = _start_run(conn)
    meter = CostMeter(conn, run_id, UNIT_COSTS)
    meter.stage = 'ai_review'
    if client is None:
        client = Client(Budget(meter))
    else:
        client.budget.meter, client.budget.conn = meter, conn

    def decide(f, decision, reason, meta):
        ts = utcnow()
        rec = {'at': ts, 'at_kst': kst_now(), 'action': decision, 'review_id': f['review_id'], 'extraction_id': f['extraction_id'],
               'label': f['label'], 'quote': sanitize_before_store(f['quote'] or '')[0], 'src': f['canonical_url'], 'reason': reason, 'model_meta': meta}
        delivery = {'audit': rec}
        if decision == 'approve':
            delivery['entry'] = annotation(f, f['review_id'], ts)
            stats['approved'] += 1
        else:
            stats['rejected'] += 1
        _close(conn, f['review_id'], decision, reason, meta, ts, delivery=delivery)
        if f.get('candidate_id'):
            conn.execute('UPDATE candidate_fact SET resolution=? WHERE candidate_id=?', ('needs_review' if decision == 'approve' else 'rejected', f['candidate_id']))
        conn.commit()

    groups = {}
    for f in items:
        f = dict(f)
        stats['considered'] += 1
        if f.get('_document_ref'):
            try:
                body = json.load(open(f['_document_ref'], encoding='utf-8'))
                text, _ = sanitize_before_store(body.get('title') or '')
                text = unicodedata.normalize('NFC', text or '')
            except (OSError, ValueError, TypeError):
                text = None
            raw_full = text
        else:
            text, raw_full = source_text(raw_dir, f['text_ref'])
            if raw_full is not None:
                text = raw_full
        why, ctx = precheck(f, text)
        if why and text is not None and raw_full is not None:
            alt_why, alt_ctx = precheck(f, raw_full)
            if not alt_why:
                why, ctx, text = None, alt_ctx, raw_full
        if not why and not eligibility(gaz, text)['target_ids']:
            why = 'skipped_no_targets'
            stats['skipped_no_targets'] += 1
        if why:
            stats['prechecked_rejects'] += 1
            decide(f, 'reject', why, None)
            continue
        stats['eligible'] += 1
        f['_ctx'] = [{'start': i, 'end': i + len(f['quote'])} for i in occurrences(text, f['quote'])]
        f['_text'] = text
        f['_source'] = source(f['canonical_url'], f['published_at'], text)
        groups.setdefault((article_key(f['canonical_url'], text), f['workload']), []).append(f)

    def payload_for(batch):
        return {'_article': batch[0]['_source'],
                'findings': [{'id': f['extraction_id'], 'label': f['label'], 'chef_text': f['chef_text'], 'restaurant_text': f['restaurant_text'],
                              'org_text': f['org_text'], 'role_text': f['role_text'], 'date_text': f['date_text'], 'quote': f['quote'],
                              'occurrences': f['_ctx'], 'source_span': [f.get('span_start'),f.get('span_end')], 'site_record': Site.record(site.match(f))} for f in batch]}

    def max_tokens(batch):
        return 200 + 120 * len(batch)

    def fits(batch):
        payloads = (client._payload(INSTRUCTION, payload_for(batch), max_tokens(batch), None, provider=provider)[1]
                    for provider in ('deepseek', client.fallback_provider))
        return all(len(payload) + 256 <= MAX_INPUT_BYTES for payload in payloads)

    def use_context(f, shared, text, segments):
        f['_source'] = {**shared, 'text': text, 'context_digest': digest(text), 'source_segments': segments}
        f['_text'] = text
        f['_ctx'] = [{'start': i, 'end': i + len(f['quote'])} for i in occurrences(text, f['quote'])]

    for fs in groups.values():
        publication = min((f['published_at'] for f in fs if f['published_at']), default=None)
        shared = source(fs[0]['canonical_url'],publication,fs[0]['_text'])
        for f in fs:
            f['_source'] = shared
        if fits(fs[:MAX_BATCH]):
            continue
        text = fs[0]['_text']
        target_spans = eligibility(gaz, text)['spans']
        intervals = [(max(0,m['start']-CONTEXT), min(len(text),m['end']+CONTEXT)) for m in target_spans]
        for f in fs:
            intervals.extend((max(0,i-CONTEXT),min(len(text),i+len(f['quote'])+CONTEXT)) for i in occurrences(text,f['quote'],limit=len(text)))
        bounded, merged = context_windows(text, intervals)
        if eligibility(gaz,bounded)['target_ids'] and all(precheck(f,bounded)[0] is None for f in fs):
            for f in fs:
                use_context(f, shared, bounded, merged)
        if fits(fs[:MAX_BATCH]):
            continue
        for f in fs:
            # Keep every verbatim occurrence, but no unrelated target windows from a long listing.
            windows = [(max(0, i - CONTEXT), min(len(text), i + len(f['quote']) + CONTEXT))
                       for i in occurrences(text, f['quote'])]
            nearby_targets = [(m['start'], m['end']) for m in target_spans
                              if any(m['start'] < end and start < m['end'] for start, end in windows)]
            minimal, segments = context_windows(text, windows + nearby_targets)
            if eligibility(gaz, minimal)['target_ids'] and precheck(f, minimal)[0] is None:
                use_context(f, shared, minimal, segments)

    queue = []
    for fs in groups.values():
        fs.sort(key=lambda f: f['extraction_id'])
        contexts = {}
        for f in fs:
            key = (f['_source']['context_digest'], tuple(f['_source'].get('source_segments', ())))
            contexts.setdefault(key, []).append(f)
        for context_fs in contexts.values():
            for i in range(0, len(context_fs), MAX_BATCH):
                queue.append(context_fs[i:i + MAX_BATCH])
    examined = 0
    while queue and not stats['stopped'] and (not limit or examined < limit):
        batch = queue.pop(0)
        if limit:
            batch = batch[:limit - examined]
        if not client.configured:
            stats['stopped'] = 'missing_key'
            break
        client.budget.workload = batch[0]['workload']
        if not fits(batch):
            if len(batch) > 1:
                h = len(batch) // 2
                queue[:0] = [batch[:h], batch[h:]]
                continue
            decide(batch[0], 'reject', 'review_unavailable: context_too_large', None)
            examined += 1
            continue
        ids = {f['extraction_id'] for f in batch}
        try:
            stats['calls'] += 1
            res = client.call(TASK, INSTRUCTION, payload_for(batch), validate_reviews(ids), max_tokens=max_tokens(batch), list_key='reviews')
        except DeepSeekError as exc:
            code = getattr(exc, 'code', str(exc)).split(' ')[0]
            if code == 'attempts_exhausted':
                stats['quarantined'] = stats.get('quarantined', 0) + len(batch)
                examined += len(batch)
                continue
            if client.stopped == 'schema_reject_limit':
                stats['stopped'] = client.stopped
                break
            if code in RETRYABLE + ('task_previously_rejected', 'truncated_json', 'incomplete_output'):
                # A batch the model cannot answer validly is split; a single finding it cannot review is closed, not left pending.
                client.stopped = None
                if len(batch) > 1:
                    h = len(batch) // 2
                    queue[:0] = [batch[:h], batch[h:]]
                else:
                    decide(batch[0], 'reject', 'review_unavailable: model returned no valid review (' + code + ')', None)
                    examined += 1
                continue
            stats['stopped'] = code
            if code in PAUSES:
                stats['calls'] -= 1
                stats['skipped_balance'] += len(batch)
            break
        meta = dict(client.last_meta)
        examined += len(batch)
        for f in batch:
            r = res[f['extraction_id']]
            decide(f, r['decision'], ('model: ' + r['reason']), meta)
    _finish_delivery(conn, seed, seed_path, log_path, raw_dir, stats, now)
    stats['candidates_closed'] += close_linked_candidates(conn, utcnow())
    from kg_collect.recheck import reconcile
    reconcile(conn)
    stats['remaining'] = conn.execute("SELECT COUNT(*) FROM review_queue WHERE reason='ai_inference' AND state='open'").fetchone()[0]
    stats['unreviewed_candidate_findings'] = conn.execute("SELECT COUNT(*) FROM review_queue WHERE reason='ai_inference' AND subject_type='candidate_fact' AND state='open'").fetchone()[0]
    stats['reason'] = stats['stopped'] or 'none'
    stats['cost_used'] = round(conn.execute('SELECT cost_used_units FROM daemon_run WHERE run_id=?', (run_id,)).fetchone()[0], 3)
    conn.execute('UPDATE daemon_run SET status=?, finished_at=? WHERE run_id=?',
                 ('succeeded' if stats['stopped'] in PAUSES else 'failed' if stats['stopped'] else 'succeeded', utcnow(), run_id))
    conn.commit()
    stats['run_id'] = run_id
    return stats


def main(argv=None):
    ap = argparse.ArgumentParser(prog='kg-ai-review')
    ap.add_argument('--db', required=True)
    ap.add_argument('--seed', default=os.path.join(ROOT, 'data', 'seed.json'))
    ap.add_argument('--log', default=os.path.join(ROOT, 'data', 'ai_review_log.jsonl'))
    ap.add_argument('--raw-dir')
    ap.add_argument('--limit', type=int)
    a = ap.parse_args(argv)
    conn = connect(a.db)
    migrate(conn)
    raw_dir = a.raw_dir or os.path.join(os.path.dirname(os.path.abspath(a.db)), 'raw')
    with seed_lock(a.seed):
        s = review(conn, a.seed, a.log, raw_dir, limit=a.limit)
    print('AI_REVIEW approved=%s rejected=%s published=%s prechecked=%s calls=%s remaining=%s stopped=%s cost=%s reason=%s skipped_no_targets=%s skipped_balance=%s unreviewed_candidate_findings=%s bundle_approved=%s bundle_rejected=%s bundle_remaining=%s promoted=%s unlinked_remaining=%s promotion_skips=%s' % (
        s['approved'], s['rejected'], s['published'], s['prechecked_rejects'], s['calls'], s.get('remaining'), s['stopped'] or 'none', s.get('cost_used', 0), s.get('reason','none'), s['skipped_no_targets'], s['skipped_balance'], s.get('unreviewed_candidate_findings',0),
        s.get('bundle_approved', 0), s.get('bundle_rejected', 0), s.get('bundle_remaining', 0), s.get('promoted', 0), s.get('unlinked_remaining', 0),
        json.dumps(s.get('promotion_skips', {}), sort_keys=True, separators=(',', ':'))))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
