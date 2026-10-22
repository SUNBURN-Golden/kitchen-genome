"""Apply crawler findings to data/seed.json under the change policy, queue the rest.
Policy: closure / rename / allowlisted field updates with >=2 independent reliable sources -> applied.
Everything else (single source, chef moves, openings, site_down, conflicting) -> crawler/pending.json (not on the site).
Never touches outreach verification fields. Never touches SHOW_VERDICTS / newsletter / robots (not in data).
Inputs: newest crawler/out/findings-*.json + crawler/manual_findings.json (hand-checked evidence, same format).
Writes: data/seed.json (if changed), crawler/pending.json, crawler/applied.json, crawler/out/last_summary.json."""
import json, os, glob, datetime as dt
H = os.path.dirname(os.path.abspath(__file__)); ROOT = os.path.dirname(H)
C = json.load(open(os.path.join(H, 'config.json'), encoding='utf-8'))
SEED = os.path.join(ROOT, 'data', 'seed.json')
PROTECTED = {'verification_status', 'contacted_at', 'confirmed_at', 'confirmed_by', 'correction_requested', 'outreach', 'reply_status'}
FIELD_ALLOW = {'note', 'area', 'chef', 'lineage_src', 'lineage_status', 'opened'}
AUTO_KINDS = {'closure', 'rename', 'field'}

def reliable(f):
    h = f.get('host', '')
    if any(u in h for u in C['unreliable_hosts']): return False
    if f.get('manual'): return True          # hand-checked; may include guide.michelin.com viewed by a person
    return not any(b in h for b in C['banned_hosts'])

def load(p, d):
    try: return json.load(open(p, encoding='utf-8'))
    except FileNotFoundError: return d

def main():
    today = dt.date.today().isoformat()
    seed = json.load(open(SEED, encoding='utf-8')); R = {r['id']: r for r in seed['restaurants']}
    files = sorted(glob.glob(os.path.join(H, 'out', 'findings-*.json')))
    igf = sorted(glob.glob(os.path.join(H, 'out', 'ig-*.json')))
    F = (load(files[-1], {}).get('findings', []) if files else []) + \
        (load(igf[-1], {}).get('findings', []) if igf else []) + \
        [dict(f, manual=True) for f in load(os.path.join(H, 'manual_findings.json'), [])]
    groups = {}
    ignored = set(C.get('ignored_urls', ()))
    for f in F:
        if f.get('id') not in R or f.get('url') in ignored: continue
        key = (f['id'], f['kind'], f.get('new_name') or '', f.get('field') or '', json.dumps(f.get('value'), ensure_ascii=False))
        groups.setdefault(key, []).append(f)
    applied, pend = [], []
    for (rid, kind, new, field, val), fs in sorted(groups.items()):
        r = R[rid]; hosts = sorted({f['host'] for f in fs if reliable(f) and not f.get('instagram')})
        ig = any(f.get('instagram') for f in fs)
        n_src = len(hosts) + (1 if ig and hosts else 0)   # IG corroborates one reliable source, never counts alone
        srcs = sorted({(f.get('date') or '', f['url']) for f in fs})
        if kind == 'closure' and r.get('closed_at'): continue
        if kind == 'rename' and r.get('name') == new: continue
        if kind == 'field' and (field not in FIELD_ALLOW or field in PROTECTED or r.get(field) == json.loads(val)):
            if field in FIELD_ALLOW and r.get(field) == json.loads(val): continue
        ok = kind in AUTO_KINDS and n_src >= 2 and not (kind == 'field' and field not in FIELD_ALLOW) \
             and not (kind == 'rename' and not new)
        rec = dict(id=rid, name=r['name'], kind=kind, new_name=new or None, field=field or None,
                   value=json.loads(val) if field else None, reliable_hosts=hosts, sources=[u for _, u in srcs],
                   dates=[d for d, _ in srcs], note='; '.join(sorted({f['note'] for f in fs if f.get('note')})) or None)
        if not ok: pend.append(rec); continue
        rel = sorted([f for f in fs if reliable(f) or f.get('instagram')], key=lambda f: f.get('date') or '')
        if kind == 'closure':
            r['closed_at'] = rel[0].get('closed_at') or rel[0]['date']; r['closed_src'] = rel[0]['url']
        elif kind == 'rename':
            r.setdefault('prev_names', []).append(r['name']); r['name'] = new
            r['rename_src'] = [f['url'] for f in rel]; r.pop('name_en', None)
        else:
            r[field] = json.loads(val)
        r['crawl_checked'] = today; r.setdefault('crawl_sources', [])
        r['crawl_sources'] = sorted(set(r['crawl_sources']) | {f['url'] for f in rel})
        rec['applied_at'] = today; applied.append(rec)
    if applied:
        json.dump(seed, open(SEED, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
    # pending queue: keep first_seen, replace content
    old = {(p['id'], p['kind'], p.get('new_name'), p.get('field')): p for p in load(os.path.join(H, 'pending.json'), [])}
    for p in pend:
        o = old.get((p['id'], p['kind'], p.get('new_name'), p.get('field')))
        p['first_seen'] = (o or {}).get('first_seen', today)  # no last_seen: avoids a daily no-op PR
    json.dump(pend, open(os.path.join(H, 'pending.json'), 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
    log = load(os.path.join(H, 'applied.json'), []) + applied
    json.dump(log, open(os.path.join(H, 'applied.json'), 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
    os.makedirs(os.path.join(H, 'out'), exist_ok=True)
    s = dict(day=today, changed=bool(applied), applied=applied, pending_count=len(pend))
    json.dump(s, open(os.path.join(H, 'out', 'last_summary.json'), 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
    print(json.dumps(dict(changed=s['changed'], applied=[(a['id'], a['kind'], a.get('new_name') or a.get('field')) for a in applied],
                          pending=len(pend)), ensure_ascii=False))

if __name__ == '__main__': main()
