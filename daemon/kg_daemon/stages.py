"""Stage handlers. Status: IMPLEMENTED = real logic + tests; STUB = interface + clear no-op (see docs/data/DAEMON.md).

 1 source_discovery          IMPLEMENTED  registry -> active/paused/skipped (IG pause flag honoured). Cadence scheduler: STUB (all active sources due)
 2 incremental_fetch         IMPLEMENTED for local crawler outputs (crawler/vault.json, crawler/out/ig-*.json); network fetchers: STUB (injected, absent = recorded as stubbed)
 3 change_detection          IMPLEMENTED  new / modified per canonical URL via content hash
 4 extraction                IMPLEMENTED  rule extractor over vault facts + IG findings; DeepSeek bounded extraction (candidates only, asserted_by=ai_inference)
 5 entity_resolution         IMPLEMENTED  exact name/alias + homonym scope; never auto-creates chefs; fuzzy matching: STUB
 6 evidence_verification     IMPLEMENTED  status from tier + independent trusted hosts; social/search leads never exceed 'unverified'
 7 state_history_update      IMPLEMENTED  status events, chef moves (close+add), mentorship (source-asserted only), conflicts
 8 service_output_recompute  IMPLEMENTED  current_employment + branch status as-of today (service_output table)
 9 quality_check             IMPLEMENTED  invariants (blocking) + staleness (review items)"""
import datetime as dt
import glob
import hashlib
import json
import os
import sys

from kg_ai import pending
from . import collect_bridge
from . import registry as reg_mod
from .claims import independent_sources, add_evidence, create_claim, host_of, link_evidence, open_conflict, queue_review, RANK
from .db import ROOT, new_id, stable_id, utcnow
from .ontology import AmbiguousMove, branch_state_asof, current_employment, move_chef, parse_iso_date, pending_moves
from .runner import NoActiveSources, NonRetryable, StageContext, StageResult

sys.path.insert(0, os.path.join(ROOT, 'crawler'))
try:
    from vault import canonicalize_url
except Exception:  # pragma: no cover
    def canonicalize_url(u):
        return (u or '').strip()


def _root(ctx):
    return ctx.env.get('root', ROOT)


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


# ───────────── 1. source discovery ─────────────
def source_discovery(ctx: StageContext) -> StageResult:
    reg = ctx.env.get('registry') or reg_mod.load_registry()
    reg_mod.sync_registry(ctx.conn, reg)
    active, paused, skipped = [], {}, {}
    for sid, s in reg['sources'].items():
        if s['collect_permission'] not in ('allowed', 'allowed_with_limits'):
            skipped[sid] = s['collect_permission']
            continue
        ps = reg_mod.pause_state(sid, s, _root(ctx))
        if ps:
            paused[sid] = ps           # documented limit: paused sources are reported, never worked around
            continue
        active.append(sid)
    if not active:
        raise NoActiveSources('every source is paused or not permitted')
    return StageResult({'active': active, 'paused': paused, 'skipped': skipped})


# ───────────── 2. incremental fetch ─────────────
def _vault_by_source(ctx) -> dict:
    """crawler/vault.json holds records of every tier; route each to its registry source (tier -> source) once per run."""
    out: dict = {}
    path = ctx.env.get('vault_path') or os.path.join(_root(ctx), 'crawler', 'vault.json')
    if not os.path.exists(path):
        return out
    for rec in json.load(open(path, encoding='utf-8')).get('records', []):
        tier = rec.get('source_tier') or 'search_lead'
        sid = 'manual_clip' if rec.get('collection_path') == 'manual_clip' else reg_mod.TIER_TO_SOURCE.get(tier, 'search_leads')
        body = {k: rec.get(k) for k in ('target_id', 'kind', 'title', 'published_at', 'extracted_facts', 'evidence_locator', 'pending_only', 'asserted_by', 'extractor', 'collector_account')}
        # one article can back several target records: identity is (url, target, kind) so they are not mistaken for edits of each other
        out.setdefault(sid, []).append({'url': rec['url'], 'doc_key': f"#target={rec.get('target_id')}&kind={rec.get('kind')}",
                                        'published_at': rec.get('published_at'), 'body': body, 'collected_at': rec.get('collected_at')})
    return out


def _instagram_records(ctx):
    """Reads what crawler/instagram.py already wrote (crawler/out/ig-*.json). The daemon never logs in to or contacts Instagram itself."""
    for p in sorted(glob.glob(os.path.join(_root(ctx), 'crawler', 'out', 'ig-*.json'))):
        data = json.load(open(p, encoding='utf-8'))
        for f in (data if isinstance(data, list) else data.get('findings', [])):
            yield {'url': f['url'], 'doc_key': f"#target={f.get('id')}&kind={f.get('kind')}", 'published_at': f.get('date'),
                   'body': {'target_id': f.get('id'), 'kind': f.get('kind'), 'title': f.get('title'), 'instagram': True}, 'collected_at': None}


