"""Time-aware operations over the ontology: dates, employment moves, as-of queries, status timeline.
Nothing here deletes. A chef move CLOSES the prior employment and ADDS a new one pending verification."""
import calendar
import re

from .claims import RANK, add_evidence, create_claim, link_evidence, queue_review
from .db import new_id, stable_id, utcnow

_DATE = re.compile(r'^(\d{4})(?:-(\d{2}))?(?:-(\d{2}))?$')


class AmbiguousMove(Exception):
    """Chef has several open employments and the caller did not say which one the move ends."""


def parse_iso_date(text):
    """'2024' | '2024-05' | '2024-05-17' -> (iso, precision) or None. Anything else is NOT guessed."""
    if not isinstance(text, str):
        return None
    m = _DATE.match(text.strip())
    if not m:
        return None
    y, mo, d = m.group(1), m.group(2), m.group(3)
    if mo and not 1 <= int(mo) <= 12:
        return None
    if d and not (mo and 1 <= int(d) <= calendar.monthrange(int(y), int(mo))[1]):
        return None
    return (text.strip(), 'day' if d else 'month' if mo else 'year')


def date_range(iso: str) -> tuple[str, str]:
    """Inclusive [start, end] day range covered by a date of any precision."""
    p = parse_iso_date(iso)
    if not p:
        raise ValueError(iso)
    y, mo, d = (iso.split('-') + [None, None])[:3]
    if d:
        return iso, iso
    if mo:
        return f'{y}-{mo}-01', f'{y}-{mo}-{calendar.monthrange(int(y), int(mo))[1]:02d}'
    return f'{y}-01-01', f'{y}-12-31'


def compare_to(iso: str, day: str) -> str:
    """Where does a (possibly coarse) date fall relative to a day: 'on_or_before' | 'after' | 'ambiguous'."""
    start, end = date_range(iso)
    if end <= day:
        return 'on_or_before'
    if start > day:
        return 'after'
    return 'ambiguous'


