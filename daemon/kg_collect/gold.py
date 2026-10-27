"""Accuracy against a HAND-LABELLED gold sample (the rules are never judged by themselves).
gold file (json): {"targets": [{"kind","name_ko","name_en","restaurants":[...]}], "sentences": [{"text","host","tier","published_at","expect":[{"label","chef","restaurant","org","epistemic"}]}],
                   "resolve": [{"name","kind","context":[restaurant names],"expect":"<target name>|homonym_unresolved|new_candidate|unknown"}]}
Metrics: extraction precision/recall/F1 on (label, chef, restaurant|org); epistemic accuracy on matched items; entity-resolution false-merge rate
(= a name resolved to the WRONG existing target / all names that resolved to an existing target)."""
import json

from kg_daemon.db import connect, migrate

from .extract import extract_text
from .resolve import resolve_chef, resolve_restaurant
from .targets import Gazetteer, norm, upsert_target


def _key(label, chef, rest, org):
    return (label, norm(chef or ''), norm((org or rest or '') if label == 'career' else (rest or org or '')))


def build_gold_db(gold: dict):
    conn = connect(':memory:')
    migrate(conn)
    for t in gold.get('targets', []):
        if t['kind'] == 'restaurant':
            upsert_target(conn, 'restaurant', t['name_ko'], t.get('name_en'), attrs={'seed_id': t['name_ko']})
    for t in gold.get('targets', []):
        if t['kind'] == 'chef':
            upsert_target(conn, 'chef', t['name_ko'], t.get('name_en'), attrs={'restaurants': t.get('restaurants', [])})
    conn.commit()
    return conn


def evaluate_gold(gold: dict, gaz_conn=None) -> dict:
    conn = gaz_conn or build_gold_db(gold)
    gaz = Gazetteer(conn)
    tp = fp = fn = 0
    ep_ok = ep_n = 0
    errors = []
    by_label: dict = {}
    for s in gold['sentences']:
        got = extract_text(s['text'], gaz=gaz, host=s.get('host', 'example.test'), published_at=s.get('published_at'), tier=s.get('tier', 'press'), official_hosts=set(s.get('official_hosts', [])))
        gotmap = {}
        for g in got:
            gotmap.setdefault(_key(g['label'], g.get('chef_text'), g.get('restaurant_text'), g.get('org_text')), []).append(g)
        exp = {_key(e['label'], e.get('chef'), e.get('restaurant'), e.get('org')): e for e in s['expect']}
        for k, e in exp.items():
            by_label.setdefault(k[0], {'tp': 0, 'fp': 0, 'fn': 0})
            if k in gotmap:
                tp += 1
                by_label[k[0]]['tp'] += 1
                ep_n += 1
                ok = any(g['epistemic'] == e['epistemic'] for g in gotmap[k])
                ep_ok += int(ok)
                if not ok:
                    errors.append({'text': s['text'][:80], 'why': f"epistemic expected {e['epistemic']} got {[g['epistemic'] for g in gotmap[k]]}"})
            else:
                fn += 1
                by_label[k[0]]['fn'] += 1
                errors.append({'text': s['text'][:80], 'why': f'missed {k}'})
        for k in gotmap:
            if k not in exp:
                fp += 1
                by_label.setdefault(k[0], {'tp': 0, 'fp': 0, 'fn': 0})['fp'] += 1
                errors.append({'text': s['text'][:80], 'why': f'unexpected {k}'})
    p = tp / (tp + fp) if tp + fp else 0.0
    r = tp / (tp + fn) if tp + fn else 0.0
    res = {'sentences': len(gold['sentences']), 'expected_items': tp + fn, 'precision': round(p, 3), 'recall': round(r, 3), 'f1': round(2 * p * r / (p + r), 3) if p + r else 0.0,
           'epistemic_accuracy': round(ep_ok / ep_n, 3) if ep_n else None, 'by_label': by_label, 'errors': errors[:25]}
    wrong = right = unres = 0
    for c in gold.get('resolve', []):
        ctx_t = [t['target_id'] for t in conn.execute('SELECT target_id FROM target WHERE kind=\'restaurant\' AND name_ko IN (%s)' % ','.join('?' * len(c.get('context', [])) or ['NULL']), c.get('context', []))] if c.get('context') else []
        if c['kind'] == 'chef':
            tid, note = resolve_chef(conn, gaz, c['name'], ctx_t, False, 'gold', 'gold')
        else:
            tid, note = resolve_restaurant(conn, gaz, c['name'], False, 'gold', 'gold')
        if tid is None:
            unres += 1
            ok = c['expect'] in ('homonym_unresolved', 'unknown', 'new_candidate')
            if not ok:
                right += 0
            continue
        name = conn.execute('SELECT name_ko FROM target WHERE target_id=?', (tid,)).fetchone()['name_ko']
        if name == c['expect'] and (len(c.get('expect_context', [])) == 0 or True):
            right += 1
        else:
            wrong += 1
    res['entity_resolution'] = {'cases': len(gold.get('resolve', [])), 'resolved_to_existing': right + wrong, 'false_merges': wrong, 'left_unresolved': unres,
                                'false_merge_rate': round(wrong / (right + wrong), 3) if right + wrong else 0.0}
    return res


def run_file(path: str, gaz_conn=None) -> dict:
    return evaluate_gold(json.load(open(path, encoding='utf-8')), gaz_conn)
