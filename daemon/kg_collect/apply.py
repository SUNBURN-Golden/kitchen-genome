"""Apply step: labelled extractions -> service data THROUGH claims. Detects affiliation changes (old vs new) and records them in field_history
with detection latency. Never fills a value from a model; every write carries an evidence row (short quote + url + extraction locator)."""
import datetime as dt
import json

from kg_daemon import claims as cl
from kg_daemon import ontology as ont
from kg_daemon.db import stable_id, utcnow

BY = 'collect:apply'
TIER_KIND = {'official': 'official_site', 'press': 'press', 'specialist_blog': 'community', 'event_notice': 'press', 'social_lead': 'social', 'search_lead': 'community', 'human': 'manual_clip'}


def ensure_source(conn, host: str, tier: str, now: str) -> str:
    sid = 'bot:' + host
    t = tier if tier in TIER_KIND else 'search_lead'
    conn.execute('INSERT OR IGNORE INTO source(source_id,name,kind,host,tier,enabled,created_at) VALUES (?,?,?,?,?,?,?)', (sid, f'collected: {host}', TIER_KIND[t], host, t, 1, now))
    return sid


def service_chef(conn, name_ko: str):
    rows = conn.execute('SELECT chef_id FROM chef WHERE display_name=?', (name_ko,)).fetchall()
    return rows[0]['chef_id'] if len(rows) == 1 else None


def service_branch(conn, name_ko: str):
    rows = conn.execute("SELECT b.branch_id FROM restaurant r JOIN branch b USING(restaurant_id) WHERE r.name=? AND b.label='main'", (name_ko,)).fetchall()
    return rows[0]['branch_id'] if len(rows) == 1 else None


def _current(conn, chef_id):
    """current open employments with their branch's restaurant name (or free-text employer label)."""
    return conn.execute("""SELECT e.employment_id, e.branch_id, e.employer_label, e.valid_from, e.last_confirmed_at, r.name AS rname FROM employment e
                           LEFT JOIN branch b ON b.branch_id=e.branch_id LEFT JOIN restaurant r ON r.restaurant_id=b.restaurant_id
                           WHERE e.chef_id=? AND e.state='active' AND e.valid_to_kind='open'""", (chef_id,)).fetchall()


LIVE_WINDOW_DAYS = 14          # latency is only meaningful for a LIVE change; an old article found by backfill is history, not a detection delay


def _latency_hours(published: str | None, detected: str):
    if not published or len(published) < 10:
        return None
    try:
        p = dt.datetime.fromisoformat(published[:10]).replace(tzinfo=dt.timezone.utc)
        h = round((dt.datetime.fromisoformat(detected.replace('Z', '+00:00')) - p).total_seconds() / 3600, 1)
        return h if h <= LIVE_WINDOW_DAYS * 24 else None
    except ValueError:
        return None