def add_employment(conn, *, chef_id, claim_id, branch_id=None, employer_label=None, role_id=None, role_text=None,
                   tenure_text=None, valid_from=None, valid_to=None, valid_to_kind='unknown', last_confirmed_at=None,
                   seed_ref=None, now=None) -> str:
    if seed_ref:
        row = conn.execute('SELECT employment_id FROM employment WHERE seed_ref=?', (seed_ref,)).fetchone()
        if row:
            return row['employment_id']
    vf = parse_iso_date(valid_from) if valid_from else None
    vt = parse_iso_date(valid_to) if valid_to else None
    if valid_from and not vf or valid_to and not vt:
        raise ValueError('valid_from/valid_to must be YYYY, YYYY-MM or YYYY-MM-DD; keep raw wording in role_text/tenure_text')
    eid = new_id('emp')
    conn.execute("""INSERT INTO employment(employment_id,chef_id,branch_id,employer_label,role_id,role_text,tenure_text,
        valid_from,valid_from_precision,valid_to,valid_to_precision,valid_to_kind,last_confirmed_at,claim_id,seed_ref,created_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                 (eid, chef_id, branch_id, employer_label, role_id, role_text, tenure_text,
                  vf[0] if vf else None, vf[1] if vf else None, vt[0] if vt else None, vt[1] if vt else None,
                  valid_to_kind, last_confirmed_at, claim_id, seed_ref, now or utcnow()))
    return eid


def close_employment(conn, employment_id, effective_date, end_claim_id) -> None:
    """End an employment (never delete). The end carries its own claim so an unverified move can be shown as pending."""
    p = parse_iso_date(effective_date)
    if not p:
        raise ValueError('close_employment needs a parseable effective_date')
    conn.execute("""UPDATE employment SET valid_to=?, valid_to_precision=?, valid_to_kind='known', state='closed', end_claim_id=?
                    WHERE employment_id=?""", (p[0], p[1], end_claim_id, employment_id))


def open_employments(conn, chef_id):
    return conn.execute("SELECT * FROM employment WHERE chef_id=? AND state='active' AND valid_to_kind='open' ORDER BY created_at", (chef_id,)).fetchall()


def move_chef(conn, *, chef_id, effective_date, evidence: dict, status='unverified', created_by='daemon:state_update',
              to_branch_id=None, to_employer_label=None, role_text=None, from_employment_id=None, now=None) -> dict:
    """Chef moved. Closes the prior open employment at effective_date and adds a new employment whose claim is
    `status` (default 'unverified' = pending verification). Raises AmbiguousMove instead of guessing."""
    if not (to_branch_id or to_employer_label):
        raise ValueError('destination required')
    if from_employment_id:
        prior = conn.execute('SELECT * FROM employment WHERE employment_id=? AND chef_id=?', (from_employment_id, chef_id)).fetchone()
        if prior is None:
            raise KeyError(from_employment_id)
        priors = [prior]
    else:
        priors = open_employments(conn, chef_id)
    if len(priors) > 1:
        queue_review(conn, 'ambiguous_move', 'chef', chef_id, {'open_employments': [p['employment_id'] for p in priors], 'destination': to_branch_id or to_employer_label},
                     dedupe_key=f'ambiguous_move:{chef_id}:{to_branch_id or to_employer_label}:{effective_date}')
        raise AmbiguousMove(chef_id)
    claim = create_claim(conn, 'employment', status, created_by, note='chef move: closes prior employment, new one pending verification', now=now)
    ev = add_evidence(conn, **evidence)
    link_evidence(conn, claim, ev)
    closed = None
    if priors:
        closed = priors[0]['employment_id']
        close_employment(conn, closed, effective_date, claim)     # same claim: the end is only as certain as the move
    new_emp = add_employment(conn, chef_id=chef_id, claim_id=claim, branch_id=to_branch_id, employer_label=to_employer_label,
                             role_text=role_text, valid_from=effective_date, valid_to_kind='open', last_confirmed_at=evidence['collected_at'][:10], now=now)
    if status in ('unverified', 'ai_inference'):
        queue_review(conn, 'pending_verification', 'employment', new_emp, {'closed_employment': closed, 'claim_id': claim},
                     dedupe_key=f'pending_verification:{new_emp}')
    return {'closed_employment': closed, 'new_employment': new_emp, 'claim_id': claim}


# ───────────────────────── as-of queries ─────────────────────────
def _admit(status, min_status):
    return RANK[status] >= RANK[min_status]


def branch_state_asof(conn, branch_id, day, min_status='source_claim') -> dict:
    """'open' | 'paused' | 'closed' | 'moved' | 'unknown' | 'uncertain' at `day` from dated, admissible events.
    Undated events are reported, never guessed into the timeline. Coarse dates that straddle `day` -> 'uncertain'."""
    rows = conn.execute("""SELECT e.*, c.status AS claim_status FROM restaurant_status_event e JOIN claim c ON c.claim_id=e.claim_id
                           WHERE e.branch_id=? AND e.state='active'""", (branch_id,)).fetchall()
    dated, undated, ambiguous = [], 0, []
    for r in rows:
        if not _admit(r['claim_status'], min_status):
            continue
        if not r['effective_date']:
            undated += 1
            continue
        pos = compare_to(r['effective_date'], day)
        if pos == 'ambiguous':
            ambiguous.append(r['event_id'])
        elif pos == 'on_or_before':
            dated.append(r)
    dated.sort(key=lambda r: date_range(r['effective_date'])[1])
    state = 'unknown'
    for r in dated:
        state = {'open': 'open', 'reopen': 'open', 'pause': 'paused', 'close': 'closed', 'move': 'moved'}.get(r['kind'], state)
    if ambiguous:
        state = 'uncertain'
    return {'branch_id': branch_id, 'as_of': day, 'state': state, 'undated_events': undated, 'ambiguous_events': ambiguous}


def employments_asof(conn, chef_id, day, min_status='source_claim') -> list[dict]:
    """Employments that apply on `day`. Rules:
      * employment claim below min_status -> excluded (an unverified move is not shown as fact)
      * an end whose claim is below min_status is treated as not-yet-ended, flagged end_pending
      * valid_to_kind 'unknown' is never current; unknown valid_from can't be placed before `day`, so it is only returned for 'now' semantics via open ends
      * branch closed/moved at `day` -> not current (branch_closed flag)"""
    rows = conn.execute("""SELECT e.*, c.status AS claim_status, ec.status AS end_status FROM employment e
                           JOIN claim c ON c.claim_id=e.claim_id LEFT JOIN claim ec ON ec.claim_id=e.end_claim_id
                           WHERE e.chef_id=? AND e.state<>'rejected'""", (chef_id,)).fetchall()
    out = []
    for r in rows:
        if not _admit(r['claim_status'], min_status):
            continue
        if r['valid_from'] and compare_to(r['valid_from'], day) == 'after':
            continue
        end_pending = end_ambiguous = False
        kind = r['valid_to_kind']
        if kind == 'known':
            if r['end_status'] and not _admit(r['end_status'], min_status):
                end_pending, kind = True, 'open'
            else:
                pos = compare_to(r['valid_to'], day)
                if pos == 'on_or_before':
                    continue                              # ended on/before day (exclusive end)
                end_ambiguous = pos == 'ambiguous'        # coarse end date straddles `day`: keep, but flag
        if kind == 'unknown':
            continue
        branch_closed = False
        if r['branch_id']:
            bs = branch_state_asof(conn, r['branch_id'], day, min_status)['state']
            branch_closed = bs in ('closed', 'moved')
        out.append({'employment_id': r['employment_id'], 'branch_id': r['branch_id'], 'employer_label': r['employer_label'],
                    'role_text': r['role_text'], 'valid_from': r['valid_from'], 'valid_to': r['valid_to'],
                    'claim_status': r['claim_status'], 'end_pending': end_pending, 'end_ambiguous': end_ambiguous, 'branch_closed': branch_closed,
                    'last_confirmed_at': r['last_confirmed_at']})
    return out


def current_employment(conn, chef_id, day, min_status='source_claim') -> list[dict]:
    return [e for e in employments_asof(conn, chef_id, day, min_status) if not e['branch_closed']]


def pending_moves(conn, chef_id) -> list[dict]:
    """Move candidates below public admission (shown to reviewers, not as facts)."""
    return [dict(r) for r in conn.execute("""SELECT e.employment_id, e.branch_id, e.employer_label, e.valid_from, c.status
        FROM employment e JOIN claim c ON c.claim_id=e.claim_id
        WHERE e.chef_id=? AND c.status IN ('unverified','ai_inference') AND e.state='active'""", (chef_id,))]


def alumni_of(conn, kitchen_restaurant_id, min_status='source_claim') -> list[dict]:
    """Chefs with a non-rejected employment at the kitchen whose end is known or unrecorded (i.e. 'alumni'),
    with the openings a source attributes to them. Does not assert mentorship."""
    out = {}
    for r in conn.execute("""
        SELECT ch.chef_id, ch.display_name, e.employment_id, e.role_text, c.status AS claim_status
        FROM employment e JOIN branch b ON b.branch_id=e.branch_id JOIN chef ch ON ch.chef_id=e.chef_id JOIN claim c ON c.claim_id=e.claim_id
        WHERE b.restaurant_id=? AND e.state<>'rejected' AND e.valid_to_kind IN ('known','unknown')""", (kitchen_restaurant_id,)):
        if RANK[r['claim_status']] < RANK[min_status]:
            continue
        a = out.setdefault(r['chef_id'], {'chef_id': r['chef_id'], 'display_name': r['display_name'], 'employments': [], 'opened': []})
        a['employments'].append({'employment_id': r['employment_id'], 'role_text': r['role_text'], 'claim_status': r['claim_status']})
    for cid, a in out.items():
        for r in conn.execute("""SELECT e.branch_id, rs.name, e.effective_date, c.status AS claim_status FROM restaurant_status_event e
            JOIN branch b ON b.branch_id=e.branch_id JOIN restaurant rs ON rs.restaurant_id=b.restaurant_id JOIN claim c ON c.claim_id=e.claim_id
            WHERE e.by_chef_id=? AND e.kind='open' AND e.state='active'""", (cid,)):
            if RANK[r['claim_status']] >= RANK[min_status]:
                a['opened'].append({'branch_id': r['branch_id'], 'restaurant': r['name'], 'effective_date': r['effective_date'], 'claim_status': r['claim_status']})
    return sorted(out.values(), key=lambda a: a['display_name'])


# Only cities and seed IDs with an established location are inferred.
SEED_CITY_COUNTRY = {'서울': 'KR', '울산': 'KR', '부산': 'KR', '인천': 'KR', '대구': 'KR',
                     '대전': 'KR', '광주': 'KR', '제주': 'KR', '뉴욕': 'US'}


def seed_country(row):
    code = row.get('country_code')
    if isinstance(code, str) and re.fullmatch('[A-Z]{2}', code):
        return code
    return SEED_CITY_COUNTRY.get(row.get('city'))


def sync_seed_geography(conn, seed):
    """Attach existing seed IDs to targets and backfill evidence-backed geography."""
    from kg_collect.targets import tid

    if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='target_entity'").fetchone():
        return
    for row in (*seed.get('kitchens', []), *seed.get('restaurants', [])):
        marker = row.get('publication') or {}
        target = marker.get('restaurant_target_id') or tid('restaurant', row['name'])
        branches = conn.execute("""SELECT b.branch_id,b.restaurant_id,b.city FROM branch b
            JOIN alias a ON a.entity_type='restaurant' AND a.entity_id=b.restaurant_id
            WHERE a.kind='legacy_id' AND a.alias=? AND b.label='main'""", (row['id'],)).fetchall()
        if len(branches) > 1:
            continue
        if branches:
            branch, restaurant, city = branches[0]
        elif marker:
            from .seed_migration import add_alias
            restaurant = stable_id('rst', 'seed:restaurant:' + row['id'])
            branch = stable_id('brn', restaurant, 'main')
            city = row.get('city')
            stamp = marker['published_at']
            conn.execute('INSERT OR IGNORE INTO restaurant(restaurant_id,name,name_en,created_at) VALUES (?,?,?,?)',
                         (restaurant, row['name'], row.get('name_en'), stamp))
            conn.execute('INSERT OR IGNORE INTO branch(branch_id,restaurant_id,area,city,created_at) VALUES (?,?,?,?,?)',
                         (branch, restaurant, row.get('area'), city, stamp))
            add_alias(conn, 'restaurant', restaurant, row['id'], 'legacy_id', stamp)
        else:
            continue
        code = seed_country(row) or SEED_CITY_COUNTRY.get(city)
        conn.execute('UPDATE branch SET country_code=COALESCE(country_code,?),city=COALESCE(city,?) WHERE branch_id=?',
                     (code, row.get('city'), branch))
        conn.execute("""INSERT OR IGNORE INTO target_entity(target_id,restaurant_id,branch_id)
            SELECT target_id,?,? FROM target WHERE target_id=? AND kind='restaurant'""", (restaurant, branch, target))

    for target in conn.execute("SELECT target_id,name_ko FROM target WHERE kind='chef'"):
        ids = conn.execute('SELECT chef_id FROM chef WHERE display_name=?', (target['name_ko'],)).fetchall()
        if len(ids) == 1:
            conn.execute('INSERT OR IGNORE INTO target_entity(target_id,chef_id) VALUES (?,?)', (target['target_id'], ids[0][0]))

    for row in conn.execute("""SELECT e.*,b.city,b.country_code,c.status FROM employment e
        JOIN branch b USING(branch_id) JOIN claim c USING(claim_id)
        WHERE e.valid_to_kind='open' AND c.status IN ('verified','source_claim')
          AND (b.city IS NOT NULL OR b.country_code IS NOT NULL)
          AND EXISTS (SELECT 1 FROM claim_evidence ce WHERE ce.claim_id=e.claim_id AND ce.stance='supports')
          AND NOT EXISTS (SELECT 1 FROM chef_location cl WHERE cl.employment_id=e.employment_id)""").fetchall():
        claim = create_claim(conn, 'attribute', 'source_claim', 'migration:seed-location',
                             claim_id=stable_id('clm', 'chef_location', row['employment_id']))
        for evidence in conn.execute("SELECT evidence_id,stance FROM claim_evidence WHERE claim_id=?", (row['claim_id'],)):
            link_evidence(conn, claim, evidence['evidence_id'], stance=evidence['stance'])
        conn.execute("""INSERT OR IGNORE INTO chef_location(claim_id,chef_id,country_code,city,valid_from,valid_to,basis,employment_id)
            VALUES (?,?,?,?,?,?,'current_employment',?)""",
                     (claim, row['chef_id'], row['country_code'], row['city'], row['valid_from'], row['valid_to'], row['employment_id']))


def link_resolved_chef(conn, target_id, now):
    target = conn.execute("SELECT name_ko,name_en FROM target WHERE target_id=? AND kind='chef'", (target_id,)).fetchone()
    if not target:
        return target_id
    mapped = conn.execute('SELECT chef_id FROM target_entity WHERE target_id=?', (target_id,)).fetchone()
    if mapped:
        return mapped['chef_id']
    matches = conn.execute('SELECT chef_id FROM chef WHERE display_name=?', (target['name_ko'],)).fetchall()
    if len(matches) > 1:
        return target_id
    if matches:
        chef = matches[0][0]
    else:
        chef = stable_id('chf', target_id)
        conn.execute('INSERT OR IGNORE INTO chef(chef_id,display_name,display_name_en,created_at) VALUES (?,?,?,?)',
                     (chef, target['name_ko'] or target['name_en'], target['name_en'], now))
    conn.execute('INSERT OR IGNORE INTO target_entity(target_id,chef_id) VALUES (?,?)', (target_id, chef))
    return chef
