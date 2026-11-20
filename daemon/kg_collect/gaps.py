"""Gap finder: empty required fields -> jobs. Gaps are CLASSIFIED (not_extracted / absent_in_source / inaccessible / conflict) and are only ever
filled by an extraction that carries evidence (quote + URL + span) and epistemic claim|fact. AI/model/inference output never fills a value."""
import json
import re

from kg_daemon.db import stable_id, utcnow

from .targets import norm

EMPTY = re.compile(r'^\s*$|미확인|미기재|^none$|^null$', re.I)
REQUIRED = {
    'restaurant': ['chef', 'opened', 'status_recent'],
    'chef': ['origin_kitchen', 'current_employer'],
}
# Which extraction labels can fill which field, and what value they carry
FILLERS = {
    'opened': ('opening', lambda e: e.get('date_iso') or e['date_text']),
    'chef': (('opening', 'employment_claim'), lambda e: e['chef_text']),
    'status_recent': (('opening', 'closure', 'relocation', 'rename', 'employment_claim'), lambda e: e['label']),
    'origin_kitchen': ('career', lambda e: e['org_text']),
    'current_employer': ('employment_claim', lambda e: e['restaurant_text']),
}
STATUS_FRESH_DAYS = 90
VOLATILITY = {'status_recent': 'volatile', 'current_employer': 'volatile', 'chef': 'volatile', 'opened': 'stable', 'origin_kitchen': 'stable'}
BLOCKED_OUTCOMES = ('fail_blocked', 'fail_login_page', 'fail_robots', 'fail_banned')


def _is_empty(v) -> bool:
    return v is None or bool(EMPTY.search(str(v)))


def seed_value(row, field):
    a = json.loads(row['attrs_json'])
    if row['kind'] == 'restaurant':
        if a.get('role') == 'origin_kitchen':
            return {'chef': 'n/a', 'opened': 'n/a', 'status_recent': None}.get(field)
        return {'chef': a.get('chef'), 'opened': a.get('opened'), 'status_recent': None}.get(field)
    if row['kind'] == 'chef':
        return {'origin_kitchen': a.get('origin_kitchen'), 'current_employer': (a.get('restaurants') or [None])[0]}.get(field)
    return 'n/a'


def open_gaps(conn, now=None) -> int:
    """Create a gap row for every required field that is empty in the initial data (seed targets only; discovered targets get gaps when promoted)."""
    now = now or utcnow()
    n = 0
    for t in conn.execute("SELECT * FROM target WHERE kind IN ('restaurant','chef') AND status='active'"):
        for f in REQUIRED[t['kind']]:
            v = seed_value(t, f)
            if v == 'n/a' or not _is_empty(v):
                continue
            gid = stable_id('gap', t['target_id'], f)
            n += conn.execute('INSERT OR IGNORE INTO gap(gap_id,target_id,field,opened_at) VALUES (?,?,?,?)', (gid, t['target_id'], f, now)).rowcount
    conn.commit()
    return n


def _evidence_for(conn, gap):
    label, getter = FILLERS[gap['field']]
    labels = (label,) if isinstance(label, str) else label
    col = {'restaurant': 'resolved_restaurant_target', 'chef': 'resolved_chef_target'}
    t = conn.execute('SELECT kind FROM target WHERE target_id=?', (gap['target_id'],)).fetchone()['kind']
    rows = conn.execute(f"""SELECT * FROM extraction WHERE {col[t]}=? AND label IN ({','.join('?' * len(labels))}) AND epistemic IN ('claim','fact') AND asserted_by<>'ai_inference' AND pending_only=0 AND extractor NOT LIKE '%deepseek%' AND extractor NOT LIKE '%model:%' AND extractor NOT LIKE '%ai:%'
                            AND (ambiguity IS NULL OR ambiguity IN ('planned_not_done')) ORDER BY epistemic='fact' DESC, published_at DESC""", (gap['target_id'], *labels)).fetchall()
    out = []
    for r in rows:
        if gap['field'] == 'chef' and r['label'] == 'employment_claim' and not r['chef_text']:
            continue
        if gap['field'] == 'opened' and not re.search(r'20\d{2}', r['date_iso'] or r['date_text'] or ''):
            continue                         # an opening without a year is not an opening DATE
        v = getter(dict(r))
        if not _is_empty(v):
            out.append((dict(r), str(v)))
    pf = profile_evidence(conn, gap)
    if any(r['epistemic'] == 'fact' for r, _ in pf):
        return [(r, v) for r, v in pf if r['epistemic'] == 'fact']   # the restaurant's own page outranks press for its own profile
    return pf + out


