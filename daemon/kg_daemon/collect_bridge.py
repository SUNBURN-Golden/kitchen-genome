"""Bridge: real collector output (kg_collect `extraction` rows, fetched from the live web under robots/403 limits)
-> daemon documents (stage 2) -> candidate facts (stage 4).

The collector already stores verbatim quote + span offsets + fetch time, so every candidate carries
source URL, evidence position (span_start/span_end), collected_at and effective_from (date stated in the text, else NULL).
Nothing here fetches anything; it only reads the collector DB."""
import datetime as dt
import json
import os
import re
import sqlite3

SEARCH_HOSTS = {'bing.com', 'www.bing.com', 'duckduckgo.com', 'html.duckduckgo.com', 'search.naver.com', 'google.com', 'www.google.com'}
# a move/appointment verb next to a current-employment claim. Without one, an employment claim is NOT a move.
MOVE_VERB = re.compile(r'옮겼|옮긴|이직|이적|새\s?둥지|합류(?:했|한|한다)|부임(?:했|한)|영입(?:했|한|됐)|선임(?:했|됐)|취임|joins|joined|has moved|takes the helm|named (?:executive |head )?chef', re.I)
CURRENT_MAX_AGE_DAYS = 365      # a current-employment claim older than this is not allowed to open a current tenure


def _open(path):
    if not path or not os.path.exists(path):
        return None
    c = sqlite3.connect(f'file:{path}?mode=ro', uri=True)
    c.row_factory = sqlite3.Row
    try:
        c.execute('SELECT 1 FROM extraction LIMIT 1')
    except sqlite3.Error:
        c.close()
        return None
    return c


def tier_of(c, host, official_hosts) -> str:
    if host in official_hosts:
        return 'official'
    if host in SEARCH_HOSTS:
        return 'search_lead'
    r = c.execute('SELECT tier_guess FROM domain_profile WHERE host=?', (host,)).fetchone()
    return (r['tier_guess'] if r and r['tier_guess'] else 'press')


def collector_records(path, tier_to_source) -> dict:
    """-> {source_id: [record,...]} one record per extraction row (doc identity: url + extraction id)."""
    c = _open(path)
    out: dict = {}
    if c is None:
        return out
    official = {r['host'] for r in c.execute('SELECT host FROM official_site WHERE verified=1')} if _has(c, 'official_site') else set()
    tgt = {}
    for t in c.execute('SELECT target_id, kind, name_ko, name_en, attrs_json FROM target'):
        a = json.loads(t['attrs_json'] or '{}')
        tgt[t['target_id']] = {'kind': t['kind'], 'name_ko': t['name_ko'], 'name_en': t['name_en'], 'seed_id': a.get('seed_id'), 'origin': a.get('origin_kitchen')}
    rows = c.execute("""SELECT e.*, f.url, f.fetched_at FROM extraction e JOIN fetch_record f ON f.fetch_id=e.fetch_id ORDER BY e.rowid""").fetchall()
    for e in rows:
        tier = tier_of(c, e['host'], official)
        sid = tier_to_source.get(tier, 'search_leads')
        body = {'collect': {k: e[k] for k in e.keys() if k not in ('created_at',)}, 'tier': tier,
                'chef': tgt.get(e['resolved_chef_target']), 'restaurant': tgt.get(e['resolved_restaurant_target']), 'org': tgt.get(e['resolved_org_target'])}
        out.setdefault(sid, []).append({'url': e['url'], 'doc_key': f'#ext={e["extraction_id"]}', 'published_at': e['published_at'], 'body': body,
                                        'collected_at': (e['fetched_at'] or '')[:19] or None})
    c.close()
    return out


def _has(c, table):
    return c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone() is not None


def _young(published_at, today) -> bool:
    try:
        return (dt.date.fromisoformat(today[:10]) - dt.date.fromisoformat(published_at[:10])).days <= CURRENT_MAX_AGE_DAYS
    except (TypeError, ValueError):
        return False


def candidates(body, today) -> list[tuple]:
    """-> [(fact_type, payload, locator, quote)] from one collector extraction. Conservative by design."""
    x, ch, rs, org = body['collect'], body.get('chef'), body.get('restaurant'), body.get('org')
    lab, amb, quote = x['label'], x.get('ambiguity'), x.get('quote')
    loc = {'span_start': x['span_start'], 'span_end': x['span_end'], 'extraction_id': x['extraction_id'], 'extractor': x['extractor'], 'extractor_version': x['extractor_version']}
    lead = x['epistemic'] in ('unverified', 'inference') or body['tier'] in ('search_lead', 'social_lead')
    base = {'lead_only': lead, 'epistemic': x['epistemic'], 'ambiguity': amb}
    rseed = rs and rs.get('seed_id')
    eff = x.get('date_iso') if 'date_iso' in x.keys() else None
    out = []
    if amb in ('negated_statement', 'planned_not_done', 'multiple_chefs_in_sentence', 'no_chef_in_sentence', 'mentor_or_mentee_unclear', 'subject_from_job_context'):
        return [('ambiguous', {**base, 'label': lab}, loc, quote)]
    if lab == 'employment_claim' and ch and rseed:
        p = {**base, 'chef_name': ch['name_ko'], 'origin_kitchen': None, 'target_id': rseed, 'role_text': x.get('role_text')}
        if MOVE_VERB.search(quote or ''):
            out.append(('chef_move', {**p, 'to_target_id': rseed, 'effective_date': eff or (x.get('published_at') or '')[:7] or None}, loc, quote))
        else:
            out.append(('employment', {**p, 'current': _young(x.get('published_at'), today)}, loc, quote))
    elif lab == 'career' and ch and ((org and org.get('seed_id')) or rseed):
        out.append(('employment', {**base, 'chef_name': ch['name_ko'], 'origin_kitchen': None, 'target_id': (org or {}).get('seed_id') or rseed,
                                   'role_text': x.get('role_text'), 'past': True, 'tenure_text': '-'.join(filter(None, [x.get('tenure_from'), x.get('tenure_to')])) or None}, loc, quote))
    elif lab in ('opening', 'closure', 'relocation', 'rename') and rseed:
        kind = 'pause' if (lab == 'closure' and amb == 'temporary_closure') else lab
        out.append((kind, {**base, 'target_id': rseed, 'effective_date': eff, 'by_chef': ch['name_ko'] if (ch and lab == 'opening') else None}, loc, quote))
    elif lab == 'menu' and rseed:
        out.append(('menu', {**base, 'target_id': rseed, 'title': (quote or '')[:120]}, loc, quote))
    elif lab == 'collab' and rseed:
        # working alongside / guest events are EVENTS. They never become mentorship.
        out.append(('collab', {**base, 'target_id': rseed, 'title': (quote or '')[:120], 'chef_name': ch['name_ko'] if ch else None}, loc, quote))
    elif lab == 'mentorship_claim' and ch and not amb and x.get('org_text'):
        out.append(('mentorship', {**base, 'chef_name': ch['name_ko'], 'origin_kitchen': None, 'mentor_label': x['org_text'], 'assertion_text': quote}, loc, quote))
    return out