def incremental_fetch(ctx: StageContext) -> StageResult:
    reg = ctx.env.get('registry') or reg_mod.load_registry()
    raw_dir = ctx.env.get('raw_dir')           # RAW STORE: outside git (data_store/raw by default; never committed)
    counts, stubbed = {}, []
    vault = _vault_by_source(ctx)
    collected = collect_bridge.collector_records(ctx.env.get('collect_db'), reg_mod.TIER_TO_SOURCE) if ctx.env.get('collect_db') else {}
    for sid in ctx.prev['source_discovery']['active']:
        s = reg['sources'][sid]
        adapter = s['adapter']
        fetcher = (ctx.env.get('fetchers') or {}).get(adapter)
        records = list(vault.get(sid, [])) + list(collected.get(sid, []))   # local, free: what the crawler + live collector already fetched
        if adapter == 'instagram_py':
            records = list(_instagram_records(ctx))
        elif fetcher:
            records = records + list(fetcher(sid, s, ctx))        # network fetchers must call ctx.cost.charge('http_request') and honour halts
        elif adapter == 'http_fetch':
            stubbed.append(sid)                                    # STUB: network fetch not wired in Phase 1 (local crawler output is still ingested)
        new = dup = 0
        for rec in records:
            content = json.dumps(rec['body'], ensure_ascii=False, sort_keys=True)
            h = _sha(content)
            fetched = rec.get('collected_at') or ctx.env.get('today') or utcnow()[:10]
            canon = canonicalize_url(rec['url']) + rec.get('doc_key', '')
            did = stable_id('doc', sid, canon, h)
            raw_ref = None
            if raw_dir and s.get('retention_days') is not None:
                os.makedirs(raw_dir, exist_ok=True)
                raw_ref = os.path.join(raw_dir, f'{h[:24]}.json')
                if not os.path.exists(raw_ref):
                    open(raw_ref, 'w', encoding='utf-8').write(content)
            exp = None
            if s.get('retention_days') is not None:
                try:
                    exp = (dt.date.fromisoformat(fetched[:10]) + dt.timedelta(days=s['retention_days'])).isoformat()
                except ValueError:
                    exp = None
            cur = ctx.conn.execute("""INSERT OR IGNORE INTO document(document_id,source_id,url,canonical_url,content_hash,fetched_at,published_at,raw_ref,retention_expires_at)
                                      VALUES (?,?,?,?,?,?,?,?,?)""", (did, sid, rec['url'], canon, h, fetched, rec.get('published_at'), raw_ref, exp))
            if cur.rowcount:
                new += 1
            else:
                dup += 1
        ctx.conn.commit()
        counts[sid] = {'new': new, 'unchanged': dup}
    return StageResult({'counts': counts, 'stubbed_adapters': stubbed})


# ───────────── 3. change detection ─────────────
def change_detection(ctx: StageContext) -> StageResult:
    docs = ctx.conn.execute("""SELECT d.* FROM document d LEFT JOIN change_event c ON c.document_id=d.document_id
                               WHERE c.document_id IS NULL ORDER BY d.fetched_at, d.rowid""").fetchall()
    n_new = n_mod = 0
    for d in docs:
        prev = ctx.conn.execute("""SELECT document_id FROM document WHERE source_id=? AND canonical_url=? AND content_hash<>? AND rowid<(SELECT rowid FROM document WHERE document_id=?)
                                   ORDER BY rowid DESC LIMIT 1""", (d['source_id'], d['canonical_url'], d['content_hash'], d['document_id'])).fetchone()
        kind = 'modified' if prev else 'new'
        ctx.conn.execute('INSERT OR IGNORE INTO change_event(change_id,document_id,prev_document_id,change_kind,run_id,created_at) VALUES (?,?,?,?,?,?)',
                         (stable_id('chg', d['document_id']), d['document_id'], prev['document_id'] if prev else None, kind, ctx.run_id, utcnow()))
        if prev:
            n_mod += 1
            applied = ctx.conn.execute('SELECT 1 FROM candidate_fact WHERE document_id=? AND applied_claim_id IS NOT NULL LIMIT 1', (prev['document_id'],)).fetchone()
            if applied:   # source text changed after we applied facts from it: re-extract AND let a human see it
                queue_review(ctx.conn, 'source_modified', 'document', d['document_id'], {'previous': prev['document_id'], 'url': d['url']},
                             dedupe_key=f'source_modified:{d["document_id"]}')
        else:
            n_new += 1
    ctx.conn.commit()
    return StageResult({'new': n_new, 'modified': n_mod})


# ───────────── 4. extraction ─────────────
def _cand(ctx, doc, ftype, payload, extractor, locator, quote, asserted_by='source'):
    held = asserted_by == 'ai_inference' or pending(payload) or pending({'extractor': extractor})
    if held:
        payload = {**payload, 'pending_only': True}
        if not payload.get('instagram'):
            asserted_by = 'ai_inference'
    key = _sha(f'{extractor}|{asserted_by}|{doc["canonical_url"]}|{doc["content_hash"]}|{ftype}|{json.dumps(payload, ensure_ascii=False, sort_keys=True)}')
    ev = {'source_url': doc['url'], 'locator': locator or {}, 'quote': (quote or None) and quote[:200],
          'collected_at': doc['fetched_at'], 'effective_from': payload.get('effective_date')}
    ctx.conn.execute("""INSERT OR IGNORE INTO candidate_fact(candidate_id,idempotency_key,document_id,source_id,fact_type,payload_json,evidence_json,extractor,asserted_by,created_at)
                        VALUES (?,?,?,?,?,?,?,?,?,?)""", (stable_id('cnd', key), key, doc['document_id'], doc['source_id'], ftype,
                                                          json.dumps(payload, ensure_ascii=False, sort_keys=True), json.dumps(ev, ensure_ascii=False), extractor, asserted_by, utcnow()))
    if held:
        cid = stable_id('cnd', key)
        ctx.conn.execute("UPDATE candidate_fact SET resolution='needs_review',verification=? WHERE candidate_id=?",
                         ('ai_inference' if asserted_by == 'ai_inference' else 'unverified', cid))
        queue_review(ctx.conn, 'ai_inference' if asserted_by == 'ai_inference' else 'pending_only', 'candidate_fact', cid,
                     {'pending_only': True, 'asserted_by': asserted_by, 'payload': payload, 'evidence': ev}, dedupe_key='pending:' + cid)