PROFILE_FIELDS = {'opened': ('opened',), 'chef': ('chef',), 'status_recent': ('status', 'relocation')}


def profile_evidence(conn, gap) -> list:
    """Profile facts read from the restaurant's own site / JSON-LD / an allowed booking page (official.py, booking.py). claim|fact only."""
    fields = PROFILE_FIELDS.get(gap['field'])
    if not fields:
        return []
    try:
        rows = conn.execute(f"""SELECT * FROM profile_fact WHERE target_id=? AND field IN ({','.join('?' * len(fields))}) AND epistemic IN ('claim','fact')
                                ORDER BY epistemic='fact' DESC, created_at DESC""", (gap['target_id'], *fields)).fetchall()
    except Exception:                                  # DB without migration 0004
        return []
    out = []
    for r in rows:
        d = dict(r)
        d['pf'] = True
        v = 'relocated' if d['field'] == 'relocation' else d['value']
        out.append((d, v))
    return out


def _mark_resolved(conn, g, r, v, now):
    if r.get('pf'):
        conn.execute("UPDATE gap SET state='resolved', klass='resolved', resolved_at=?, resolved_by_fact=?, resolved_value=? WHERE gap_id=?", (now, r['fact_id'], v, g['gap_id']))
    else:
        conn.execute("UPDATE gap SET state='resolved', klass='resolved', resolved_at=?, resolved_by_extraction=?, resolved_value=? WHERE gap_id=?", (now, r['extraction_id'], v, g['gap_id']))


def resolve_gaps(conn, now=None) -> dict:
    """Fill gaps that now have admissible evidence; flag conflicts (never choose between them)."""
    now = now or utcnow()
    res = {'resolved': 0, 'conflict': 0}
    for g in conn.execute("SELECT * FROM gap WHERE state='open'").fetchall():
        ev = _evidence_for(conn, g)
        if not ev:
            continue
        values = {norm(v) for _, v in ev}
        if g['field'] == 'status_recent':
            fresh = [(r, v) for r, v in ev]
            r, v = fresh[0]
            _mark_resolved(conn, g, r, v, now)
            res['resolved'] += 1
            continue
        if len(values) > 1 and g['field'] in ('opened', 'chef', 'current_employer', 'origin_kitchen'):
            # different sources disagree. Dates that nest (2026 vs 2026-05) are not a conflict.
            vs = sorted(values, key=len)
            nested = all(vs[-1].startswith(x) for x in vs[:-1]) if g['field'] == 'opened' else False
            if not nested:
                conn.execute("UPDATE gap SET klass='conflict', klass_reason=? WHERE gap_id=?", (json.dumps(sorted({v for _, v in ev}), ensure_ascii=False), g['gap_id']))
                res['conflict'] += 1
                continue
        r, v = ev[0]
        _mark_resolved(conn, g, r, v, now)
        res['resolved'] += 1
    conn.commit()
    return res


def classify_gaps(conn, now=None) -> dict:
    """not_extracted: we hold documents about the target but no admissible extraction (or none yet).
    absent_in_source: >=2 usable documents about the target were extracted and none states the field.
    inaccessible: the only known sources for the target are blocked (robots/login/429) or banned.
    conflict: kept as set by resolve_gaps (needs a human)."""
    counts = {'not_extracted': 0, 'absent_in_source': 0, 'inaccessible': 0, 'conflict': 0}
    for g in conn.execute("SELECT g.*, t.attrs_json, t.name_ko FROM gap g JOIN target t USING(target_id) WHERE g.state='open'").fetchall():
        if g['klass'] == 'conflict':
            counts['conflict'] += 1
            continue
        docs = conn.execute("""SELECT COUNT(DISTINCT f.fetch_id) n FROM doc_target d JOIN fetch_record f USING(fetch_id) WHERE d.target_id=? AND f.outcome='ok'""", (g['target_id'],)).fetchone()['n']
        dom_blocked = conn.execute(f"""SELECT COUNT(*) n FROM fetch_record f WHERE f.outcome IN ({','.join('?' * len(BLOCKED_OUTCOMES))}) AND
                                       f.canonical_url IN (SELECT canonical_url FROM fetch_record WHERE job_id IN (SELECT job_id FROM crawl_job WHERE target_id=?))""", (*BLOCKED_OUTCOMES, g['target_id'])).fetchone()['n']
        if docs == 0 and dom_blocked > 0:
            klass, why = 'inaccessible', f'{dom_blocked} source fetches blocked (robots/login/429); no readable document'
        elif docs == 0:
            klass, why = 'not_extracted', 'no document about this target collected yet'
        elif docs >= 2 and g['attempts'] >= 1:
            klass, why = 'absent_in_source', f'{docs} readable documents mention the target; none states {g["field"]}'
        else:
            klass, why = 'not_extracted', f'{docs} document(s) mention the target but no admissible {g["field"]} extraction yet'
        conn.execute('UPDATE gap SET klass=?, klass_reason=? WHERE gap_id=?', (klass, why, g['gap_id']))
        counts[klass] += 1
    conn.commit()
    return counts


