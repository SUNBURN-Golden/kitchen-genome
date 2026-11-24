#!/usr/bin/env python3
"""Replay AI promotion on temporary copies, with no provider calls."""
import argparse
from collections import Counter
from contextlib import closing
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import sys
from tempfile import TemporaryDirectory
from urllib.parse import urlsplit, urlunsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'daemon'))
from kg_ai import promotion
from kg_collect.global_queue import write
from kg_collect.site_publish import atomic_json, safe_url
from kg_collect.targets import name_key
from kg_daemon.db import connect, migrate, utcnow

GUARDED_VENUES = {name_key(name) for name in (
    '오닐', '오닐(ONIL)', "O'Nil", 'DOORI', '주052', '조선호텔앤리조트', '신세계그룹', '신세계백화점',
    '오덴세', 'Odense', '마스터셰프 코리아', '마스터셰프 코리아4', '흑백요리사 : 요리 계급 전쟁2',
    '사찰음식 연구소', '궁중음식연구원', '라 리스트(La Liste)', '더본코리아', '가온소사이어티',
    '레스케이프 호텔', '신세계조선호텔', '퀴진케이',
)}


def _guarded(venue, alias=None):
    return bool(venue and GUARDED_VENUES.intersection(map(name_key, promotion._spellings(venue, alias))))


def _diagnostic_url(url):
    if not safe_url(url):
        return None
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, '', ''))


def _print(value):
    print(json.dumps(value, ensure_ascii=False, sort_keys=True))


def _replay(conn, seed, raw_dir, log_path, now):
    with write(conn):
        promoted = promotion.backfill(conn, seed, raw_dir, log_path, now)
        skips = promotion.skip_histogram(conn)
        rows = conn.execute('SELECT * FROM ai_promotion_skip ORDER BY unlinked_index').fetchall()
    assert sum(skips.values()) == len(seed.get('ai_reviewed_unlinked', ())) == len(rows)
    for i, (row, entry) in enumerate(zip(rows, seed.get('ai_reviewed_unlinked', ()))):
        assert (row['unlinked_index'], row['review_id'], row['extraction_id'], row['quote_key']) == (
            i, *promotion._skip_fingerprint(entry))
    return promoted, skips, rows


