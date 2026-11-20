"""Migrate data/seed.json (legacy flat careers) into ontology rows: Chef, Restaurant, Branch, Employment,
RestaurantStatusEvent, MentorshipAssertion (only where the seed text says '사사'), Alias, Claim, Evidence.

Rules (docs/data/ONTOLOGY.md §Migration):
  * Idempotent: deterministic ids + seed_ref keys; re-running adds nothing.
  * Never reads/writes seed.json except to read. Never touches web/ or data/build_data.py.
  * Legacy lineage_status 'verified' maps to claim status 'source_claim' (seed has no locator/quote, so the new
    'verified' bar is not met until the verification stage re-checks). unverified/related/n-a -> 'unverified' unless a
    source URL exists (then 'source_claim' for n/a).
  * Origin-kitchen employment: dates unknown, valid_to_kind='unknown' (never shown as current).
  * Chef at own restaurant: valid_to_kind='open' unless the restaurant has closed_at.
  * Unparseable opened/period text is kept verbatim (detail / tenure_text), never turned into a date.
  * Anything ambiguous (unnamed chef, '+' side notes, unresolved employer) goes to review_queue, not into facts."""
import json
import os
import re
import sys

from . import registry as reg_mod
from .claims import add_evidence, create_claim, link_evidence, queue_review
from .db import ROOT, stable_id, utcnow
from .ontology import add_employment, parse_iso_date, sync_seed_geography

sys.path.insert(0, os.path.join(ROOT, 'crawler'))
try:  # reuse the crawler's host classification
    from extractor import classify_source_tier
except Exception:  # pragma: no cover - fixtures without crawler on path
    def classify_source_tier(url):
        return 'search_lead'

CREATED_BY = 'migration:seed'
ROLE_MAP = [  # (regex on role text, role_id) — only unambiguous wording
    (re.compile(r'총괄\s*페이스트리|Executive Pastry'), 'pastry_chef'),
    (re.compile(r'파티시에'), 'pastry_chef'),
    (re.compile(r'수\s*셰프'), 'sous_chef'),
    (re.compile(r'헤드\s*셰프|수석\s*셰프'), 'head_chef'),
    (re.compile(r'창립\s*멤버|개업\s*멤버'), 'founding_member'),
    (re.compile(r'오너\s*셰프'), 'owner_chef'),
    (re.compile(r'실습'), 'trainee'),
    (re.compile(r'출신'), 'alumnus_unspecified'),
]
RE_MENTOR = re.compile(r'([가-힣]{2,4})\s*셰프\s*사사')
RE_HANJA = re.compile(r'[\u4e00-\u9fff]')
RE_UNNAMED = re.compile(r'이름\s*미기재')
SOURCE_ID_BY_TIER = reg_mod.TIER_TO_SOURCE


def role_id_for(text):
    if not text:
        return None
    for rx, rid in ROLE_MAP:
        if rx.search(text):
            return rid
    return None


def split_chefs(raw: str):
    """-> (names, aliases[(name, kind)], notes[str]). Splits '·', strips parentheticals; '+' notes are not parsed."""
    notes, aliases = [], []
    if not raw or RE_UNNAMED.search(raw):
        return [], [], []
    paren = re.findall(r'\(([^)]*)\)', raw)
    base = re.sub(r'\s*\([^)]*\)', '', raw).strip()
    for p in paren:
        p = p.strip()
        if p.startswith('+'):
            notes.append(p)
        elif RE_HANJA.search(p):
            aliases.append((p, 'hanja'))
    names = [n.strip() for n in base.split('·') if n.strip()]
    return names, aliases, notes


def parse_opened(text):
    """Only a clean leading date counts; multi-value ('/') or free text stays verbatim."""
    if not text or text == '미확인' or '/' in text:
        return None, text if text and text != '미확인' else None
    m = re.match(r'^(\d{4}(?:-\d{2}(?:-\d{2})?)?)(?:\s|$)', text.strip())
    if m and parse_iso_date(m.group(1)):
        rest = text.strip()[len(m.group(1)):].strip()
        return m.group(1), rest or None
    return None, text


def claim_status_for(row) -> str:
    ls = row.get('lineage_status')
    if ls == 'verified':
        return 'source_claim'
    if ls == 'conflict':
        return 'source_claim'
    if ls in ('unverified', 'related'):
        return 'unverified'
    return 'source_claim' if (row.get('lineage_src') or '').startswith('http') else 'unverified'   # n/a (established)


def _evidence(conn, url, row, field):
    if not (url or '').startswith('http'):
        return None
    tier = classify_source_tier(url)
    return add_evidence(conn, source_id=SOURCE_ID_BY_TIER.get(tier, 'search_leads'), source_url=url,
                        collected_at=row.get('checked') or '1970-01-01',
                        locator={'legacy_seed_field': field, 'seed_id': row['id']})