def extract_from_body(ctx, doc, body) -> int:
    """Rule extractor. Emits CANDIDATES only — each backed by text in the source record; nothing is inferred."""
    before = ctx.conn.total_changes
    tid, f, loc = body.get('target_id'), body.get('extracted_facts') or {}, body.get('evidence_locator') or {}
    quote = loc.get('quote') or body.get('title')
    if body.get('collect'):
        today = ctx.env.get('today') or utcnow()[:10]
        for ftype, payload, loc2, q in collect_bridge.candidates(body, today):
            if ftype == 'ambiguous' and not pending(body['collect']):
                ctx.env.setdefault('_ambiguous_skipped', [0])[0] += 1   # negated / planned / unclear subject: not a fact, not a candidate
                continue
            _cand(ctx, doc, ftype, payload, f"collect:{body['collect']['extractor']}", loc2, q, asserted_by='ai_inference' if pending(body['collect']) else 'source')
        return ctx.conn.total_changes - before
    if pending(body):
        queue_review(ctx.conn, 'pending_only', 'document', doc['document_id'], {'pending_only': True, 'body': body}, dedupe_key='pending:doc:' + doc['document_id'])
        return ctx.conn.total_changes - before
    if body.get('instagram'):   # IG findings are keyword hits with a <=40-char excerpt: lead only
        kind = {'closure': 'closure', 'rename': 'rename', 'opening': 'opening', 'chef_move': 'chef_move'}.get(body.get('kind'))
        if kind:
            _cand(ctx, doc, kind, {'target_id': tid, 'excerpt': (body.get('title') or '')[:40], 'lead_only': True, 'instagram': True}, 'rule:instagram_keyword', {'post_url': doc['url']}, body.get('title'))
        return ctx.conn.total_changes - before
    if f.get('closure') and parse_iso_date(str(f['closure'])):
        _cand(ctx, doc, 'closure', {'target_id': tid, 'effective_date': str(f['closure'])}, 'rule:vault_facts', loc, quote)
    if f.get('relocation') is True:
        _cand(ctx, doc, 'relocation', {'target_id': tid}, 'rule:vault_facts', loc, quote)
    if f.get('new_name'):
        _cand(ctx, doc, 'rename', {'target_id': tid, 'new_name': f['new_name']}, 'rule:vault_facts', loc, quote)
    if f.get('chef') and f.get('origin_kitchen'):
        _cand(ctx, doc, 'employment', {'chef_name': f['chef'], 'origin_kitchen': f['origin_kitchen'], 'target_kitchen_id': f['origin_kitchen'],
                                       'role_text': f.get('role'), 'tenure_text': f.get('tenure_period')}, 'rule:vault_facts', loc, quote)
    mv = f.get('move')   # structured move (from manual findings / outreach / future extractor): {chef_name, origin_kitchen?, to_target_id|to_label, effective_date}
    if isinstance(mv, dict) and mv.get('chef_name') and (mv.get('to_target_id') or mv.get('to_label')):
        _cand(ctx, doc, 'chef_move', {k: mv.get(k) for k in ('chef_name', 'origin_kitchen', 'to_target_id', 'to_label', 'effective_date', 'role_text')}, 'rule:structured_move', loc, quote)
    if f.get('mentor_assertion'):   # only an explicit quoted assertion creates a mentorship candidate
        m = f['mentor_assertion']
        _cand(ctx, doc, 'mentorship', {'chef_name': f.get('chef'), 'origin_kitchen': f.get('origin_kitchen'), 'mentor_label': m.get('mentor_label'),
                                       'assertion_text': m.get('assertion_text')}, 'rule:vault_facts', loc, m.get('assertion_text'))
    return ctx.conn.total_changes - before


def extraction(ctx: StageContext) -> StageResult:
    rows = ctx.conn.execute("""SELECT d.*, c.change_id FROM change_event c JOIN document d ON d.document_id=c.document_id WHERE c.state='pending' ORDER BY d.rowid""").fetchall()
    extracted = missing_raw = 0
    ai = ctx.env.get('ai_extractor')
    if ctx.env.get('deepseek') and ai is None:
        from .deepseek import extract
        ai = lambda doc, body: extract(ctx, doc, body)
    for d in rows:
        if not d['raw_ref'] or not os.path.exists(d['raw_ref']):
            missing_raw += 1
            continue
        body = json.load(open(d['raw_ref'], encoding='utf-8'))
        extract_from_body(ctx, d, body)
        if ai and not body.get('collect') and not body.get('instagram') and not pending(body):
            for ftype, payload, quote in ai(d, body):
                _cand(ctx, d, ftype, payload, 'deepseek:local' if ctx.env.get('deepseek') else 'ai:hook', payload.get('evidence_locator') or {}, quote, asserted_by='ai_inference')
        ctx.conn.execute("UPDATE change_event SET state='extracted' WHERE change_id=?", (d['change_id'],))
        extracted += 1
    ctx.conn.commit()
    return StageResult({'documents_extracted': extracted, 'documents_missing_raw': missing_raw})


# ───────────── 5. entity resolution ─────────────
def _branch_for_target(conn, target_id):
    r = conn.execute("""SELECT b.branch_id FROM alias a JOIN branch b ON b.restaurant_id=a.entity_id WHERE a.entity_type='restaurant' AND a.kind='legacy_id' AND a.alias=?""", (target_id,)).fetchall()
    return [x['branch_id'] for x in r]


