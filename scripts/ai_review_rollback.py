"""Roll back findings the DeepSeek review stage published (data/seed.json `ai_reviewed`).

  python3 scripts/ai_review_rollback.py --extraction-id ext_... [--extraction-id ...]
  python3 scripts/ai_review_rollback.py --review-id rev_...
  python3 scripts/ai_review_rollback.py --since 2026-11-09T00:00:00Z      # everything published at/after this UTC time
  add --db data_store/kg_service.db to also re-close the DB review row as rolled back (state stays closed, payload marks rollback)

Removes matching entries from seed (restaurants, kitchens, ai_reviewed_unlinked) and appends a
'rollback' line per entry to data/ai_review_log.jsonl. Rebuilds the affected career links.
Then ship through the normal branch -> PR -> squash merge -> deploy flow."""
import argparse
import json
import os
import sys
from contextlib import ExitStack, closing
from copy import deepcopy
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'daemon'))
from kg_ai import promotion, review
from kg_collect import site_projection
from kg_collect.global_queue import write
from kg_collect.site_publish import atomic_json, seed_lock
from kg_daemon.db import connect, migrate, utcnow


def rollback(seed, pred):
    removed = []
    for kind in ('restaurants', 'kitchens'):
        for holder in seed.get(kind, []):
            entries = holder.get('ai_reviewed', [])
            matches, keep = [], []
            for entry in entries:
                (matches if pred(entry) else keep).append(entry)
            if matches:
                removed.extend((f"{kind[:-1]}:{holder['id']}", entry) for entry in matches)
                if keep:
                    holder['ai_reviewed'] = keep
                else:
                    del holder['ai_reviewed']
    if 'ai_reviewed_unlinked' in seed:
        entries, keep = seed['ai_reviewed_unlinked'], []
        for entry in entries:
            if pred(entry):
                removed.append(('unlinked', entry))
            else:
                keep.append(entry)
        if keep:
            seed['ai_reviewed_unlinked'] = keep
        else:
            seed.pop('ai_reviewed_unlinked')
    ids = {r['review_id'] for _, r in removed if r.get('review_id')}
    if ids:
        promotion.withdraw(seed, ids, utcnow())
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
    now = utcnow()
    with seed_lock(a.seed), ExitStack() as resources:
        with open(a.seed, encoding='utf-8') as stream:
            seed = json.load(stream)
        previous = deepcopy(seed)
        conn = None
        pending = []
        if a.db:
            conn = resources.enter_context(closing(connect(a.db)))
            migrate(conn)
            resources.enter_context(write(conn))
            if any(pred(entry) for _, _, entry in promotion.annotations(seed)) and seed.get(promotion.KEY, {}).get('supports'):
                site_projection.refresh(conn, seed)
            for row in conn.execute("""SELECT json_extract(payload_json,'$.ai_review.delivery.entry') FROM review_queue
                                      WHERE reason='ai_inference' AND state='resolved'
                                      AND json_type(payload_json,'$.ai_review.delivery.entry')='object'"""):
                entry = json.loads(row[0])
                if pred(entry):
                    pending.append(('unlinked', entry))
        removed = rollback(seed, pred)
        pending_ids = {entry['review_id'] for _, entry in pending}
        if pending_ids:
            promotion.withdraw(seed, pending_ids, now)
        if conn:
            for rid in seed.get(promotion.KEY, {}).get('withdrawn', ()):
                conn.execute("""UPDATE review_queue SET state='dismissed',
                    payload_json=json_set(json_remove(payload_json,'$.ai_review.delivery'),'$.ai_review.rolled_back',json(?))
                    WHERE review_id=? AND json_type(payload_json,'$.ai_review.rolled_back') IS NULL""",
                    (json.dumps(dict(at=now, reason=a.reason)), rid))
            if removed or pending:
                promotion.prune_identities(conn, seed)
                site_projection.refresh(conn, seed)
                if seed.get(promotion.KEY, {}).get('supports'):
                    promotion.record_links(seed, now)
        if seed != previous:
            atomic_json(a.seed, seed)
        if removed or pending:
            logged = promotion.tombstones(a.log)
            for target, entry in removed + pending:
                rid = entry.get('review_id')
                if rid and rid in logged:
                    continue
                review.append_log(a.log, dict(at=now, action='rollback', target=target, extraction_id=entry.get('extraction_id'),
                                              review_id=rid, quote=entry.get('quote'), src=entry.get('src'), reason=a.reason))
                logged.add(rid)
    print(json.dumps({'removed': len(removed)}))


if __name__ == '__main__':
    main()