def migrate_seed(conn, seed: dict, now: str | None = None) -> dict:
    now = now or utcnow()
    reg_mod.sync_registry(conn, reg_mod.load_registry(), now)
    stats = {k: 0 for k in ('restaurants', 'chefs', 'employments', 'status_events', 'mentorships', 'aliases', 'review_items', 'skipped_unnamed_chef')}
    before = {t: conn.execute(f'SELECT COUNT(*) c FROM {t}').fetchone()['c'] for t in ('restaurant', 'chef', 'employment', 'restaurant_status_event', 'mentorship_assertion', 'alias', 'review_queue')}

    # ── restaurants / branches (kitchens first; established restaurants with an identical name are the same entity)
    rest_of_seed: dict[str, tuple[str, str]] = {}   # seed id -> (restaurant_id, branch_id)
    by_name: dict[str, tuple[str, str]] = {}

    def ensure_restaurant(seed_id, name, name_en, area=None, city=None, kind='kitchen'):
        key = f'seed:{kind}:{seed_id}'
        rid = stable_id('rst', key)
        bid = stable_id('brn', rid, 'main')
        conn.execute('INSERT OR IGNORE INTO restaurant(restaurant_id,name,name_en,created_at) VALUES (?,?,?,?)', (rid, name, name_en, now))
        conn.execute('INSERT OR IGNORE INTO branch(branch_id,restaurant_id,label,area,city,created_at) VALUES (?,?,?,?,?,?)', (bid, rid, 'main', area, city, now))
        add_alias(conn, 'restaurant', rid, seed_id, 'legacy_id', now)
        return rid, bid

    for k in seed.get('kitchens', []):
        ids = ensure_restaurant(k['id'], k['name'], k.get('name_en') or k.get('en'))
        rest_of_seed[k['id']] = ids
        by_name[k['name']] = ids
    for r in seed.get('restaurants', []):
        if r.get('type') == 'established' and r['name'] in by_name:
            rid, bid = by_name[r['name']]
            rest_of_seed[r['id']] = (rid, bid)
            add_alias(conn, 'restaurant', rid, r['id'], 'legacy_id', now)
            queue_review(conn, 'entity_merge_by_name', 'restaurant', rid, {'seed_ids': [r['id']], 'rule': 'identical Korean name'},
                         dedupe_key=f'merge_by_name:{rid}:{r["id"]}', now=now)
            continue
        ids = ensure_restaurant(r['id'], r['name'], r.get('name_en'), r.get('area') if r.get('area') != '미확인' else None, r.get('city'), kind='restaurant')
        rest_of_seed[r['id']] = ids
        by_name.setdefault(r['name'], ids)
        for prev in r.get('prev_names') or []:
            add_alias(conn, 'restaurant', ids[0], prev, 'former_name', now, claim=_alias_claim(conn, r, now))

    # ── per restaurant row: chef, employments, status events, mentorship
    for r in seed.get('restaurants', []):
        rid, bid = rest_of_seed[r['id']]
        status = claim_status_for(r)
        closed_at = r.get('closed_at')
        names, h_aliases, notes = split_chefs(r.get('chef') or '')
        for n in notes:
            queue_review(conn, 'unparsed_chef_note', 'restaurant', rid, {'seed_id': r['id'], 'note': n}, dedupe_key=f'chef_note:{r["id"]}:{n}', now=now)
        if not names:
            stats['skipped_unnamed_chef'] += 1
            queue_review(conn, 'unnamed_chef', 'restaurant', rid, {'seed_id': r['id'], 'raw': r.get('chef')}, dedupe_key=f'unnamed_chef:{r["id"]}', now=now)
        origin = r.get('kitchen')
        for name in names:
            chef_id = stable_id('chf', name, origin or '')
            marker = r.get('publication')
            if marker and conn.execute("SELECT 1 FROM sqlite_master WHERE name='target_entity'").fetchone():
                mapped = conn.execute('SELECT chef_id FROM target_entity WHERE target_id=?', (marker['chef_target_id'],)).fetchone()
                if mapped and mapped[0]:
                    chef_id = mapped[0]
            conn.execute('INSERT OR IGNORE INTO chef(chef_id,display_name,display_name_en,disambiguator,created_at) VALUES (?,?,?,?,?)',
                         (chef_id, name, r.get('chef_en') if len(names) == 1 else None, origin, now))
            add_alias(conn, 'chef', chef_id, f'chef:{name}@{origin or ""}', 'legacy_id', now)
            for a, kind in h_aliases:
                add_alias(conn, 'chef', chef_id, a, kind, now)
            ev_lineage = _evidence(conn, r.get('lineage_src'), r, 'lineage_src')

            # (A) origin-kitchen employment (alumni rows)
            if origin and origin in rest_of_seed:
                ref = f'seed:{r["id"]}:{name}:origin'
                cl = create_claim(conn, 'employment', status, CREATED_BY, note=f'legacy lineage_status={r.get("lineage_status")}', claim_id=stable_id('clm', ref), now=now)
                if ev_lineage:
                    link_evidence(conn, cl, ev_lineage)
                add_employment(conn, chef_id=chef_id, claim_id=cl, branch_id=rest_of_seed[origin][1], role_id=role_id_for(r.get('role')),
                               role_text=r.get('role'), tenure_text=r.get('period'), valid_to_kind='unknown', last_confirmed_at=r.get('checked'),
                               seed_ref=ref, now=now)
                m = RE_MENTOR.search(r.get('role') or '')
                if m and not _mentor_exists(conn, f'seed:{r["id"]}:{name}:mentor'):
                    mc = create_claim(conn, 'mentorship', 'source_claim' if ev_lineage else 'unverified', CREATED_BY,
                                      note='source wording contains 사사', claim_id=stable_id('clm', f'seed:{r["id"]}:{name}:mentor'), now=now)
                    if ev_lineage:
                        link_evidence(conn, mc, ev_lineage)
                    conn.execute('INSERT OR IGNORE INTO mentorship_assertion(mentorship_id,mentee_chef_id,mentor_label,claim_id,seed_ref,created_at) VALUES (?,?,?,?,?,?)',
                                 (stable_id('mnt', f'seed:{r["id"]}:{name}'), chef_id, f'{m.group(1)} 셰프', mc, f'seed:{r["id"]}:{name}:mentor', now))

            # (B) chef at own restaurant (open unless the restaurant closed)
            ref = f'seed:{r["id"]}:{name}:own'
            cl = create_claim(conn, 'employment', status, CREATED_BY, note='chef listed on restaurant row', claim_id=stable_id('clm', ref), now=now)
            if ev_lineage:
                link_evidence(conn, cl, ev_lineage)
            add_employment(conn, chef_id=chef_id, claim_id=cl, branch_id=bid, valid_to_kind='open', last_confirmed_at=r.get('checked'), seed_ref=ref, now=now)

            # (C) chef_career entries (structured, dated)
            for i, c in enumerate(r.get('chef_career') or []):
                if c.get('type') != 'employment':
                    continue
                _migrate_career_entry(conn, seed, rest_of_seed, r, chef_id, name, i, c, now)

            # (D) opening attribution (alumni opened it) — only for alumni rows
            if r.get('type') == 'alumni':
                eff, rest = parse_opened(r.get('opened'))
                ref = f'seed:{r["id"]}:{name}:open'
                _status_event(conn, ref, bid, 'open', eff, rest, status, ev_lineage, now, by_chef=chef_id)

        # (E) closure event (restaurant-level; independent of chef parsing)
        if closed_at and parse_iso_date(closed_at):
            ev = _evidence(conn, r.get('closed_src'), r, 'closed_src')
            _status_event(conn, f'seed:{r["id"]}:close', bid, 'close', closed_at, None, 'source_claim' if ev else 'unverified', ev, now)
        if r.get('lineage_status') == 'conflict':
            queue_review(conn, 'legacy_conflict', 'restaurant', rid, {'seed_id': r['id'], 'note': 'seed lineage_status=conflict; competing claims not preserved in seed'},
                         dedupe_key=f'legacy_conflict:{r["id"]}', now=now)

    for t, key in (('restaurant', 'restaurants'), ('chef', 'chefs'), ('employment', 'employments'), ('restaurant_status_event', 'status_events'),
                   ('mentorship_assertion', 'mentorships'), ('alias', 'aliases'), ('review_queue', 'review_items')):
        stats[key] = conn.execute(f'SELECT COUNT(*) c FROM {t}').fetchone()['c'] - before[t]
    sync_seed_geography(conn, seed)
    conn.commit()
    return stats