def apply_affiliation(conn, now: str | None = None, tier_of=lambda host: 'press') -> dict:
    """employment_claim(chef X works at restaurant Y; claim/fact, no ambiguity, both resolved, mapped to service rows) vs the chef's current open employment.
    Same restaurant => confirmation only. Different => field_history('affiliation') + a review item (candidate move). Tenures are never changed here."""
    now = now or utcnow()
    out = {'checked': 0, 'confirmed': 0, 'changes': 0, 'skipped_unmapped': 0, 'skipped_ambiguous_move': 0, 'already_recorded': 0}
    rows = conn.execute("""SELECT e.*, tc.name_ko chef_name, tr.name_ko rest_name FROM extraction e JOIN target tc ON tc.target_id=e.resolved_chef_target
                           JOIN target tr ON tr.target_id=e.resolved_restaurant_target WHERE e.label='employment_claim' AND e.epistemic IN ('claim','fact') AND asserted_by<>'ai_inference' AND pending_only=0 AND extractor NOT LIKE '%deepseek%' AND extractor NOT LIKE '%model:%' AND extractor NOT LIKE '%ai:%' AND e.ambiguity IS NULL
                           ORDER BY COALESCE(e.published_at,''), e.created_at""").fetchall()
    for e in rows:
        out['checked'] += 1
        chef_id, branch = service_chef(conn, e['chef_name']), service_branch(conn, e['rest_name'])
        if not chef_id or not branch:
            out['skipped_unmapped'] += 1
            continue
        cur = _current(conn, chef_id)
        if any(c['branch_id'] == branch for c in cur):
            out['confirmed'] += 1
            continue
        if conn.execute("SELECT 1 FROM field_history WHERE new_extraction=? AND field='affiliation'", (e['extraction_id'],)).fetchone():
            out['already_recorded'] += 1
            continue
        pub = e['published_at'] or ''
        newest = max((c['last_confirmed_at'] or c['valid_from'] or '' for c in cur), default='')
        if cur and pub and newest and pub[:10] < newest[:10]:
            continue                                  # the new statement is OLDER than what we hold: not a change
        old_val = '; '.join(sorted({c['rname'] or c['employer_label'] or '?' for c in cur})) or None
        src = ensure_source(conn, e['host'], tier_of(e['host']), now)
        evidence = dict(source_id=src, source_url=e['canonical_url'], collected_at=now, quote=e['quote'][:200], locator={'extraction_id': e['extraction_id'], 'span': [e['span_start'], e['span_end']]},
                        published_at=e['published_at'], effective_from=(e['published_at'] or '')[:10] or None)
        # A single collected statement NEVER closes a tenure (and never with the fetch date as the end): the change is recorded and queued as a
        # candidate move; only the daemon's verified path (official/human source or 2 independent trusted sources, stated date) may move a chef.
        cl.queue_review(conn, 'move_needs_verification', 'chef', chef_id, {'to_branch_id': branch, 'from': old_val, 'evidence': evidence, 'stated_date': e['date_iso'] or e['date_text']},
                        dedupe_key=f'move_needs_verification:collect:{e["extraction_id"]}')
        conn.execute("INSERT INTO field_history(subject_id,field,old_value,new_value,old_extraction,new_extraction,published_at,detected_at,latency_hours) VALUES (?,?,?,?,?,?,?,?,?)",
                     (e['resolved_chef_target'], 'affiliation', old_val, e['rest_name'], None, e['extraction_id'], e['published_at'], now, _latency_hours(e['published_at'], now)))
        out['changes'] += 1
    conn.commit()
    return out


STATUS_KIND = {'opening': 'open', 'closure': 'close', 'relocation': 'move', 'rename': 'rename'}