def _chefs_for(conn, name, origin):
    rows = conn.execute("""SELECT DISTINCT c.chef_id, c.disambiguator FROM chef c LEFT JOIN alias a ON a.entity_type='chef' AND a.entity_id=c.chef_id
                           WHERE c.display_name=? OR a.alias=?""", (name, name)).fetchall()
    if origin and rows:
        scoped = [r for r in rows if r['disambiguator'] == origin]
        if not scoped:
            return None            # same name exists but under a different origin: possible homonym, never guess
        rows = scoped
    return [r['chef_id'] for r in rows]


def entity_resolution(ctx: StageContext) -> StageResult:
    res = needs = 0
    for c in ctx.conn.execute("SELECT * FROM candidate_fact WHERE resolution='pending' ORDER BY rowid").fetchall():
        p = json.loads(c['payload_json'])
        if pending(p) or pending(c):
            ctx.conn.execute("UPDATE candidate_fact SET resolution='needs_review' WHERE candidate_id=?", (c['candidate_id'],))
            queue_review(ctx.conn, 'pending_only', 'candidate_fact', c['candidate_id'], {'pending_only': True, 'payload': p}, dedupe_key='pending:' + c['candidate_id'])
            needs += 1
            continue
        chef_id = branch_id = None
        reason = None
        t = p.get('target_id') or p.get('target_kitchen_id')
        if t:
            bs = _branch_for_target(ctx.conn, t)
            if len(bs) == 1:
                branch_id = bs[0]
            else:
                reason = 'unknown_target' if not bs else 'ambiguous_branch'
        if c['fact_type'] == 'chef_move' and p.get('to_target_id'):
            bs = _branch_for_target(ctx.conn, p['to_target_id'])
            branch_id = bs[0] if len(bs) == 1 else None
            reason = None if branch_id else 'unknown_destination'
        if not reason and p.get('chef_name'):
            cs = _chefs_for(ctx.conn, p['chef_name'], p.get('origin_kitchen'))
            if cs is None:
                reason = 'homonym'
            elif len(cs) == 1:
                chef_id = cs[0]
            else:
                reason = 'unknown_chef' if not cs else 'homonym'
        if not reason and c['fact_type'] in ('closure', 'opening', 'relocation', 'rename', 'pause', 'menu', 'collab') and not branch_id:
            reason = 'unknown_target'
        if reason:
            ctx.conn.execute("UPDATE candidate_fact SET resolution='needs_review' WHERE candidate_id=?", (c['candidate_id'],))
            queue_review(ctx.conn, reason, 'candidate_fact', c['candidate_id'], {'fact_type': c['fact_type'], 'payload': p}, dedupe_key=f'{reason}:{c["candidate_id"]}')
            needs += 1
        else:
            ctx.conn.execute("UPDATE candidate_fact SET resolution='resolved', resolved_chef_id=?, resolved_branch_id=? WHERE candidate_id=?", (chef_id, branch_id, c['candidate_id']))
            res += 1
    ctx.conn.commit()
    return StageResult({'resolved': res, 'needs_review': needs})


# ───────────── 6. evidence verification ─────────────
def evidence_verification(ctx: StageContext) -> StageResult:
    out = {'verified': 0, 'source_claim': 0, 'unverified': 0, 'ai_inference': 0}
    for c in ctx.conn.execute("""SELECT c.*, s.tier FROM candidate_fact c JOIN source s ON s.source_id=c.source_id WHERE c.resolution='resolved'
                                   AND (c.verification IS NULL OR (c.fact_type='chef_move' AND c.applied_claim_id IS NULL)) ORDER BY c.rowid""").fetchall():
        if pending(c) or pending(json.loads(c['payload_json'])):
            v = 'ai_inference'
        elif json.loads(c['payload_json']).get('lead_only'):
            v = 'unverified'                   # search snippet / hearsay / Instagram: a candidate to check, never a fact on its own
        elif c['tier'] in ('social_lead', 'search_lead', 'specialist_blog'):
            v = 'unverified'                   # leads never become source_claim/verified on their own
        else:
            hosts = [(lambda e: (e['source_url'], e.get('quote')))(json.loads(r['evidence_json'])) for r in ctx.conn.execute(
                """SELECT x.evidence_json FROM candidate_fact x JOIN source s ON s.source_id=x.source_id
                   WHERE x.fact_type=? AND IFNULL(x.resolved_chef_id,'')=IFNULL(?,'') AND IFNULL(x.resolved_branch_id,'')=IFNULL(?,'')
                   AND x.resolution='resolved' AND x.asserted_by='source' AND s.tier IN ('official','press','human')
                   AND x.extractor NOT LIKE '%deepseek%' AND x.extractor NOT LIKE '%model:%' AND x.extractor NOT LIKE '%ai:%'
                   AND IFNULL(json_extract(x.payload_json,'$.pending_only'),0)=0
                   AND IFNULL(json_extract(x.payload_json,'$.asserted_by'),'source')<>'ai_inference'
                   AND IFNULL(json_extract(x.payload_json,'$.lead_only'),0)=0""",
                (c['fact_type'], c['resolved_chef_id'], c['resolved_branch_id']))]
            # 2 INDEPENDENT trusted sources: the same wording on two hosts is one syndicated/reposted article, not corroboration
            v = 'verified' if (c['tier'] in ('official', 'human') or independent_sources(hosts) >= 2) else 'source_claim'
        ctx.conn.execute('UPDATE candidate_fact SET verification=? WHERE candidate_id=?', (v, c['candidate_id']))
        out[v] += 1
    ctx.conn.commit()
    return StageResult(out)


# ───────────── 7. state / history update ─────────────
def _evidence_of(ctx, c):
    ev = json.loads(c['evidence_json'])
    return dict(source_id=c['source_id'], source_url=ev['source_url'], collected_at=ev['collected_at'], locator=ev.get('locator'),
                quote=ev.get('quote'), effective_from=ev.get('effective_from'), document_id=c['document_id'])


