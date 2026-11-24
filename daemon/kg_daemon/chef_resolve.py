"""Deterministic chef identity for unknown_chef review items and entity resolution.

Matches a surface name to an existing chef by exact display name, non-legacy alias,
name keys (including romanization folds), or a chef spelling already stored on a
Michelin / Wikipedia extraction or an official profile fact. Never creates a chef
and never guesses when two people share a name.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass

from kg_collect.lineage_identity import candidate_keys
from kg_collect.review import ACRONYM, INSTITUTION, NAME_STOP
from kg_collect.targets import norm
from kg_daemon.db import utcnow
from kg_daemon.seed_migration import split_chefs

GUIDE_HOST_SQL = "(e.host LIKE '%.wikipedia.org' OR instr(e.host, 'michelin') > 0)"
MULTI_SEP = re.compile(r'[/&+,]| 및 ')
HUMAN_HOLD_DECISIONS = {'ambiguous', 'hold', 'reject'}


@dataclass(frozen=True)
class NameClass:
    status: str  # single, multiple, reject
    names: tuple[str, ...] = ()
    reason: str | None = None


@dataclass(frozen=True)
class ChefMatch:
    status: str  # matched, homonym, unknown, multiple, reject
    chef_id: str | None = None
    names: tuple[str, ...] = ()
    reason: str | None = None
    source: str | None = None


def _not_a_person(name: str | None) -> str | None:
    """Place, nationality, role, acronym, or institution — not a person. Latin letters stay allowed
    (bilingual names, and fixture names such as 테스트셰프C)."""
    text = (name or '').strip()
    if not text:
        return 'empty'
    if norm(text) in NAME_STOP:
        return 'generic_name'
    if ACRONYM.match(text):
        return 'acronym_not_a_person'
    if INSTITUTION.search(text):
        return 'institution_not_a_person'
    return None


def classify_chef_name(raw: str | None) -> NameClass:
    """Strip role parentheticals, split coordinated names, and refuse non-person surfaces."""
    text = (raw or '').strip()
    if not text:
        return NameClass('reject', reason='empty')
    names, _aliases, _notes = split_chefs(text)
    if not names:
        return NameClass('reject', reason=_not_a_person(text) or 'unnamed_or_placeholder')
    cleaned = []
    for name in names:
        if MULTI_SEP.search(name):
            return NameClass('multiple', names=tuple(names), reason='coordinated_names')
        why = _not_a_person(name)
        if why:
            return NameClass('reject', reason=why)
        cleaned.append(name)
    if len(cleaned) > 1:
        return NameClass('multiple', names=tuple(cleaned), reason='coordinated_names')
    return NameClass('single', names=tuple(cleaned))


def _add(index: dict[str, set[tuple[str, str | None]]], key: str, chef_id: str, source: str):
    if key:
        index.setdefault(key, set()).add((chef_id, source))


class ChefIndex:
    """One-shot lookup table for a connection. Built from chefs already in the database."""

    def __init__(self, conn):
        self.by_surface: dict[str, set[tuple[str, str]]] = {}
        self.by_key: dict[str, set[tuple[str, str]]] = {}
        self.disambiguator: dict[str, str | None] = {}
        for row in conn.execute('SELECT chef_id, display_name, display_name_en, disambiguator FROM chef'):
            self.disambiguator[row['chef_id']] = row['disambiguator']
            self._index_spelling(row['display_name'], row['chef_id'], 'exact')
            self._index_spelling(row['display_name_en'], row['chef_id'], 'alias')
        for row in conn.execute("""SELECT entity_id, alias FROM alias
                                   WHERE entity_type='chef' AND kind<>'legacy_id'"""):
            self._index_spelling(row['alias'], row['entity_id'], 'alias')
        for row in conn.execute('SELECT name_key, chef_id FROM chef_name_key'):
            _add(self.by_key, row['name_key'], row['chef_id'], 'name_key')
        if conn.execute("SELECT 1 FROM sqlite_master WHERE name='extraction'").fetchone():
            for row in conn.execute(f"""SELECT e.chef_text, e.host, te.chef_id
                                        FROM extraction e
                                        JOIN target_entity te ON te.target_id=e.resolved_chef_target
                                        WHERE te.chef_id IS NOT NULL AND e.chef_text IS NOT NULL
                                          AND {GUIDE_HOST_SQL}"""):
                source = 'wikipedia' if 'wikipedia.org' in (row['host'] or '') else 'michelin'
                self._index_spelling(row['chef_text'], row['chef_id'], source)
        if conn.execute("SELECT 1 FROM sqlite_master WHERE name='profile_fact'").fetchone():
            for row in conn.execute("SELECT value FROM profile_fact WHERE field='chef'"):
                # Official stored spellings only attach to chefs already known by another key.
                for chef_id, _src in self._hits(row['value']):
                    self._index_spelling(row['value'], chef_id, 'official_profile')

    def _index_spelling(self, spelling: str | None, chef_id: str, source: str):
        if not spelling or chef_id not in self.disambiguator:
            return
        folded = norm(spelling)
        _add(self.by_surface, folded, chef_id, source)
        _add(self.by_surface, spelling.strip().casefold(), chef_id, source)
        for key in candidate_keys(spelling):
            _add(self.by_key, key, chef_id, 'name_key' if source in ('exact', 'alias', 'name_key') else source)

    def _hits(self, spelling: str) -> set[tuple[str, str]]:
        found: set[tuple[str, str]] = set()
        found.update(self.by_surface.get(norm(spelling), ()))
        found.update(self.by_surface.get(spelling.strip().casefold(), ()))
        for key in candidate_keys(spelling):
            found.update(self.by_key.get(key, ()))
        return found

    def match(self, raw: str | None, origin: str | None = None) -> ChefMatch:
        classified = classify_chef_name(raw)
        if classified.status == 'reject':
            return ChefMatch('reject', names=classified.names, reason=classified.reason)
        if classified.status == 'multiple':
            return ChefMatch('multiple', names=classified.names, reason=classified.reason)
        name = classified.names[0]
        hits = self._hits(name)
        ids = {chef_id for chef_id, _ in hits}
        if not ids:
            return ChefMatch('unknown', names=classified.names)
        if origin:
            scoped = {chef_id for chef_id in ids if self.disambiguator.get(chef_id) == origin}
            if not scoped:
                # Same name exists under a different kitchen: never guess.
                return ChefMatch('homonym', names=classified.names, reason='origin_mismatch')
            ids = scoped
        if len(ids) > 1:
            return ChefMatch('homonym', names=classified.names, reason='shared_name')
        chef_id = next(iter(ids))
        source = sorted({src for cid, src in hits if cid == chef_id}, key=_source_rank)[0]
        return ChefMatch('matched', chef_id=chef_id, names=classified.names, source=source)


def _source_rank(source: str) -> int:
    order = ('exact', 'alias', 'name_key', 'official_profile', 'michelin', 'wikipedia')
    return order.index(source) if source in order else len(order)


def match_chef(conn, raw: str | None, origin: str | None = None, index: ChefIndex | None = None) -> ChefMatch:
    return (index or ChefIndex(conn)).match(raw, origin)


def _human_hold(payload: dict) -> bool:
    review = payload.get('chatgpt_review') or payload.get('human_review') or {}
    applied = review.get('applied') or {}
    if applied.get('left_open') or applied.get('awaiting_chef_confirmation'):
        return True
    return review.get('decision') in HUMAN_HOLD_DECISIONS


def _annotate(payload: dict, decision: dict) -> str:
    body = dict(payload or {})
    body['deterministic_resolution'] = decision
    return json.dumps(body, ensure_ascii=False, sort_keys=True)


def _reject_candidate_targets(conn, name: str, now: str, reason: str) -> int:
    """Stop the gazetteer from re-matching a non-person as a chef on the next crawl."""
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='target'").fetchone():
        return 0
    n = 0
    for row in conn.execute("""SELECT target_id, attrs_json FROM target
                               WHERE kind='chef' AND status='candidate' AND name_ko=?""", (name,)):
        if conn.execute("SELECT 1 FROM sqlite_master WHERE name='target_entity'").fetchone():
            mapped = conn.execute('SELECT chef_id FROM target_entity WHERE target_id=?', (row['target_id'],)).fetchone()
            if mapped and mapped['chef_id']:
                continue
        attrs = json.loads(row['attrs_json'] or '{}')
        attrs.update(rejected_reason=reason, rejected_at=now, rejected_by='deterministic_chef_resolve')
        conn.execute("UPDATE target SET status='rejected', attrs_json=? WHERE target_id=?",
                     (json.dumps(attrs, ensure_ascii=False, sort_keys=True), row['target_id']))
        n += 1
    return n


def resolve_open_unknown_chefs(conn, now: str | None = None) -> dict:
    """Close open unknown_chef reviews that a deterministic match or non-person rule can settle."""
    now = now or utcnow()
    index = ChefIndex(conn)
    out = {'resolved': 0, 'dismissed': 0, 'held': 0, 'left_open': 0, 'targets_rejected': 0}
    rows = conn.execute("""SELECT review_id, subject_id, payload_json FROM review_queue
                           WHERE reason='unknown_chef' AND state='open' AND subject_type='candidate_fact'
                           ORDER BY rowid""").fetchall()
    for row in rows:
        payload = json.loads(row['payload_json'] or '{}')
        fact = payload.get('payload') or payload
        name = fact.get('chef_name')
        origin = fact.get('origin_kitchen')
        result = index.match(name, origin)
        if result.status == 'reject':
            decision = {'decision': 'dismissed', 'reason': result.reason, 'at': now}
            conn.execute("""UPDATE review_queue SET state='dismissed', resolved_at=?, payload_json=?
                            WHERE review_id=?""", (now, _annotate(payload, decision), row['review_id']))
            conn.execute("UPDATE candidate_fact SET resolution='rejected' WHERE candidate_id=?", (row['subject_id'],))
            out['targets_rejected'] += _reject_candidate_targets(conn, (name or '').strip(), now, result.reason or 'generic_name')
            out['dismissed'] += 1
            continue
        if result.status == 'matched' and _human_hold(payload):
            decision = {'decision': 'held', 'chef_id': result.chef_id, 'source': result.source, 'at': now,
                        'reason': 'human_review_left_open'}
            conn.execute('UPDATE review_queue SET payload_json=? WHERE review_id=?',
                         (_annotate(payload, decision), row['review_id']))
            out['held'] += 1
            continue
        if result.status == 'matched':
            decision = {'decision': 'resolved', 'chef_id': result.chef_id, 'source': result.source, 'at': now}
            conn.execute("""UPDATE review_queue SET state='resolved', resolved_at=?, payload_json=?
                            WHERE review_id=?""", (now, _annotate(payload, decision), row['review_id']))
            conn.execute("""UPDATE candidate_fact SET resolution='resolved', resolved_chef_id=?
                            WHERE candidate_id=?""", (result.chef_id, row['subject_id']))
            out['resolved'] += 1
            continue
        out['left_open'] += 1
    return out
