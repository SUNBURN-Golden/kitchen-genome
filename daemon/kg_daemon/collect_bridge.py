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
from kg_ai import pending

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


# extraction columns that are not source content: which AI budget pool ran it (workload) and the ISO form
# derived from date_text + published_at (date_iso). They stay in the body but are left out of the content hash.
_NON_CONTENT_COLLECT_KEYS = ('workload', 'date_iso')


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
    # only the CURRENT extractor's reading of each document: older rule versions' rows stay for audit but are not re-applied as new facts
    rows = c.execute("""SELECT e.*, f.url, f.fetched_at FROM extraction e JOIN fetch_record f ON f.fetch_id=e.fetch_id
                        WHERE f.extractor_version IS NULL OR e.extractor_version=f.extractor_version OR e.pending_only=1 OR e.extractor LIKE '%deepseek%' OR e.epistemic='inference' ORDER BY e.rowid""").fetchall()
    for e in rows:
        tier = tier_of(c, e['host'], official)
        sid = tier_to_source.get(tier, 'search_leads')
        body = {'collect': {k: e[k] for k in e.keys() if k not in ('created_at',)}, 'tier': tier,
                'chef': tgt.get(e['resolved_chef_target']), 'restaurant': tgt.get(e['resolved_restaurant_target']), 'org': tgt.get(e['resolved_org_target'])}
        # hash only source content: bookkeeping/derived columns added later (workload, date_iso) must not make an unchanged extraction look edited
        hash_body = dict(body, collect={k: v for k, v in body['collect'].items() if k not in _NON_CONTENT_COLLECT_KEYS})
        out.setdefault(sid, []).append({'url': e['url'], 'doc_key': f'#ext={e["extraction_id"]}', 'published_at': e['published_at'], 'body': body,
                                        'hash_body': hash_body, 'collected_at': (e['fetched_at'] or '')[:19] or None})
    c.close()
    return out


def _has(c, table):
    return c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone() is not None


def _young(published_at, today) -> bool:
    try:
        return (dt.date.fromisoformat(today[:10]) - dt.date.fromisoformat(published_at[:10])).days <= CURRENT_MAX_AGE_DAYS
    except (TypeError, ValueError):
        return False


def effective_date(x):
    """Date stated in the evidence text (year inferred from publication only for '10월 3일'-style dates). Never the fetch date."""
    if x.get('date_iso'):
        return x['date_iso']
    if not x.get('date_text'):
        # a stated tenure start ('2019년부터') is the effective date of a role
        return x.get('tenure_from') if x.get('label') in ('career', 'employment_claim') and x.get('tenure_from') else None
    if re.fullmatch(r'\d{4}(-\d{2}(-\d{2})?)?', x['date_text'].strip()):
        return x['date_text'].strip()
    try:
        from kg_collect.extract import DATE_RE, EN_DATE, REL_DATE, iso_from_dateish, relative_or_english_date
    except ImportError:  # pragma: no cover
        return None
    if REL_DATE.match(x['date_text']) or EN_DATE.search(x['date_text']):     # '지난해 5월' / 'March 2024': anchored on the publication year
        return relative_or_english_date(x['date_text'], (x.get('published_at') or '')[:4] or None)[1]
    m = DATE_RE.search(x['date_text'])
    iso, _note = iso_from_dateish(m, (x.get('published_at') or '')[:4] or None) if m else (None, None)
    return iso


def normalized_effective_date(x):
    """Validated (ISO date, precision), or None; publication is the only inferred year."""
    from .ontology import parse_iso_date
    from kg_collect.extract import DATE_RE, EN_MONTHS, relative_or_english_date

    if x.get('date_iso'):
        return parse_iso_date(x['date_iso'])
    text = (x.get('date_text') or '').strip()
    parsed = parse_iso_date(text)
    if parsed:
        return parsed
    year = (x.get('published_at') or '')[:4]
    # The bridge's English parser requires a year. Supply only the publication
    # year for a standalone month-day, just as for Korean month-day dates.
    english = re.fullmatch(r'([A-Za-z]+)\s+(\d{1,2}),?', text)
    if english and english[1].lower() in EN_MONTHS and re.fullmatch(r'\d{4}', year):
        return parse_iso_date(relative_or_english_date(f'{text.title()} {year}', year)[1])
    parsed = parse_iso_date(effective_date(dict(x, date_text=text)))
    korean = DATE_RE.search(text)
    # iso_from_dateish drops an out-of-range day; do not turn a bad day into
    # an apparently valid month when applying or repairing stored dates.
    if korean and korean[3] and parsed and parsed[1] != 'day':
        return None
    return parsed