def add_alias(conn, etype, eid, alias, kind, now, claim=None):
    conn.execute('INSERT OR IGNORE INTO alias(alias_id,entity_type,entity_id,alias,kind,claim_id,created_at) VALUES (?,?,?,?,?,?,?)',
                 (stable_id('als', etype, eid, alias, kind), etype, eid, alias, kind, claim, now))


def _alias_claim(conn, r, now):
    ref = f'seed:{r["id"]}:rename'
    cl = create_claim(conn, 'alias', 'source_claim' if r.get('rename_src') else 'unverified', CREATED_BY, claim_id=stable_id('clm', ref), now=now)
    for u in r.get('rename_src') or []:
        ev = _evidence(conn, u, r, 'rename_src')
        if ev:
            link_evidence(conn, cl, ev)
    return cl


def _mentor_exists(conn, ref):
    return conn.execute('SELECT 1 FROM mentorship_assertion WHERE seed_ref=?', (ref,)).fetchone() is not None


def _status_event(conn, ref, bid, kind, eff, detail, status, ev, now, by_chef=None):
    if conn.execute('SELECT 1 FROM restaurant_status_event WHERE seed_ref=?', (ref,)).fetchone():
        return
    p = parse_iso_date(eff) if eff else None
    cl = create_claim(conn, 'status_event', status, CREATED_BY, claim_id=stable_id('clm', ref), now=now)
    if ev:
        link_evidence(conn, cl, ev)
    conn.execute("""INSERT INTO restaurant_status_event(event_id,branch_id,kind,effective_date,effective_precision,by_chef_id,detail,claim_id,seed_ref,created_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?)""",
                 (stable_id('evt', ref), bid, kind, p[0] if p else None, p[1] if p else None, by_chef, detail, cl, ref, now))


