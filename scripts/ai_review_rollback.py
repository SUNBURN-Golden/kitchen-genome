"""Roll back findings the DeepSeek review stage published (data/seed.json `ai_reviewed`).

  python3 scripts/ai_review_rollback.py --extraction-id ext_... [--extraction-id ...]
  python3 scripts/ai_review_rollback.py --review-id rev_...
  python3 scripts/ai_review_rollback.py --since 2026-11-09T00:00:00Z      # everything published at/after this UTC time
  add --db data_store/kg_service.db to also re-close the DB review row as rolled back (state stays closed, payload marks rollback)

Removes matching entries from seed (restaurants, kitchens, ai_reviewed_unlinked) and appends a
'rollback' line per entry to data/ai_review_log.jsonl. Nothing else in seed is touched.
Then ship through the normal branch -> PR -> squash merge -> deploy flow."""
import argparse, json, os, sqlite3, datetime as dt
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def rollback(seed, pred):
    removed = []
    holders = list(seed.get('restaurants', [])) + list(seed.get('kitchens', []))
    for h in holders:
        keep = [x for x in h.get('ai_reviewed', []) if not pred(x)]
        removed += [(f"{'kitchen' if h in seed.get('kitchens', []) else 'restaurant'}:{h['id']}", x) for x in h.get('ai_reviewed', []) if pred(x)]
        if 'ai_reviewed' in h:
            if keep: h['ai_reviewed'] = keep
            else: del h['ai_reviewed']
    if 'ai_reviewed_unlinked' in seed:
        removed += [('unlinked', x) for x in seed['ai_reviewed_unlinked'] if pred(x)]
        keep = [x for x in seed['ai_reviewed_unlinked'] if not pred(x)]
        if keep: seed['ai_reviewed_unlinked'] = keep
        else: del seed['ai_reviewed_unlinked']
    return removed


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument('--extraction-id', action='append', default=[])
    ap.add_argument('--review-id', action='append', default=[])
    ap.add_argument('--since')
    ap.add_argument('--reason', default='manual rollback')
    ap.add_argument('--seed', default=os.path.join(ROOT, 'data', 'seed.json'))
    ap.add_argument('--log', default=os.path.join(ROOT, 'data', 'ai_review_log.jsonl'))
    ap.add_argument('--db')
    a = ap.parse_args(argv)
    if not (a.extraction_id or a.review_id or a.since):
        ap.error('give --extraction-id, --review-id or --since')
    pred = lambda x: (x.get('extraction_id') in a.extraction_id or x.get('review_id') in a.review_id
                      or (a.since is not None and (x.get('reviewed_at') or '') >= a.since))
    seed = json.load(open(a.seed, encoding='utf-8'))
    removed = rollback(seed, pred)
    if removed:
        json.dump(seed, open(a.seed, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
    now = dt.datetime.now(dt.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    with open(a.log, 'a', encoding='utf-8') as fh:
        for target, x in removed:
            fh.write(json.dumps({'at': now, 'action': 'rollback', 'target': target, 'extraction_id': x.get('extraction_id'), 'review_id': x.get('review_id'),
                                 'quote': x.get('quote'), 'src': x.get('src'), 'reason': a.reason}, ensure_ascii=False, sort_keys=True) + '\n')
    if a.db and removed:
        c = sqlite3.connect(a.db)
        for _, x in removed:
            row = c.execute('SELECT payload_json FROM review_queue WHERE review_id=?', (x.get('review_id'),)).fetchone()
            if row:
                p = json.loads(row[0] or '{}'); p.setdefault('ai_review', {})['rolled_back'] = {'at': now, 'reason': a.reason}
                c.execute("UPDATE review_queue SET state='dismissed', payload_json=? WHERE review_id=?", (json.dumps(p, ensure_ascii=False, sort_keys=True), x.get('review_id')))
        c.commit()
    print(json.dumps({'removed': len(removed)}))


if __name__ == '__main__':
    main()
