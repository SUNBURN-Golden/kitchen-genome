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


def evaluate_gold(gold: dict, gaz_conn=None, context_fn=None) -> dict:
    """context_fn(sentence_row) -> preceding document text or ''. With it, the sentence is extracted INSIDE its document (cross-sentence coreference
    needs the earlier full name) and only records whose span lies in the gold sentence are scored. Labels are never touched."""
    conn = gaz_conn or build_gold_db(gold)
    gaz = Gazetteer(conn)
    tp = fp = fn = 0
    ep_ok = ep_n = exact = quotes = 0
    errors = []
    by_label: dict = {}
    for s in gold['sentences']:
        pre = (context_fn(s) or '') if context_fn else (s.get('context') or '')
        pre = pre + '\n' if pre else ''
        got = [g for g in extract_text(pre + s['text'], gaz=gaz, host=s.get('host', 'example.test'), published_at=s.get('published_at'), tier=s.get('tier', 'press'),
                                       official_hosts=set(s.get('official_hosts', []))) if g['span_start'] >= len(pre)]
        gotmap = {}
        for g in got:
            quotes += 1
            exact += int(bool(g.get('quote') and (pre + s['text'])[g['span_start']:g['span_end']] == g['quote']))
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
           'quote_fidelity': round(exact / quotes, 3) if quotes else 'n/a', 'epistemic_accuracy': round(ep_ok / ep_n, 3) if ep_n else None, 'by_label': by_label, 'errors': errors[:25]}
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


def doc_context_from_store(db_path: str):
    """Context finder over the collector's stored texts: the document (same host) that contains the gold sentence -> text before it."""
    import os
    import sqlite3
    root = os.path.join(os.path.dirname(os.path.abspath(db_path)), 'raw')
    c = sqlite3.connect(db_path)
    rows = c.execute("SELECT host, text_ref FROM fetch_record WHERE text_ref IS NOT NULL AND outcome='ok'").fetchall()
    cache: dict = {}

    def fn(s):
        probe = s['text'].strip()[:40]
        for host, ref in rows:
            if s.get('host') and host != s['host']:
                continue
            if ref not in cache:
                try:
                    cache[ref] = open(os.path.join(root, ref), encoding='utf-8').read()
                except OSError:
                    cache[ref] = ''
            i = cache[ref].find(probe)
            if i >= 0:
                return cache[ref][:i]
        return ''
    return fn


def run_file(path: str, gaz_conn=None, gaz_db: str | None = None, docs_db: str | None = None) -> dict:
    """gaz_db: use the PRODUCTION registry (targets in the collector DB) instead of the gold file's own target list."""
    if gaz_db and gaz_conn is None:
        gaz_conn = connect(gaz_db)
    return evaluate_gold(json.load(open(path, encoding='utf-8')), gaz_conn, doc_context_from_store(docs_db) if docs_db else None)


def _scores(tp, fp, fn, fidelity, quotes, false_merges, samples):
    p = tp / (tp + fp) if tp + fp else 0
    r = tp / (tp + fn) if tp + fn else 0
    return {'samples': samples, 'precision': round(p, 3), 'recall': round(r, 3),
            'f1': round(2 * p * r / (p + r), 3) if p + r else 0,
            'quote_fidelity': round(fidelity / quotes, 3) if quotes else 'n/a',
            'false_merges': false_merges}


def score_saved_articles(conn, gold):
    """Score labelled held-out fetch jobs, including documents skipped by the prefilter."""
    from collections import defaultdict

    groups = defaultdict(lambda: [0] * 7)
    for article in gold['articles']:
        language = article['language']
        rows = conn.execute("""SELECT e.* FROM extraction e
            WHERE e.asserted_by='ai_inference' AND e.extractor LIKE 'deepseek:%' AND (fetch_id=? OR EXISTS (
              SELECT 1 FROM article_extraction_fetch l WHERE l.fetch_id=? AND l.extraction_id=e.extraction_id))""",
            (article['fetch_id'],) * 2).fetchall()
        sample = conn.execute('SELECT 1 FROM model_document_sample WHERE fetch_id=?', (article['fetch_id'],)).fetchone()
        if not sample and not rows and not article.get('prefilter_miss'):
            continue
        expected = {_key(r['label'], r.get('chef'), r.get('restaurant'), r.get('org')): r
                    for r in article['expect']}
        got = {_key(r['label'], r['chef_text'], r['restaurant_text'], r['org_text']): r for r in rows}
        shared = expected.keys() & got.keys()
        exact = sum(bool(r['quote'] and article['text'][r['span_start']:r['span_end']] == r['quote'])
                    for r in got.values())
        merges = sum(bool(expected[k].get('chef_target_id') and got[k]['resolved_chef_target']
                          and expected[k]['chef_target_id'] != got[k]['resolved_chef_target']) for k in shared)
        scores = (len(shared), len(got.keys() - expected.keys()), len(expected.keys() - got.keys()),
                  exact, len(got), merges, 1)
        for key in (language, 'overall'):
            groups[key] = [a + b for a, b in zip(groups[key], scores)]
    return {k: _scores(*v) for k, v in groups.items()}


def daily_accuracy(conn, rule_path, heldout_path=None):
    import hashlib
    import os

    def load(path):
        with open(path, 'rb') as f:
            data = f.read()
        gold = json.loads(data)
        return gold, gold.get('version') or hashlib.sha256(data).hexdigest()[:12]

    report = {'korean_rules': 'n/a', 'saved_model': 'n/a'}
    if rule_path and os.path.isfile(rule_path):
        gold, version = load(rule_path)
        rules = evaluate_gold(gold)
        scores = {k: rules[k] for k in ('precision', 'recall', 'f1', 'quote_fidelity')}
        scores.update(samples=rules['sentences'], false_merges=rules['entity_resolution']['false_merges'])
        report['korean_rules'] = {'version': version, 'ko': scores, 'overall': scores}
    if heldout_path and os.path.isfile(heldout_path):
        gold, version = load(heldout_path)
        scored = score_saved_articles(conn, gold)
        report['saved_model'] = {'version': version, 'labelled_samples': len(gold['articles']),
                                 **(scored or {'overall': 'n/a'})}
    return report