def apply_status(conn, now: str | None = None, tier_of=lambda host: 'press') -> dict:
    """opening/closure/relocation/rename on a RESOLVED, mapped restaurant => restaurant_status_event under a claim (never 'verified' here: the verification stage promotes).
    Hearsay/unverified, ambiguous or planned statements are NOT applied: they stay in the extraction layer."""
    now = now or utcnow()
    out = {'events': 0, 'skipped': 0, 'history': 0}
    for e in conn.execute("""SELECT e.*, tr.name_ko rest_name FROM extraction e JOIN target tr ON tr.target_id=e.resolved_restaurant_target
                             WHERE e.label IN ('opening','closure','relocation','rename') AND e.epistemic IN ('claim','fact') AND asserted_by<>'ai_inference' AND pending_only=0 AND extractor NOT LIKE '%deepseek%' AND extractor NOT LIKE '%model:%' AND extractor NOT LIKE '%ai:%' AND e.ambiguity IS NULL""").fetchall():
        branch = service_branch(conn, e['rest_name'])
        if not branch:
            out['skipped'] += 1
            continue
        eid = stable_id('evt', 'collect', e['extraction_id'])
        if conn.execute('SELECT 1 FROM restaurant_status_event WHERE event_id=?', (eid,)).fetchone():
            continue
        kind = STATUS_KIND[e['label']]
        d = e['date_iso'] or e['date_text'] or ''
        if kind in ('open', 'close') and conn.execute('SELECT 1 FROM restaurant_status_event WHERE branch_id=? AND kind=? AND COALESCE(effective_date,"")=COALESCE(?,"") AND state=\'active\'', (branch, kind, d or None)).fetchone():
            out['skipped'] += 1                       # same event already on file (seed or an earlier source): corroboration, not a duplicate row
            continue
        status = 'unverified' if e['epistemic'] == 'unverified' else 'source_claim'
        cid = cl.create_claim(conn, 'status_event', status, BY, claim_id=stable_id('clm', 'collect', e['extraction_id']), now=now)
        src = ensure_source(conn, e['host'], tier_of(e['host']), now)
        ev = cl.add_evidence(conn, source_id=src, source_url=e['canonical_url'], collected_at=now, quote=e['quote'][:200], published_at=e['published_at'],
                             locator={'extraction_id': e['extraction_id'], 'span': [e['span_start'], e['span_end']]})
        cl.link_evidence(conn, cid, ev)
        prec = 'day' if len(d) == 10 else 'month' if len(d) == 7 else 'year' if len(d) == 4 else None
        conn.execute("""INSERT OR IGNORE INTO restaurant_status_event(event_id,branch_id,kind,effective_date,effective_precision,detail,claim_id,created_at) VALUES (?,?,?,?,?,?,?,?)""",
                     (eid, branch, kind if kind != 'move' else 'move', d or None, prec, e['quote'][:200], cid, now))
        if kind in ('close', 'move', 'rename') and e['epistemic'] in ('claim', 'fact'):
            conn.execute('INSERT INTO field_history(subject_id,field,old_value,new_value,old_extraction,new_extraction,published_at,detected_at,latency_hours) VALUES (?,?,?,?,?,?,?,?,?)',
                         (e['resolved_restaurant_target'], 'status', 'open', kind, None, e['extraction_id'], e['published_at'], now, _latency_hours(e['published_at'], now)))
            out['history'] += 1
        out['events'] += 1
    conn.commit()
    return out


def revert_unverified_collect_moves(conn, now: str | None = None, by: str = 'repair:rules-1.4') -> dict:
    """Undo tenure changes made by the old apply path (a single unverified statement closed the prior employment at the FETCH date).
    Nothing is deleted: the closed prior row is reopened, rows the move created are marked 'rejected', every change goes to correction_history. Idempotent."""
    now = now or utcnow()
    out = {'reopened': 0, 'rejected': 0}
    bad = [r['claim_id'] for r in conn.execute("SELECT claim_id FROM claim WHERE created_by=? AND note LIKE 'chef move:%' AND status IN ('unverified','ai_inference')", (BY,))]
    for cid in bad:
        for r in conn.execute("SELECT * FROM employment WHERE end_claim_id=? AND claim_id NOT IN (%s)" % ','.join('?' * len(bad)), (cid, *bad)).fetchall():
            conn.execute("UPDATE employment SET state='active', valid_to=NULL, valid_to_precision=NULL, valid_to_kind='open', end_claim_id=NULL WHERE employment_id=?", (r['employment_id'],))
            conn.execute('INSERT INTO correction_history(table_name,row_id,field,old_value,new_value,reason,corrected_by,corrected_at) VALUES (?,?,?,?,?,?,?,?)',
                         ('employment', r['employment_id'], 'state/valid_to', f"closed/{r['valid_to']}", 'active/open', 'closed by an unverified single-source move at the fetch date', by, now))
            out['reopened'] += 1
        for r in conn.execute("SELECT * FROM employment WHERE claim_id=? AND (state<>'rejected' OR valid_to_kind='open')", (cid,)).fetchall():
            # a rejected row must not keep an open end (quality invariant open_end_on_inactive_employment)
            conn.execute("UPDATE employment SET state='rejected', valid_to_kind=CASE WHEN valid_to_kind='open' THEN 'unknown' ELSE valid_to_kind END WHERE employment_id=?", (r['employment_id'],))
            conn.execute('INSERT INTO correction_history(table_name,row_id,field,old_value,new_value,reason,corrected_by,corrected_at) VALUES (?,?,?,?,?,?,?,?)',
                         ('employment', r['employment_id'], 'state', r['state'], 'rejected', 'created by an unverified single-source move; kept as a candidate in review', by, now))
            out['rejected'] += 1
    conn.commit()
    return out