def _apply_status_event(ctx, c, p, kind):
    d = parse_iso_date(p.get('effective_date')) if p.get('effective_date') else None
    existing = ctx.conn.execute("SELECT event_id, effective_date, claim_id FROM restaurant_status_event WHERE branch_id=? AND kind=? AND state='active'", (c['resolved_branch_id'], kind)).fetchall()
    for e in existing:
        if e['effective_date'] and d and e['effective_date'] == d[0]:      # same fact again: add supporting evidence to the existing claim, no new event
            link_evidence(ctx.conn, e['claim_id'], add_evidence(ctx.conn, **_evidence_of(ctx, c)))
            return e['claim_id']
    claim = create_claim(ctx.conn, 'status_event', c['verification'], f'daemon:{c["extractor"]}', claim_id=stable_id('clm', 'cand', c['candidate_id']))
    link_evidence(ctx.conn, claim, add_evidence(ctx.conn, **_evidence_of(ctx, c)))
    detail = p.get('excerpt') or (f'{kind} announced (details not extracted)' if kind == 'move' else None)
    eid = stable_id('evt', 'cand', c['candidate_id'])
    ctx.conn.execute("""INSERT OR IGNORE INTO restaurant_status_event(event_id,branch_id,kind,effective_date,effective_precision,detail,claim_id,seed_ref,created_at)
                        VALUES (?,?,?,?,?,?,?,?,?)""", (eid, c['resolved_branch_id'], kind, d[0] if d else None, d[1] if d else None, detail, claim, f'cand:{c["candidate_id"]}', utcnow()))
    for e in existing:                                                       # disagreement on the date: keep both, flag
        if e['effective_date'] and d and e['effective_date'] != d[0]:
            open_conflict(ctx.conn, 'branch', c['resolved_branch_id'], f'{kind}.effective_date', e['claim_id'], claim)
    return claim