def run(conn, seed_path, log_path, ledger_path, raw_dir, now):
    seed_bytes, audit_bytes = seed_path.read_bytes(), log_path.read_bytes()
    seed, ledger = json.loads(seed_bytes), json.loads(ledger_path.read_bytes())
    before = deepcopy(seed.get('ai_reviewed_unlinked', ()))
    cohort = {e['review_id'] for e in before if e.get('label') in promotion.LABELS
              and e.get('chef') and e.get('restaurant') and e.get('review_id')}
    guarded = {e['review_id'] for _, _, e in promotion.annotations(seed) if _guarded(e.get('restaurant'))}
    prior = deepcopy(seed.get(promotion.KEY, {}))
    checked_originals = 0
    for entry in before:
        if entry.get('review_id') not in cohort or not safe_url(entry.get('src')):
            continue
        original, _ = promotion._original(conn, entry, raw_dir)
        if original and original[1]['snapshot_checked']:
            checked_originals += 1
    promoted, skips, skip_rows = _replay(conn, seed, raw_dir, log_path, now)
    atomic_json(seed_path, seed)
    state = seed.get(promotion.KEY, {})
    supports = state.get('supports', {})
    gained = supports.keys() - prior.get('supports', {}).keys()
    links = state.get('links', {})
    public = {}
    for kind in ('restaurants', 'kitchens'):
        for holder in seed.get('career_projection', {}).get(kind, ()):
            for edge in holder.get('career_affiliations', ()):
                for evidence in edge['evidence']:
                    if evidence.get('badge') == promotion.BADGE:
                        key = (holder['id'], edge['chef_id'], edge['relation'], evidence['src'], evidence['quote'])
                        public[key] = edge
    covered = set()
    linked = set()
    guard_hits = guarded & gained
    guard_hits.update(rid for rid in gained if _guarded(supports[rid]['venue_text'], supports[rid].get('bilingual_venue')))
    holders = {row['id']: row for kind in ('restaurants', 'kitchens') for row in seed[kind]}
    for lid, link in sorted(links.items()):
        if lid not in prior.get('links', {}) and (row := holders.get(link['holder'])):
            if _guarded(row['name'], row.get('name_en') or row.get('en')):
                guard_hits.update(link['reviews'])
        if not (review_ids := set(link['reviews']) & gained):
            continue
        linked.update(review_ids)
        for rid in review_ids:
            row = supports[rid]
            if (row['holder'], row['chef_id'], row['relation'], row['src'], row['quote']) in public:
                covered.add(rid)
        row = supports[min(review_ids)]
        _print(dict(link=lid, chef=row.get('resolved_chef_text') or row['chef_text'], chef_id=link['chef'],
                    venue=row['venue_text'], branch_id=link['branch'], relation=link['relation'],
                    gate=link['gate'], review_ids=sorted(review_ids),
                    sources=sorted({url for src in link['sources'] if (url := _diagnostic_url(src))})))
    remains = {e.get('review_id') for e in seed.get('ai_reviewed_unlinked', ())}
    events = promotion.events(seed, ledger)
    annotation_only = {e['review_id'] for e in before} - remains - gained
    one = dict(seed_sha256=hashlib.sha256(seed_bytes).hexdigest(), unlinked_before=len(before),
               unlinked_after=len(skip_rows), cohort_size=len(cohort), cohort_matched_originals=checked_originals,
               cohort_published=len(covered & cohort), new_supports=len(gained),
               annotation_only_moves=len(annotation_only), cohort_annotation_only=len(cohort & annotation_only),
               support_only=len(gained - covered), support_without_link=len(gained - linked),
               withdrawn_supports=len(prior.get('supports', {}).keys() - supports.keys()),
               promoted=promoted, new_link_records=len(links.keys() - prior.get('links', {}).keys()),
               upgrades=sum(l.get('gate') == 'verified' and prior.get('links', {}).get(k, {}).get('gate') == 'checking'
                            for k, l in links.items()),
               skips=skips, cohort_skips=dict(Counter(r['reason'] for r in skip_rows if r['review_id'] in cohort)),
               guard_hits=len(guard_hits), pending_publication_events=len(events))
    _print({'first_pass': one})
    ledger.extend(events)
    atomic_json(ledger_path, ledger)
    first_bytes = seed_path.read_bytes()
    seed = json.loads(first_bytes)
    second, second_skips, second_rows = _replay(conn, seed, raw_dir, log_path, now)
    atomic_json(seed_path, seed)
    second_events = promotion.events(seed, ledger)
    stable = (first_bytes == seed_path.read_bytes() and audit_bytes == log_path.read_bytes()
              and skip_rows == second_rows and skips == second_skips)
    _print(dict(second_pass=dict(promoted=second, pending_publication_events=len(second_events), unchanged=stable,
                                 new_supports=len(seed.get(promotion.KEY, {}).get('supports', {}).keys() - supports.keys()),
                                 new_link_records=len(seed.get(promotion.KEY, {}).get('links', {}).keys() - links.keys()))))
    return 0 if stable and not second and not second_events and not guard_hits else 1


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', required=True, type=Path, help='Service DB opened read-only for backup')
    parser.add_argument('--raw-dir', type=Path, help='Retained articles read-only (default: raw next to DB)')
    parser.add_argument('--seed', type=Path, default=ROOT / 'data/seed.json')
    parser.add_argument('--log', type=Path, default=ROOT / 'data/ai_review_log.jsonl')
    parser.add_argument('--ledger', type=Path, default=ROOT / 'crawler/applied.json')
    parser.add_argument('--now', default=utcnow())
    args = parser.parse_args(argv)
    with TemporaryDirectory(prefix='kg-ai-promotion-', dir='/tmp') as directory:
        target = Path(directory)
        seed, log, ledger = [Path(shutil.copyfile(path, target / name)) for path, name in
                             ((args.seed, 'seed.json'), (args.log, 'review.jsonl'), (args.ledger, 'applied.json'))]
        with closing(connect(str(target / 'service.db'))) as conn:
            with closing(sqlite3.connect(args.db.resolve().as_uri() + '?mode=ro', uri=True)) as original:
                original.backup(conn)
            migrate(conn)
            return run(conn, seed, log, ledger, args.raw_dir or args.db.resolve().parent / 'raw', args.now)


if __name__ == '__main__':
    raise SystemExit(main())
