"""Deterministic, non-AI closing of clear-cut review_queue items.

Never approves a public claim and never edits a fact. It only closes queue items that a
human cannot act on or that repeat another item, and rejects candidate targets whose names
are plainly not a person or a venue. Every closed item keeps the decision under
payload['deterministic_resolution'] so it can be audited or reopened.

Rules (each item is touched by at most one rule):
  career_no_quote          AI career output that failed validation and stored no quote, chef or venue.
  career_duplicate_quote   Same quote wording as another open unbound_career item: merged into it.
  career_listing_snippet   unbound_career quote taken from a search or listing page, not an article.
  career_covered           One named chef + only venues that chef already holds a verified or
                           source-claimed employment at, with no former/past wording: already covered.
  heavy_fetch_terminal     Heavy fetch guard item whose frontier URL is now terminal (failed) or done.
  candidate_rejected       Candidate target already rejected, or a name that is plainly not a person/venue.
  candidate_known_chef     Candidate chef target already mapped to an existing chef.
  conflict_duplicate       Several open conflicts on the same subject/field/claim: one item stays open
                           with the other quotes merged into it. Conflict rows themselves are untouched.
  quality_conflict_dup     Monthly quality:open_conflict warning for a conflict that already has a
                           conflict review item (or whose conflict is closed).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
from collections import Counter, defaultdict

from kg_collect.review import reject_reason
from kg_collect.targets import norm
from .claims import quote_fingerprint
from .db import stable_id, utcnow

CAREER_VALIDATION = ('invalid_task_json', 'quote_missing', 'quote_not_exact', 'offset_ambiguous',
                     'task_previously_rejected', 'shape_invalid')
LISTING_URL = re.compile(r'articleList\.html|[?&](?:sc_word|keyword|kwd|q|query|search)=|/search(?:[/?]|$)|/tag/', re.I)
PAST_WORDS = re.compile(r'(?:^|[\s(（])전\s|출신|거쳐|거친|역임|떠나|떠난|퇴사|이전에|former|formerly|\bex-|previously|used to', re.I)
CHEF_TITLE = r'\s*(?:셰프|쉐프|오너\s*셰프|총괄\s*셰프|헤드\s*셰프|chef)'
OTHER_TITLED = re.compile(r'([가-힣]{2,4})\s*(?:셰프|쉐프)')
HUMAN_HOLD = {'ambiguous', 'hold', 'reject'}

# Candidate names that are plainly not a person or not a venue. Kept narrow on purpose.
NOT_A_PERSON = {norm(n) for n in ('마스터', '최연소', '부부', '조리장', '대표', '원장', '명장', '장인', '사장')}
NOT_A_VENUE = re.compile(
    r'^(?:흑백\s*요리사\s*\d*|(?:많은|유명|여러|다양한|일부|스타|유명한|인기)\s*레스토랑|조리장|주방장|셰프'
    r'|캐나다|미국|일본|프랑스|중국|영국|이탈리아|스페인|독일|호주|태국|베트남|대만|덴마크|싱가포르)$'
    r'|베스트\s*레스토랑|50\s*best|프로젝트$|예대$|요리\s*경연|서바이벌', re.I)


def _annotate(payload_json, decision):
    body = json.loads(payload_json or '{}')
    body['deterministic_resolution'] = decision
    return json.dumps(body, ensure_ascii=False, sort_keys=True)


def _close(conn, row, state, rule, now, out, **extra):
    decision = {'decision': state, 'rule': rule, 'at': now, 'by': 'deterministic_review_resolve', **extra}
    conn.execute('UPDATE review_queue SET state=?, resolved_at=?, payload_json=? WHERE review_id=? AND state=\'open\'',
                 (state, now, _annotate(row['payload_json'], decision), row['review_id']))
    out[rule] += 1


def _merge_into(conn, keep_id, merged):
    row = conn.execute('SELECT payload_json FROM review_queue WHERE review_id=?', (keep_id,)).fetchone()
    body = json.loads(row['payload_json'] or '{}')
    have = {m.get('review_id') for m in body.get('merged_duplicates', [])}
    body['merged_duplicates'] = body.get('merged_duplicates', []) + [m for m in merged if m['review_id'] not in have]
    conn.execute('UPDATE review_queue SET payload_json=? WHERE review_id=?',
                 (json.dumps(body, ensure_ascii=False, sort_keys=True), keep_id))


def _table(conn, name):
    return bool(conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone())


# ───────────── career assertions ─────────────
def _career(conn, now, out):
    if not _table(conn, 'career_assertion'):
        return
    rows = conn.execute("""SELECT q.review_id, q.reason, q.payload_json, a.quote, a.chef_text, a.venue_text, a.state AS a_state,
                                  a.canonical_url
                           FROM review_queue q JOIN career_assertion a ON a.assertion_id=q.subject_id
                           WHERE q.state='open' AND q.subject_type='career_assertion'
                           ORDER BY q.created_at, q.rowid""").fetchall()
    unbound = []
    for r in rows:
        if (r['reason'] in CAREER_VALIDATION and r['a_state'] == 'review' and r['quote'] is None
                and r['chef_text'] is None and r['venue_text'] is None):
            _close(conn, r, 'dismissed', 'career_no_quote', now, out, why='AI output failed validation; no quote, chef or venue stored')
        elif r['reason'] == 'unbound_career' and r['a_state'] == 'review' and r['quote']:
            unbound.append(r)
    groups = defaultdict(list)
    for r in unbound:
        groups[quote_fingerprint(r['quote'])].append(r)
    covered = _CoverageIndex(conn)
    for fp, items in groups.items():
        # An article source represents the group before any listing-page copy; otherwise the oldest item.
        items.sort(key=lambda r: bool(LISTING_URL.search(r['canonical_url'] or '')))
        keep, dups = items[0], items[1:]
        for d in dups:
            _close(conn, d, 'dismissed', 'career_duplicate_quote', now, out, duplicate_of=keep['review_id'])
        if dups:
            _merge_into(conn, keep['review_id'], [{'review_id': d['review_id'], 'url': d['canonical_url']} for d in dups])
        if LISTING_URL.search(keep['canonical_url'] or ''):
            _close(conn, keep, 'dismissed', 'career_listing_snippet', now, out,
                   why='quote comes from a search/listing page snippet, not an article')
            continue
        hit = covered.match(keep['quote'])
        if hit:
            _close(conn, keep, 'resolved', 'career_covered', now, out, chef_id=hit[0], employment_ids=hit[1],
                   why='chef and venue already linked by an existing verified/source-claimed employment')


class _CoverageIndex:
    def __init__(self, conn):
        self.chefs = [(r['chef_id'], r['display_name']) for r in
                      conn.execute('SELECT chef_id, display_name FROM chef WHERE length(display_name)>=2')]
        self.chef_names = {n for _, n in self.chefs}
        self.venues = defaultdict(set)
        for r in conn.execute("""SELECT b.branch_id, r.name, r.name_en FROM branch b JOIN restaurant r USING(restaurant_id)"""):
            for n in (r['name'], r['name_en']):
                if n and len(n.strip()) >= 2:
                    self.venues[n.strip()].add(r['branch_id'])
        self.emp = defaultdict(dict)
        for r in conn.execute("""SELECT e.employment_id, e.chef_id, e.branch_id FROM employment e JOIN claim c USING(claim_id)
                                 WHERE e.state<>'rejected' AND c.status IN ('verified','source_claim') AND e.branch_id IS NOT NULL"""):
            self.emp[r['chef_id']].setdefault(r['branch_id'], []).append(r['employment_id'])

    def match(self, quote):
        if PAST_WORDS.search(quote):
            return None
        named = [(cid, n) for cid, n in self.chefs if re.search(re.escape(n) + CHEF_TITLE, quote, re.I)
                 or re.search(r'chef\s+' + re.escape(n), quote, re.I)]
        if len({cid for cid, _ in named}) != 1:
            return None
        cid, name = named[0]
        # Any other titled person (X 셰프) that is not this chef makes the subject unclear.
        for other in OTHER_TITLED.findall(quote):
            if other != name and not name.startswith(other) and norm(other) not in _generic():
                return None
        venues = [v for v in self.venues if v in quote and v not in self.chef_names]
        if not venues:
            return None
        branches = set().union(*(self.venues[v] for v in venues))
        held = self.emp.get(cid, {})
        # Every venue mentioned must be one this chef already holds; a single unknown venue keeps the item open.
        for v in venues:
            if not (self.venues[v] & held.keys()):
                return None
        return cid, sorted({e for b in branches & held.keys() for e in held[b]})


def _generic():
    from kg_collect.review import NAME_STOP
    return NAME_STOP


# ───────────── heavy fetch guard items ─────────────
def _heavy(conn, heavy_db, now, out):
    if not heavy_db or not os.path.exists(heavy_db):
        return
    rows = conn.execute("""SELECT review_id, subject_id, payload_json FROM review_queue
                           WHERE state='open' AND reason='guard_attempts_exhausted' AND subject_type='heavy_frontier'""").fetchall()
    if not rows:
        return
    h = sqlite3.connect('file:' + heavy_db + '?mode=ro', uri=True)
    h.row_factory = sqlite3.Row
    try:
        frontier = {}
        for f in h.execute('SELECT url, canonical_url, state, status, error, host FROM frontier'):
            frontier[stable_id('url', f['url'])] = f
            if f['canonical_url']:
                frontier.setdefault(stable_id('url', f['canonical_url']), f)
    finally:
        h.close()
    for r in rows:
        f = frontier.get(r['subject_id'])
        if f is None:
            continue
        if f['state'] == 'done':
            _close(conn, r, 'resolved', 'heavy_fetch_terminal', now, out, frontier_state='done', host=f['host'])
        elif f['state'] in ('failed', 'skipped'):
            _close(conn, r, 'dismissed', 'heavy_fetch_terminal', now, out, frontier_state=f['state'],
                   status=f['status'], error=f['error'], host=f['host'], why='URL retired after exhausted attempts; no fact involved')


# ───────────── candidate targets ─────────────
def _hold(payload_json):
    body = json.loads(payload_json or '{}')
    review = body.get('chatgpt_review') or body.get('human_review') or {}
    applied = review.get('applied') or {}
    return bool(applied.get('left_open') or applied.get('awaiting_chef_confirmation') or review.get('decision') in HUMAN_HOLD)


def noise_reason(kind, name):
    """Plainly-not-an-entity names, beyond kg_collect.review.reject_reason."""
    n = (name or '').strip()
    if kind == 'chef' and norm(n) in NOT_A_PERSON:
        return 'not_a_person'
    if kind == 'restaurant' and NOT_A_VENUE.search(n):
        return 'not_a_venue'
    return None


def _candidates(conn, now, out):
    if not _table(conn, 'target'):
        return
    mapped = _table(conn, 'target_entity')
    rows = conn.execute("""SELECT q.review_id, q.payload_json, t.target_id, t.kind, t.status, t.name_ko, t.name_en, t.attrs_json
                           FROM review_queue q JOIN target t ON t.target_id=q.subject_id
                           WHERE q.state='open' AND q.reason='candidate_target' AND q.subject_type='target'""").fetchall()
    for r in rows:
        if _hold(r['payload_json']):
            continue
        name = r['name_ko'] or r['name_en']
        if r['status'] == 'rejected':
            _close(conn, r, 'dismissed', 'candidate_rejected', now, out, why='target already rejected')
            continue
        if r['status'] != 'candidate':
            continue
        why = reject_reason(r['kind'], name) or noise_reason(r['kind'], name)
        if why:
            attrs = json.loads(r['attrs_json'] or '{}')
            attrs.update(rejected_reason=why, rejected_at=now, rejected_by='deterministic_review_resolve')
            conn.execute("UPDATE target SET status='rejected', attrs_json=? WHERE target_id=? AND status='candidate'",
                         (json.dumps(attrs, ensure_ascii=False, sort_keys=True), r['target_id']))
            _close(conn, r, 'dismissed', 'candidate_rejected', now, out, why=why)
            continue
        if r['kind'] == 'chef' and mapped:
            m = conn.execute('SELECT chef_id FROM target_entity WHERE target_id=? AND chef_id IS NOT NULL', (r['target_id'],)).fetchone()
            if m:
                _close(conn, r, 'resolved', 'candidate_known_chef', now, out, chef_id=m['chef_id'],
                       why='candidate already mapped to an existing chef')


# ───────────── conflicts ─────────────
def _conflicts(conn, now, out):
    if not _table(conn, 'conflict'):
        return
    rows = conn.execute("""SELECT q.review_id, q.payload_json, q.subject_id, f.subject_type AS f_type, f.subject_id AS f_subject,
                                  f.field, f.claim_b, f.claim_a, f.state AS f_state
                           FROM review_queue q JOIN conflict f ON f.conflict_id=q.subject_id
                           WHERE q.state='open' AND q.reason='conflict' AND q.subject_type='conflict'
                           ORDER BY q.created_at, q.rowid""").fetchall()
    groups = defaultdict(list)
    for r in rows:
        if r['f_state'] == 'open':
            groups[(r['f_type'], r['f_subject'], r['field'], r['claim_b'])].append(r)
    for items in groups.values():
        keep, dups = items[0], items[1:]
        for d in dups:
            _close(conn, d, 'dismissed', 'conflict_duplicate', now, out, duplicate_of=keep['review_id'], conflict_id=d['subject_id'])
        if dups:
            _merge_into(conn, keep['review_id'], [{'review_id': d['review_id'], 'conflict_id': d['subject_id'], 'claim_a': d['claim_a'],
                                                   'quote': json.loads(d['payload_json'] or '{}').get('quote')} for d in dups])
    # Quality warnings repeat open conflicts every month.
    reviewed = {r['subject_id'] for r in conn.execute("SELECT subject_id FROM review_queue WHERE reason='conflict' AND subject_type='conflict'")}
    qrows = conn.execute("""SELECT q.review_id, q.payload_json, q.subject_id, f.state AS f_state, f.subject_type AS f_type,
                                   f.subject_id AS f_subject, f.field, f.claim_b
                            FROM review_queue q LEFT JOIN conflict f ON f.conflict_id=q.subject_id
                            WHERE q.state='open' AND q.reason='quality:open_conflict'
                            ORDER BY q.created_at, q.rowid""").fetchall()
    qgroups = defaultdict(list)
    for r in qrows:
        if r['f_state'] is not None and r['f_state'] != 'open':
            _close(conn, r, 'resolved', 'quality_conflict_dup', now, out, why='conflict no longer open')
        elif r['subject_id'] in reviewed:
            _close(conn, r, 'dismissed', 'quality_conflict_dup', now, out, why='conflict already has its own review item')
        elif r['f_state'] == 'open':
            qgroups[(r['f_type'], r['f_subject'], r['field'], r['claim_b'])].append(r)
    for items in qgroups.values():
        keep, dups = items[0], items[1:]
        for d in dups:
            _close(conn, d, 'dismissed', 'quality_conflict_dup', now, out, duplicate_of=keep['review_id'], conflict_id=d['subject_id'])
        if dups:
            _merge_into(conn, keep['review_id'], [{'review_id': d['review_id'], 'conflict_id': d['subject_id']} for d in dups])


def _heavy_path(conn):
    for row in conn.execute('PRAGMA database_list'):
        if row[1] == 'main' and row[2]:
            return os.path.join(os.path.dirname(row[2]), 'heavy_queue.db')
    return None


def open_counts(conn):
    return dict(Counter(f"{r['reason']}|{r['subject_type']}" for r in
                        conn.execute("SELECT reason, subject_type FROM review_queue WHERE state='open'")))


def resolve_review_backlog(conn, now=None, heavy_db=None):
    """Close clear-cut open review items. Caller commits (or rolls back for a dry run)."""
    now = now or utcnow()
    out = Counter()
    _career(conn, now, out)
    _heavy(conn, heavy_db if heavy_db is not None else _heavy_path(conn), now, out)
    _candidates(conn, now, out)
    _conflicts(conn, now, out)
    return dict(out)


def main(argv=None):
    from .db import DEFAULT_DB, connect
    ap = argparse.ArgumentParser(description='Deterministic review_queue backlog resolver (no AI).')
    ap.add_argument('--db', default=DEFAULT_DB)
    ap.add_argument('--heavy-db', default=None)
    ap.add_argument('--apply', action='store_true', help='commit; default is a dry run that rolls back')
    a = ap.parse_args(argv)
    conn = connect(a.db)
    before = open_counts(conn)
    stats = resolve_review_backlog(conn, heavy_db=a.heavy_db)
    after = open_counts(conn)
    if a.apply:
        conn.commit()
    else:
        conn.rollback()
    print(json.dumps({'mode': 'apply' if a.apply else 'dry_run', 'open_before': sum(before.values()), 'open_after': sum(after.values()),
                      'closed_by_rule': stats, 'before': before, 'after': after}, ensure_ascii=False, sort_keys=True, indent=1))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