def state_history_update(ctx: StageContext) -> StageResult:
    applied = held = 0
    for c in ctx.conn.execute("SELECT * FROM candidate_fact WHERE resolution='resolved' AND verification IS NOT NULL AND applied_claim_id IS NULL ORDER BY rowid").fetchall():
        p = json.loads(c['payload_json'])
        claim = None
        if c['verification'] == 'ai_inference' or pending(c) or pending(p):        # AI inference is never materialised as a fact
            ctx.conn.execute("UPDATE candidate_fact SET resolution='needs_review' WHERE candidate_id=?", (c['candidate_id'],))
            queue_review(ctx.conn, 'ai_inference', 'candidate_fact', c['candidate_id'], {'fact_type': c['fact_type'], 'payload': p}, dedupe_key=f'ai_inference:{c["candidate_id"]}')
            held += 1
            continue
        ft = c['fact_type']
        if ft in ('closure', 'opening', 'relocation', 'pause'):
            claim = _apply_status_event(ctx, c, p, {'closure': 'close', 'opening': 'open', 'relocation': 'move', 'pause': 'pause'}[ft])
        elif ft in ('menu', 'collab'):
            # menus and collab / pop-up / guest events are dated EVENTS of a branch. Working alongside someone is never mentorship.
            claim = create_claim(ctx.conn, 'menu_event', c['verification'], f'daemon:{c["extractor"]}', claim_id=stable_id('clm', 'cand', c['candidate_id']))
            link_evidence(ctx.conn, claim, add_evidence(ctx.conn, **_evidence_of(ctx, c)))
            kind = 'popup' if '팝업' in (p.get('title') or '') else ft
            ctx.conn.execute('INSERT OR IGNORE INTO menu_event(menu_event_id,branch_id,kind,title,starts_on,claim_id,created_at) VALUES (?,?,?,?,?,?,?)',
                             (stable_id('mev', c['candidate_id']), c['resolved_branch_id'], kind, (p.get('title') or ft)[:120], p.get('effective_date'), claim, utcnow()))
        elif ft == 'rename':       # recorded as a dated status event carrying the new name; the display name is never auto-changed
            p = {**p, 'excerpt': f"rename -> {p.get('new_name') or '(new name not extracted)'}"[:200]}
            claim = _apply_status_event(ctx, c, p, 'rename')
        elif ft == 'employment':
            ex = ctx.conn.execute("SELECT employment_id, claim_id, valid_to_kind, state FROM employment WHERE chef_id=? AND branch_id=? AND state<>'rejected' ORDER BY valid_to_kind='open' DESC LIMIT 1", (c['resolved_chef_id'], c['resolved_branch_id'])).fetchone()
            if ex and p.get('past') and ex['valid_to_kind'] == 'open' and ex['state'] == 'active':
                # source says 'ex-/출신' while we hold this tenure as current: keep both, open a conflict for a human
                claim = create_claim(ctx.conn, 'employment_end', c['verification'], f'daemon:{c["extractor"]}', note='source describes this tenure as past',
                                     claim_id=stable_id('clm', 'cand', c['candidate_id']))
                link_evidence(ctx.conn, claim, add_evidence(ctx.conn, **_evidence_of(ctx, c)))
                cf = open_conflict(ctx.conn, 'employment', ex['employment_id'], 'employment.current_vs_past', ex['claim_id'], claim)
                queue_review(ctx.conn, 'conflict', 'conflict', cf, {'employment_id': ex['employment_id'], 'quote': json.loads(c['evidence_json']).get('quote')}, dedupe_key=f'conflict:{cf}')
            elif ex and p.get('current') and ex['valid_to_kind'] == 'open':
                link_evidence(ctx.conn, ex['claim_id'], add_evidence(ctx.conn, **_evidence_of(ctx, c)))
                ctx.conn.execute('UPDATE employment SET last_confirmed_at=MAX(IFNULL(last_confirmed_at,\'\'),?) WHERE employment_id=?', (json.loads(c['evidence_json'])['collected_at'][:10], ex['employment_id']))
                claim = ex['claim_id']
            elif not ex and p.get('current'):
                claim = create_claim(ctx.conn, 'employment', c['verification'], f'daemon:{c["extractor"]}', claim_id=stable_id('clm', 'cand', c['candidate_id']))
                link_evidence(ctx.conn, claim, add_evidence(ctx.conn, **_evidence_of(ctx, c)))
                from .ontology import add_employment
                add_employment(ctx.conn, chef_id=c['resolved_chef_id'], branch_id=c['resolved_branch_id'], claim_id=claim, role_text=p.get('role_text'), valid_from=p.get('effective_date') if parse_iso_date(p.get('effective_date') or '') else None,
                               valid_to_kind='open', last_confirmed_at=json.loads(c['evidence_json'])['collected_at'][:10], seed_ref=f'cand:{c["candidate_id"]}')
            elif ex:   # already known: this document just adds supporting evidence
                link_evidence(ctx.conn, ex['claim_id'], add_evidence(ctx.conn, **_evidence_of(ctx, c)))
                claim = ex['claim_id']
            else:
                claim = create_claim(ctx.conn, 'employment', c['verification'], f'daemon:{c["extractor"]}', claim_id=stable_id('clm', 'cand', c['candidate_id']))
                link_evidence(ctx.conn, claim, add_evidence(ctx.conn, **_evidence_of(ctx, c)))
                from .ontology import add_employment
                add_employment(ctx.conn, chef_id=c['resolved_chef_id'], branch_id=c['resolved_branch_id'], claim_id=claim, role_text=p.get('role_text'), valid_from=p.get('effective_date') if parse_iso_date(p.get('effective_date') or '') else None,
                               tenure_text=p.get('tenure_text'), valid_to_kind='unknown', seed_ref=f'cand:{c["candidate_id"]}')
        elif ft == 'chef_move':
            if c['verification'] != 'verified':
                # a move closes a tenure ONLY once verified (official/human source or 2 independent trusted hosts). Until then: candidate to check.
                queue_review(ctx.conn, 'move_needs_verification', 'candidate_fact', c['candidate_id'], {'payload': p, 'verification': c['verification']},
                             dedupe_key=f'move_needs_verification:{c["candidate_id"]}')
                held += 1
                continue
            same = ctx.conn.execute("SELECT employment_id, claim_id FROM employment WHERE chef_id=? AND branch_id=? AND state='active' AND valid_to_kind='open'",
                                    (c['resolved_chef_id'], c['resolved_branch_id'])).fetchone()
            if same:     # already there: confirmation, not a move
                link_evidence(ctx.conn, same['claim_id'], add_evidence(ctx.conn, **_evidence_of(ctx, c)))
                ctx.conn.execute('UPDATE candidate_fact SET applied_claim_id=? WHERE candidate_id=?', (same['claim_id'], c['candidate_id']))
                applied += 1
                continue
            if not (p.get('effective_date') and parse_iso_date(p['effective_date'])):
                ctx.conn.execute("UPDATE candidate_fact SET resolution='needs_review' WHERE candidate_id=?", (c['candidate_id'],))
                queue_review(ctx.conn, 'move_without_date', 'candidate_fact', c['candidate_id'], {'payload': p}, dedupe_key=f'move_without_date:{c["candidate_id"]}')
                held += 1
                continue
            try:
                r = move_chef(ctx.conn, chef_id=c['resolved_chef_id'], effective_date=p['effective_date'], evidence=_evidence_of(ctx, c), status=c['verification'],
                              created_by=f'daemon:{c["extractor"]}', to_branch_id=c['resolved_branch_id'], to_employer_label=None if c['resolved_branch_id'] else p.get('to_label'),
                              role_text=p.get('role_text'))
                claim = r['claim_id']
            except AmbiguousMove:
                ctx.conn.execute("UPDATE candidate_fact SET resolution='needs_review' WHERE candidate_id=?", (c['candidate_id'],))
                held += 1
                continue
        elif ft == 'mentorship':
            if not p.get('assertion_text'):
                ctx.conn.execute("UPDATE candidate_fact SET resolution='rejected' WHERE candidate_id=?", (c['candidate_id'],))
                continue
            claim = create_claim(ctx.conn, 'mentorship', c['verification'], f'daemon:{c["extractor"]}', claim_id=stable_id('clm', 'cand', c['candidate_id']))
            link_evidence(ctx.conn, claim, add_evidence(ctx.conn, **_evidence_of(ctx, c)))
            ctx.conn.execute('INSERT OR IGNORE INTO mentorship_assertion(mentorship_id,mentee_chef_id,mentor_label,claim_id,seed_ref,created_at) VALUES (?,?,?,?,?,?)',
                             (stable_id('mnt', c['candidate_id']), c['resolved_chef_id'], p.get('mentor_label') or 'unspecified', claim, f'cand:{c["candidate_id"]}', utcnow()))
        if claim:
            ctx.conn.execute('UPDATE candidate_fact SET applied_claim_id=? WHERE candidate_id=?', (claim, c['candidate_id']))
            applied += 1
    ctx.conn.commit()
    return StageResult({'applied': applied, 'held_for_review': held})