def _migrate_career_entry(conn, seed, rest_of_seed, r, chef_id, chef_name, i, c, now):
    """chef_career items: dedupe identical entries repeated on several rows; resolve the employer only when exactly one
    known restaurant name appears in the text; otherwise keep it unresolved (employer_label) + review."""
    text = c.get('text') or ''
    sig = stable_id('car', chef_id, c.get('date') or '', c.get('src') or '', text)
    ref = f'seed:career:{sig}'
    if conn.execute('SELECT 1 FROM employment WHERE seed_ref=?', (ref,)).fetchone():
        return
    cands = sorted({rest_of_seed[x['id']][1] for x in seed.get('restaurants', []) if x['name'] in text and x['id'] in rest_of_seed})
    cands += [rest_of_seed[k['id']][1] for k in seed.get('kitchens', []) if k['name'] in text and rest_of_seed[k['id']][1] not in cands]
    cands = sorted(set(cands))
    date = c.get('date')
    p = parse_iso_date(date) if date else None
    cl = create_claim(conn, 'employment', 'source_claim' if c.get('src') else 'unverified', CREATED_BY, note='seed chef_career entry', claim_id=stable_id('clm', ref), now=now)
    ev = _evidence(conn, c.get('src'), r, f'chef_career[{i}]')
    if ev:
        link_evidence(conn, cl, ev)
    if len(cands) == 1:
        own = conn.execute("""SELECT employment_id, valid_from, claim_id FROM employment WHERE chef_id=? AND branch_id=? AND seed_ref LIKE '%:own' ORDER BY created_at LIMIT 1""",
                           (chef_id, cands[0])).fetchone()
        if own and p and own['valid_from'] in (None, date):
            # refine the plain 'own restaurant' employment with the dated career entry instead of duplicating it
            if own['valid_from'] is None:
                conn.execute('UPDATE employment SET valid_from=?, valid_from_precision=?, role_text=COALESCE(role_text, ?) WHERE employment_id=?', (p[0], p[1], text, own['employment_id']))
            if ev:
                link_evidence(conn, own['claim_id'], ev)
            return
        eid = add_employment(conn, chef_id=chef_id, claim_id=cl, branch_id=cands[0], role_text=text, valid_from=date if p else None,
                             valid_to_kind='open', last_confirmed_at=r.get('checked'), seed_ref=ref, now=now)
    else:
        eid = add_employment(conn, chef_id=chef_id, claim_id=cl, employer_label=text[:200], role_text=text, valid_from=date if p else None,
                             valid_to_kind='open', last_confirmed_at=r.get('checked'), seed_ref=ref, now=now)
        queue_review(conn, 'unresolved_employer', 'employment', eid, {'candidates': cands, 'text': text[:200]}, dedupe_key=f'unresolved_employer:{ref}', now=now)


def main(argv=None):  # pragma: no cover - thin CLI
    import argparse
    from .db import DEFAULT_DB, connect, migrate
    ap = argparse.ArgumentParser(description='Migrate data/seed.json into the service DB (read-only on seed)')
    ap.add_argument('--seed', default=os.path.join(ROOT, 'data', 'seed.json'))
    ap.add_argument('--db', default=DEFAULT_DB)
    ap.add_argument('--dry-run', action='store_true', help='migrate into an in-memory DB and print stats')
    a = ap.parse_args(argv)
    conn = connect(':memory:' if a.dry_run else a.db)
    migrate(conn)
    stats = migrate_seed(conn, json.load(open(a.seed, encoding='utf-8')))
    print(json.dumps({'db': ':memory:' if a.dry_run else a.db, **stats}, ensure_ascii=False))