FIELD_QUERIES = {
    'opened': ['{name} 오픈', '{name} 개업 연도'],
    'chef': ['{name} 셰프', '{name} 총괄 셰프 헤드셰프'],
    'status_recent': ['{name} 폐업', '{name} 이전 리뉴얼'],
    'origin_kitchen': ['{name} 셰프 출신 경력', '{name} 수셰프 근무'],
    'current_employer': ['{name} 셰프 합류', '{name} 헤드셰프 부임'],
}


def gap_jobs(conn, now=None) -> list[dict]:
    """Jobs for open gaps. inaccessible -> NO network job (review item instead). conflict -> review item. Others -> targeted reverse-search queries."""
    now = now or utcnow()
    jobs = []
    for g in conn.execute("""SELECT g.*, t.name_ko, t.name_en, t.kind FROM gap g JOIN target t USING(target_id) WHERE g.state='open' ORDER BY g.attempts, g.opened_at"""):
        if g['klass'] in ('inaccessible', 'conflict'):
            rid = stable_id('rev', 'gap', g['gap_id'], g['klass'])
            conn.execute("INSERT OR IGNORE INTO review_queue(review_id,reason,subject_type,subject_id,payload_json,dedupe_key,created_at) VALUES (?,?,?,?,?,?,?)",
                         (rid, f"gap_{g['klass']}", 'target', g['target_id'], json.dumps({'field': g['field'], 'why': g['klass_reason']}, ensure_ascii=False), f"gap:{g['gap_id']}:{g['klass']}", now))
            continue
        name = g['name_ko'] or g['name_en']
        for q in FIELD_QUERIES[g['field']][: 1 if g['klass'] == 'absent_in_source' else 2]:
            jobs.append({'path': 'reverse_gap', 'pool': 'backfill', 'query': q.format(name=name), 'target_id': g['target_id'], 'gap_id': g['gap_id'], 'field': g['field'],
                         'absent': g['klass'] == 'absent_in_source', 'attempts': g['attempts']})
    conn.commit()
    return jobs


def bump_attempts(conn, gap_id, now=None):
    conn.execute('UPDATE gap SET attempts=attempts+1, last_job_at=? WHERE gap_id=?', (now or utcnow(), gap_id))


def coverage(conn) -> dict:
    """Required-field coverage over seed targets. 'initial' = filled in the seed, 'now' = seed + bot-resolved gaps (with evidence)."""
    total = filled_seed = 0
    for t in conn.execute("SELECT * FROM target WHERE kind IN ('restaurant','chef') AND origin='seed' AND status='active'"):
        for f in REQUIRED[t['kind']]:
            v = seed_value(t, f)
            if v == 'n/a':
                continue
            total += 1
            filled_seed += int(not _is_empty(v))
    resolved = conn.execute("SELECT COUNT(*) n FROM gap WHERE state='resolved'").fetchone()['n']
    all_gaps = conn.execute('SELECT COUNT(*) n FROM gap').fetchone()['n']
    return {'required_fields': total, 'filled_initial': filled_seed, 'coverage_initial': round(filled_seed / total, 4) if total else None,
            'gaps_total': all_gaps, 'gaps_resolved_with_evidence': resolved, 'coverage_now': round((filled_seed + resolved) / total, 4) if total else None,
            'gap_resolution_rate': round(resolved / all_gaps, 4) if all_gaps else None}