# ───────────── 8. service output recompute ─────────────
def service_output_recompute(ctx: StageContext) -> StageResult:
    day = ctx.env.get('today') or utcnow()[:10]
    n = 0
    for ch in ctx.conn.execute('SELECT chef_id FROM chef').fetchall():
        payload = {'current': current_employment(ctx.conn, ch['chef_id'], day), 'pending_moves': pending_moves(ctx.conn, ch['chef_id'])}
        ctx.conn.execute("""INSERT INTO service_output(output_key,subject_id,as_of,payload_json,min_status,computed_at,run_id) VALUES ('current_employment',?,?,?,?,?,?)
                            ON CONFLICT(output_key,subject_id,as_of) DO UPDATE SET payload_json=excluded.payload_json, computed_at=excluded.computed_at, run_id=excluded.run_id""",
                         (ch['chef_id'], day, json.dumps(payload, ensure_ascii=False, sort_keys=True), 'source_claim', utcnow(), ctx.run_id))
        n += 1
    for b in ctx.conn.execute('SELECT branch_id FROM branch').fetchall():
        payload = branch_state_asof(ctx.conn, b['branch_id'], day)
        ctx.conn.execute("""INSERT INTO service_output(output_key,subject_id,as_of,payload_json,min_status,computed_at,run_id) VALUES ('branch_status',?,?,?,?,?,?)
                            ON CONFLICT(output_key,subject_id,as_of) DO UPDATE SET payload_json=excluded.payload_json, computed_at=excluded.computed_at, run_id=excluded.run_id""",
                         (b['branch_id'], day, json.dumps(payload, ensure_ascii=False, sort_keys=True), 'source_claim', utcnow(), ctx.run_id))
        n += 1
    n += _recompute_product_views(ctx, day)
    ctx.conn.commit()
    return StageResult({'outputs_written': n, 'as_of': day})


def _upsert_out(ctx, key, subject, day, payload):
    ctx.conn.execute("""INSERT INTO service_output(output_key,subject_id,as_of,payload_json,min_status,computed_at,run_id) VALUES (?,?,?,?,?,?,?)
                        ON CONFLICT(output_key,subject_id,as_of) DO UPDATE SET payload_json=excluded.payload_json, computed_at=excluded.computed_at, run_id=excluded.run_id""",
                     (key, subject, day, json.dumps(payload, ensure_ascii=False, sort_keys=True), 'source_claim', utcnow(), ctx.run_id))


def _recompute_product_views(ctx, day) -> int:
    """Service data the product reads (no frontend here): restaurant_detail, career_map, recommendation_eligibility.
    All three derive from employments_asof/current_employment, so a verified move (old tenure closed, new opened) flows into every view
    in the same run, and a closed tenure can never be listed as current."""
    from .ontology import employments_asof
    q = ctx.conn.execute
    n = 0
    names = {r['chef_id']: r['display_name'] for r in q('SELECT chef_id, display_name FROM chef')}
    rest_of = {r['branch_id']: (r['restaurant_id'], r['name']) for r in q('SELECT b.branch_id, b.restaurant_id, r.name FROM branch b JOIN restaurant r ON r.restaurant_id=b.restaurant_id')}
    current_by_branch, past_by_branch = {}, {}
    for cid, nm in names.items():
        cur = current_employment(ctx.conn, cid, day)
        cur_ids = {e['employment_id'] for e in cur}
        past = []
        for e in q("""SELECT e.employment_id, e.branch_id, e.employer_label, e.role_text, e.valid_from, e.valid_to, e.valid_to_kind, c.status
                      FROM employment e JOIN claim c ON c.claim_id=e.claim_id WHERE e.chef_id=? AND e.state<>'rejected' ORDER BY e.valid_from""", (cid,)):
            if e['employment_id'] in cur_ids or RANK[e['status']] < RANK['source_claim']:
                continue
            past.append({'employment_id': e['employment_id'], 'branch_id': e['branch_id'], 'restaurant': rest_of.get(e['branch_id'], (None, e['employer_label']))[1],
                         'role_text': e['role_text'], 'valid_from': e['valid_from'], 'valid_to': e['valid_to'],
                         'end': 'known' if e['valid_to_kind'] == 'known' else 'not_stated', 'claim_status': e['status']})
            past_by_branch.setdefault(e['branch_id'], []).append({'chef_id': cid, 'name': nm, 'valid_to': e['valid_to']})
        current = [{**e, 'restaurant': rest_of.get(e['branch_id'], (None, e['employer_label']))[1]} for e in cur]
        for e in cur:
            current_by_branch.setdefault(e['branch_id'], []).append({'chef_id': cid, 'name': nm, 'role_text': e['role_text'], 'claim_status': e['claim_status'], 'end_pending': e['end_pending']})
        mentors = [{'mentor_label': m['mentor_label'], 'claim_status': m['status']} for m in q(
            """SELECT m.mentor_label, c.status FROM mentorship_assertion m JOIN claim c ON c.claim_id=m.claim_id WHERE m.mentee_chef_id=?""", (cid,)) if RANK[m['status']] >= RANK['source_claim']]
        _upsert_out(ctx, 'career_map', cid, day, {'chef_id': cid, 'name': nm, 'current': current, 'past': past,
                                                  'mentorship': mentors,       # source-asserted only; co-working / collab events are NOT mentorship
                                                  'pending_moves': pending_moves(ctx.conn, cid)})
        n += 1
    for bid, (rid, rname) in rest_of.items():
        st = branch_state_asof(ctx.conn, bid, day)
        events = [dict(r) for r in q("""SELECT m.kind, m.title, m.starts_on, c.status FROM menu_event m JOIN claim c ON c.claim_id=m.claim_id
                                        WHERE m.branch_id=? AND c.status IN ('verified','source_claim') ORDER BY m.created_at DESC LIMIT 10""", (bid,))]
        cur = current_by_branch.get(bid, [])
        _upsert_out(ctx, 'restaurant_detail', bid, day, {'branch_id': bid, 'restaurant': rname, 'state': st['state'], 'current_chefs': cur,
                                                         'former_chefs': past_by_branch.get(bid, []), 'events': events})
        eligible = st['state'] in ('open', 'unknown') and bool(cur)
        _upsert_out(ctx, 'recommendation_eligibility', bid, day, {'branch_id': bid, 'eligible': eligible,
                                                                  'reason': None if eligible else ('not_open' if st['state'] not in ('open', 'unknown') else 'no_current_chef'),
                                                                  'chef_ids': [x['chef_id'] for x in cur]})
        n += 2
    return n