def candidates(body, today) -> list[tuple]:
    """-> [(fact_type, payload, locator, quote)] from one collector extraction. Conservative by design."""
    x, ch, rs, org = body['collect'], body.get('chef'), body.get('restaurant'), body.get('org')
    lab, amb, quote = x['label'], x.get('ambiguity'), x.get('quote')
    loc = {'span_start': x['span_start'], 'span_end': x['span_end'], 'extraction_id': x['extraction_id'], 'extractor': x['extractor'], 'extractor_version': x['extractor_version']}
    lead = x['epistemic'] in ('unverified', 'inference') or body['tier'] in ('search_lead', 'social_lead')
    base = {'pending_only': pending(x), 'asserted_by': 'ai_inference' if pending(x) else 'source', 'model_meta': x.get('model_meta_json'), 'lead_only': lead, 'epistemic': x['epistemic'], 'ambiguity': amb}
    rseed = rs and rs.get('seed_id')
    eff = effective_date(x)
    out = []
    if amb in ('negated_statement', 'planned_not_done', 'multiple_chefs_in_sentence', 'no_chef_in_sentence', 'mentor_or_mentee_unclear', 'subject_from_job_context'):
        return [('menu' if pending(x) else 'ambiguous', {**base, 'label': lab}, loc, quote)]
    if lab == 'employment_claim' and ch and rseed:
        p = {**base, 'chef_name': ch['name_ko'], 'origin_kitchen': None, 'target_id': rseed, 'role_text': x.get('role_text')}
        if MOVE_VERB.search(quote or ''):
            out.append(('chef_move', {**p, 'to_target_id': rseed, 'effective_date': eff or (x.get('published_at') or '')[:7] or None}, loc, quote))
        else:
            out.append(('employment', {**p, 'current': _young(x.get('published_at'), today), 'effective_date': eff}, loc, quote))
    elif lab == 'career' and ch and ((org and org.get('seed_id')) or rseed):
        out.append(('employment', {**base, 'chef_name': ch['name_ko'], 'origin_kitchen': None, 'target_id': (org or {}).get('seed_id') or rseed,
                                   'role_text': x.get('role_text'), 'past': True, 'effective_date': eff, 'tenure_text': '-'.join(filter(None, [x.get('tenure_from'), x.get('tenure_to')])) or None}, loc, quote))
    elif lab in ('opening', 'closure', 'relocation', 'rename') and rseed:
        kind = 'pause' if (lab == 'closure' and amb == 'temporary_closure') else lab
        out.append((kind, {**base, 'target_id': rseed, 'effective_date': eff, 'by_chef': ch['name_ko'] if (ch and lab == 'opening') else None}, loc, quote))
    elif lab == 'menu' and rseed:
        out.append(('menu', {**base, 'target_id': rseed, 'title': (quote or '')[:120], 'effective_date': eff}, loc, quote))
    elif lab == 'collab' and rseed:
        # working alongside / guest events are EVENTS. They never become mentorship.
        out.append(('collab', {**base, 'target_id': rseed, 'title': (quote or '')[:120], 'chef_name': ch['name_ko'] if ch else None, 'effective_date': eff}, loc, quote))
    elif lab == 'mentorship_claim' and ch and not amb and x.get('org_text'):
        out.append(('mentorship', {**base, 'chef_name': ch['name_ko'], 'origin_kitchen': None, 'mentor_label': x['org_text'], 'assertion_text': quote}, loc, quote))
    if pending(x) and not out:
        out.append(('menu', {**base, 'label': lab}, loc, quote))
    return out


def persist_career_span(conn, fetch, text, row, now):
    """Bridge a checked publisher span directly into the immutable source ontology."""
    from . import claims
    from .db import stable_id
    from kg_collect.apply import ensure_source

    start, end = row['span_start'], row['span_end']
    if not (0 <= start < end <= len(text) and text[start:end] == row['quote'] and end - start <= 200):
        raise ValueError('career evidence does not match the stored publisher span')
    evidence_id = stable_id('evd', 'career', row['canonical_url'], row['content_hash'], str(start), str(end))
    sid = ensure_source(conn, fetch['host'], row['source_tier'], now)
    return claims.add_evidence(
        conn, evidence_id=evidence_id, source_id=sid, source_url=row['canonical_url'], quote=row['quote'],
        collected_at=fetch['fetched_at'], published_at=row['published_at'],
        locator={'fetch_id': fetch['fetch_id'], 'span_start': start, 'span_end': end,
                 'content_hash': row['content_hash'], 'normalization': 'NFC'})


def persist_career_claim(conn, row, now):
    from . import claims, ontology
    from .db import stable_id

    assertion_id = row['assertion_id']
    opening = row['relation'] == 'opening'
    claim_id = claims.create_claim(
        conn, 'status_event' if opening else 'employment', 'source_claim', 'collect:career-span',
        claim_id=stable_id('clm', assertion_id), now=now)
    claims.link_evidence(conn, claim_id, row['evidence_id'])
    employment_id = None
    if opening:
        date = ontology.parse_iso_date(row['valid_from'])
        conn.execute('''INSERT OR IGNORE INTO restaurant_status_event(
            event_id,branch_id,kind,effective_date,effective_precision,by_chef_id,claim_id,seed_ref,created_at)
            VALUES (?,?,'open',?,?,?,?,?,?)''',
                     (stable_id('evt', assertion_id), row['branch_id'], *(date or (None, None)),
                      row['chef_id'], claim_id, assertion_id, now))
    else:
        open_ended = row['relation'] == 'current'
        employment_id = ontology.add_employment(
            conn, chef_id=row['chef_id'], branch_id=row['branch_id'], claim_id=claim_id,
            role_id=row['role_id'], role_text=row['role_text'],
            tenure_text=' – '.join(filter(None, (row['from_text'], row['to_text']))) or None,
            valid_from=row['valid_from'], valid_to=row['valid_to'],
            valid_to_kind='known' if row['valid_to'] else 'open' if open_ended else 'unknown',
            last_confirmed_at=(row['published_at'] or row['fetched_at']) if open_ended else None,
            seed_ref=assertion_id, now=now, employment_id=stable_id('emp', assertion_id))
    return claim_id, employment_id
