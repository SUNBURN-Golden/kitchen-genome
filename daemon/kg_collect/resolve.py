"""Entity resolution: EXACT normalised alias only. Homonyms are never merged: they are resolved by restaurant context or left unresolved.
New people/places become `candidate` targets (discovered), promoted only with >=2 independent hosts or an official source."""
import json
import re

from kg_ai import pending
from kg_daemon.db import utcnow

from .extract import clean_name, clean_venue
from .review import reject_reason
from .targets import Gazetteer, norm, upsert_target

NEW_NAME_OK = re.compile(r'^[가-힣]{2,4}$')


def _restaurants_of(chef_row) -> set:
    try:
        attrs = json.loads(chef_row['attrs_json'])
        return set(attrs.get('restaurants', [])) | {attrs.get('origin_kitchen')}
    except (TypeError, ValueError):
        return set()


def _seed_ids(conn, tids) -> set:
    out = set()
    for t in tids:
        r = conn.execute('SELECT attrs_json FROM target WHERE target_id=?', (t,)).fetchone()
        if r:
            sid = json.loads(r['attrs_json']).get('seed_id')
            if sid:
                out.add(sid)
    return out


def resolve_chef(conn, gaz: Gazetteer, name: str | None, context_restaurant_targets: list[str], allow_new: bool, via: str, doc_url: str):
    """-> (target_id|None, note)"""
    if not name:
        return None, None
    cands = {c['target_id']: c for c in gaz.lookup(name) if c['kind'] == 'chef'}
    if len(cands) == 1:
        return next(iter(cands)), None
    if len(cands) > 1:
        ctx = _seed_ids(conn, context_restaurant_targets)
        narrowed = [t for t, c in cands.items() if _restaurants_of(c) & ctx]
        if len(narrowed) == 1:
            return narrowed[0], 'homonym_resolved_by_restaurant_context'
        return None, 'homonym_unresolved'          # never guess
    if allow_new and NEW_NAME_OK.fullmatch(name) and clean_name(name) == name and not reject_reason('chef', name):
        t = upsert_target(conn, 'chef', name, origin='discovered', via=via, status='candidate', attrs={'first_url': doc_url})
        return t, 'new_candidate'
    return None, 'unknown_name'


def resolve_restaurant(conn, gaz: Gazetteer, name: str | None, allow_new: bool, via: str, doc_url: str, *, context='', min_length=3):
    if not name:
        return None, None
    cands = {c['target_id']: c for c in gaz.lookup(name) if c['kind'] == 'restaurant'}
    if len(cands) == 1:
        return next(iter(cands)), None
    if len(cands) > 1:
        narrowed = []
        for tid, candidate in cands.items():
            attrs = json.loads(candidate['attrs_json'])
            if any(isinstance(value, str) and len(value) >= 3 and norm(value) in norm(context)
                   for value in (attrs.get('branch'), attrs.get('address'))):
                narrowed.append(tid)
        if len(narrowed) == 1:
            return narrowed[0], 'homonym_resolved_by_branch'
        return None, 'restaurant_homonym_unresolved'
    if allow_new and min_length <= len(name) <= 24 and clean_venue(name) and not reject_reason('restaurant', name):
        t = upsert_target(conn, 'restaurant', name, origin='discovered', via=via, status='candidate', attrs={'first_url': doc_url})
        return t, 'new_candidate'
    return None, 'unknown_restaurant'


def resolve_extractions(conn, recs: list[dict], gaz: Gazetteer, doc_url: str, via: str) -> None:
    """Mutates recs: resolved_chef_target / resolved_restaurant_target / resolved_org_target / resolve_note."""
    careers = {}
    for row in recs:
        if row.get('extractor') == 'rule:publication_career' and not row.get('ambiguity') and not pending(row):
            key = (row.get('span_start'), norm(row.get('chef_text')))
            careers.setdefault(key, []).append(row['org_text'])
    for r in recs:
        if pending(r):
            continue
        notes = []
        allow_new = r['label'] in ('career', 'employment_claim', 'opening') and r['epistemic'] in ('claim', 'fact') and not r.get('ambiguity') or \
            (r['label'] in ('career', 'employment_claim') and r.get('ambiguity') == 'multiple_chefs_in_sentence')
        strict = r.get('extractor', '').startswith('rule:publication_')
        rest_t, n1 = resolve_restaurant(conn, gaz, r.get('restaurant_text'), allow_new and strict, via, doc_url,
                                        context=r.get('quote', ''), min_length=2)
        org_t, n2 = resolve_restaurant(conn, gaz, r.get('org_text'), allow_new and r['label'] == 'career', via, doc_url,
                                       context=r.get('quote', ''), min_length=2 if strict else 3)
        ctx = [t for t in (rest_t, org_t) if t]
        if strict and rest_t:
            ctx.extend(t for name in careers.get((r.get('span_start'), norm(r.get('chef_text'))), [])
                       if (t := resolve_restaurant(conn, gaz, name, False, via, doc_url, context=r.get('quote', ''))[0]))
        # the chef's NAME is solid ('X 셰프') even when the org name is a bare unverified token: the chef may become a candidate, the org may not
        chef_t, n3 = resolve_chef(conn, gaz, r.get('chef_text'), ctx, allow_new or r.get('ambiguity') == 'org_name_unverified', via, doc_url)
        r['resolved_restaurant_target'], r['resolved_org_target'], r['resolved_chef_target'] = rest_t, org_t, chef_t
        r['resolve_note'] = ';'.join(x for x in (n1, n2, n3) if x) or None


def promote_candidates(conn, qualified) -> int:
    """Activate candidates only after the stored, independent source evidence is checked."""
    n = 0
    for t in conn.execute("SELECT target_id,kind,name_ko,name_en FROM target WHERE status='candidate'").fetchall():
        tid = t['target_id']
        if tid in qualified and not reject_reason(t['kind'], t['name_ko'] or t['name_en']):
            conn.execute("UPDATE target SET status='active' WHERE target_id=?", (tid,))
            n += 1
    conn.commit()
    return n