# ───────────── 9. quality check ─────────────
def run_quality_checks(conn, today: str, stale_days: int = 180) -> dict:
    """-> {'violations': [...blocking...], 'warnings': [...]}"""
    v, w = [], []
    q = lambda sql, *a: conn.execute(sql, a).fetchall()   # noqa: E731
    for r in q("""SELECT c.claim_id, c.status FROM claim c WHERE c.status IN ('verified','source_claim')
                  AND NOT EXISTS (SELECT 1 FROM claim_evidence ce WHERE ce.claim_id=c.claim_id AND ce.stance='supports')"""):
        v.append(('claim_without_evidence', r['claim_id']))
    for tbl, col in (('employment', 'claim_id'), ('restaurant_status_event', 'claim_id'), ('mentorship_assertion', 'claim_id')):
        for r in q(f"SELECT t.{col} cid FROM {tbl} t JOIN claim c ON c.claim_id=t.{col} WHERE c.status='ai_inference'"):
            v.append(('ai_inference_materialised', f'{tbl}:{r["cid"]}'))
    for r in q("""SELECT claim_id FROM claim WHERE status='verified' AND claim_id IN (SELECT claim_id FROM claim_evidence WHERE stance='supports')
                  AND NOT EXISTS (SELECT 1 FROM claim_evidence ce JOIN evidence e ON e.evidence_id=ce.evidence_id JOIN source s ON s.source_id=e.source_id
                                  WHERE ce.claim_id=claim.claim_id AND s.tier IN ('official','press','human'))"""):
        v.append(('verified_from_leads_only', r['claim_id']))
    for r in q("""SELECT m.mentorship_id FROM mentorship_assertion m WHERE NOT EXISTS (SELECT 1 FROM claim_evidence ce WHERE ce.claim_id=m.claim_id AND ce.stance='supports')"""):
        v.append(('mentorship_without_source', r['mentorship_id']))
    for r in q("SELECT employment_id FROM employment WHERE valid_to_kind='open' AND state<>'active'"):
        v.append(('open_end_on_inactive_employment', r['employment_id']))
    for r in q("""SELECT e.employment_id, e.last_confirmed_at FROM employment e WHERE e.valid_to_kind='open' AND e.state='active'"""):
        lc = r['last_confirmed_at']
        try:
            if not lc or (dt.date.fromisoformat(today) - dt.date.fromisoformat(lc[:10])).days > stale_days:
                w.append(('stale_open_employment', r['employment_id']))
        except ValueError:
            w.append(('stale_open_employment', r['employment_id']))
    for r in q("""SELECT e.employment_id FROM employment e JOIN restaurant_status_event s ON s.branch_id=e.branch_id AND s.kind='close' AND s.state='active'
                  WHERE e.valid_to_kind='open' AND e.state='active' AND s.effective_date IS NOT NULL AND s.effective_date <= ?""", today):
        w.append(('open_employment_at_closed_branch', r['employment_id']))
    for r in q("SELECT subject_id, payload_json FROM service_output WHERE output_key='career_map' AND as_of=?", today):
        for e in json.loads(r['payload_json'])['current']:
            row = conn.execute('SELECT state, valid_to_kind, valid_to FROM employment WHERE employment_id=?', (e['employment_id'],)).fetchone()
            if row and (row['valid_to_kind'] == 'unknown' or (row['valid_to_kind'] == 'known' and row['valid_to'] <= today and not e.get('end_pending'))):
                v.append(('past_shown_as_current', e['employment_id']))
    for r in q("SELECT m.mentorship_id FROM mentorship_assertion m JOIN claim c ON c.claim_id=m.claim_id WHERE c.created_by LIKE '%collab%'"):
        v.append(('collab_materialised_as_mentorship', r['mentorship_id']))
    for r in q("SELECT conflict_id FROM conflict WHERE state='open'"):
        w.append(('open_conflict', r['conflict_id']))
    return {'violations': v, 'warnings': w}


def quality_check(ctx: StageContext) -> StageResult:
    day = ctx.env.get('today') or utcnow()[:10]
    res = run_quality_checks(ctx.conn, day, ctx.config.get('stale_open_employment_days', 180))
    for kind, subj in res['warnings']:
        queue_review(ctx.conn, f'quality:{kind}', 'quality', subj, {'kind': kind}, dedupe_key=f'quality:{kind}:{subj}:{day[:7]}')
    ctx.conn.commit()
    if res['violations']:
        raise NonRetryable(f'invariant violations: {res["violations"][:5]} (+{max(0, len(res["violations"]) - 5)} more)')
    from collections import Counter
    return StageResult({'warnings': dict(Counter(k for k, _ in res['warnings']))})


HANDLERS = {
    'source_discovery': source_discovery, 'incremental_fetch': incremental_fetch, 'change_detection': change_detection,
    'extraction': extraction, 'entity_resolution': entity_resolution, 'evidence_verification': evidence_verification,
    'state_history_update': state_history_update, 'service_output_recompute': service_output_recompute, 'quality_check': quality_check,
}
