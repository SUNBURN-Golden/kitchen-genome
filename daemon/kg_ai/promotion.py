"""Turn retained AI approvals into quote-backed G3 career evidence."""
from collections import Counter, defaultdict, deque
import json
from pathlib import Path
import re
import sqlite3
from types import SimpleNamespace
import unicodedata

from kg_ai.article import digest
from kg_collect import evaluate, lineage, lineage_identity as identity, site_projection, site_publish
from kg_collect.net import host_of
from kg_collect.targets import Gazetteer, name_key
from kg_daemon.db import stable_id, utcnow

from .promotion_binding import (
    CONTEST, LABELS, OUTLET, _blocked_name, _endpoint_reason, _literals, _named_subject,
    _parse, _reject_reason, _sentences, _spellings, _venue_reason, _venue_tail_reason,
)

KEY = 'ai_promotion'
BADGE = 'AI 검토'
MEDIA_HOSTS = ('youtube.com', 'youtu.be', 'youtube-nocookie.com')


def annotations(seed):
    for kind in ('restaurants', 'kitchens'):
        for holder in seed.get(kind, ()):
            for entry in holder.get('ai_reviewed', ()):
                yield kind, holder, entry
    for entry in seed.get('ai_reviewed_unlinked', ()):
        yield None, None, entry


def audit_rows(path):
    if not Path(path).is_file():
        return
    with open(path, encoding='utf-8') as log:
        for line in log:
            try:
                yield json.loads(line)
            except ValueError:
                continue


def tombstones(log):
    return {r['review_id'] for r in audit_rows(log) if r.get('action') == 'rollback' and r.get('review_id')}


def _safe_source_url(url):
    if not site_publish.safe_url(url):
        return False
    host = host_of(url)
    return not any(host == domain or host.endswith('.' + domain) for domain in MEDIA_HOSTS)


def _normalized(text):
    from privacy_filter import sanitize_before_store

    return unicodedata.normalize('NFC', sanitize_before_store(text)[0] or '')


def _repeated_occurrences_agree(text, quote):
    """Repeated text is usable only when every occurrence has the same local context.

    Compare the clause before the quote (which carries the subject) and the
    immediate tail. A reposted listing can repeat the article verbatim while
    truncating or padding the remainder of the sentence differently.
    """
    spans = list(_sentences(text))
    seen = set()
    position = -1
    while (position := text.find(quote, position + 1)) >= 0:
        sentence = next(((s, e) for s, e in spans if s <= position and position + len(quote) <= e), None)
        if sentence is None:
            return False
        seen.add((text[sentence[0]:position], text[position + len(quote):sentence[1]][:80]))
    return len(seen) <= 1


def _context(raw, text, quote, details):
    """Map a raw extraction span through sanitization to the extracted occurrence."""
    start, end = details.get('span_start'), details.get('span_end')
    occurrences = text.count(quote)
    if occurrences == 1:
        offset = text.index(quote)
        if not isinstance(start, int) or not isinstance(end, int) or text[start:end] == quote:
            return text, offset
        # Map offsets from the raw fetch after whitespace, privacy and NFC edits.
        if (0 <= start < end <= len(raw) and _normalized(raw[start:end]) == quote
                and len(_normalized(raw[:start])) in (offset, offset - 1)):
            return text, offset
        return None
    if occurrences == 0:
        return None
    # A listing page can repeat the whole article verbatim. Reproduce the
    # occurrence the extraction span points at instead of rejecting every
    # review; a span that does not match this exact quote still abstains.
    if (not isinstance(start, int) or not isinstance(end, int)
            or not (0 <= start < end <= len(raw)) or raw[start:end] != quote
            or not _repeated_occurrences_agree(text, quote)):
        return None
    if raw.count(quote) == occurrences:
        # Sanitization kept the occurrence count, so the span's ordinal index
        # still identifies the same occurrence in the normalized text.
        offset = -1
        for _ in range(raw.count(quote, 0, start) + 1):
            offset = text.find(quote, offset + 1)
            if offset < 0:
                return None
        return text, offset
    offset = len(_normalized(raw[:start]))
    for candidate in (offset - 1, offset, offset + 1):
        if 0 <= candidate <= len(text) and text[candidate:candidate + len(quote)] == quote:
            return text, candidate
    return None


def _original(conn, entry, raw_dir):
    review_id = entry['review_id']
    record = conn.execute('SELECT * FROM review_queue WHERE review_id=?', (review_id,)).fetchone()
    details, text, links, tier = {}, None, (), None
    if record:
        approval = json.loads(record['payload_json'] or '{}').get('ai_review', {})
        if approval.get('rolled_back'):
            return None, 'tombstoned'
        if record['state'] != 'resolved' or approval.get('decision') != 'approve':
            return None, 'no_original'
        if entry.get('extraction_id') != record['subject_id']:
            return None, 'no_original'
        if record['subject_type'] == 'candidate_fact':
            original = conn.execute("""SELECT c.payload_json,c.evidence_json,c.document_id,d.raw_ref,d.canonical_url,
                d.content_hash,d.published_at,s.tier,s.enabled,p.collect_permission
                FROM candidate_fact c LEFT JOIN document d USING(document_id)
                LEFT JOIN source s ON s.source_id=d.source_id LEFT JOIN source_policy p ON p.source_id=s.source_id
                WHERE candidate_id=?""", (record['subject_id'],)).fetchone()
            if original:
                if original['enabled'] == 0 or original['collect_permission'] in ('manual_only', 'prohibited'):
                    return None, 'unsafe_source'
                tier = original['tier']
                locator = json.loads(original['evidence_json'])
                details = {**json.loads(original['payload_json']), 'quote': locator.get('quote'),
                           'canonical_url': original['canonical_url'] or locator.get('source_url'),
                           'published_at': original['published_at'], 'evidence_id': original['document_id'],
                           'evidence_hash': original['content_hash'],
                           'span_start': locator.get('span_start'), 'span_end': locator.get('span_end')}
                if original['raw_ref']:
                    try:
                        path = Path(original['raw_ref'])
                        if not path.is_absolute():
                            path = Path(raw_dir) / path
                        body = json.loads(path.read_text(encoding='utf-8'))
                        text = '\n'.join(dict.fromkeys(s for k in ('title', 'text', 'body', 'snippet')
                                                       if isinstance(s := body.get(k), str)))
                    except (OSError, ValueError, AttributeError):
                        pass
        elif record['subject_type'] == 'extraction':
            original = conn.execute("""SELECT e.*,f.text_ref,f.raw_ref,f.method,f.content_hash AS evidence_hash FROM extraction e
                LEFT JOIN fetch_record f USING(fetch_id) WHERE extraction_id=?""", (record['subject_id'],)).fetchone()
            if original:
                details = dict(original)
                details['evidence_id'] = details['fetch_id']
                if original['text_ref']:
                    try:
                        text = (Path(raw_dir) / original['text_ref']).read_text(encoding='utf-8')
                    except (OSError, UnicodeError):
                        pass
                profile = conn.execute('SELECT tier_guess FROM domain_profile WHERE host=?', (details['host'],)).fetchone()
                if profile and profile[0] in ('event_notice', 'social_lead'):
                    tier = profile[0]
                if original['raw_ref'] and original['method'] in ('html', 'browser', 'jsonld'):
                    from kg_collect.htmlx import parse_html
                    try:
                        links = parse_html((Path(raw_dir) / original['raw_ref']).read_text(encoding='utf-8', errors='replace'), entry['src'])['links']
                    except OSError:
                        pass
        if (not details or details.get('quote') != entry['quote'] or details.get('canonical_url') != entry['src']
                or details.get('label', entry['label']) != entry['label']):
            return None, 'no_original'
        for stated, saved in ((details.get('chef_text'), entry.get('chef')),
                              (details.get('restaurant_text') or details.get('org_text'), entry.get('restaurant'))):
            if stated and saved and not {name_key(n) for n in _spellings(stated)}.intersection(
                    map(name_key, _spellings(saved))):
                return None, 'no_original'
    if text is not None:
        raw, text = text, _normalized(text)
        if entry['quote'] not in text:
            return None, 'no_original'
        details['context'] = _context(raw, text, entry['quote'], details)
    doc = SimpleNamespace(url=entry['src'], canonical_url=entry['src'], links=links,
                          published_at=details.get('published_at') or entry.get('date'))
    metadata = lineage._source(conn, doc, text or entry['quote'], tier)
    if metadata is None:
        return None, 'unsafe_source'
    if text is None:
        metadata['independent_eligible'] = 0
    metadata['body_fingerprint'] = site_projection.retained_fingerprint(
        digest(shingle)[:12] for shingle in evaluate.shingles(text[:3000].casefold())) if text else '[]'
    metadata['quote_key'] = digest(name_key(entry['quote']))
    metadata['snapshot_checked'] = text is not None
    return (details, metadata), None


def _own(conn, kind, entity):
    conn.execute('INSERT OR IGNORE INTO ai_promotion_identity VALUES (?,?)', (kind, entity))


def _legacy_link(conn, branch, site_id, now):
    from kg_daemon.seed_migration import add_alias

    restaurant = conn.execute('SELECT restaurant_id FROM branch WHERE branch_id=?', (branch,)).fetchone()[0]
    alias = stable_id('als', 'restaurant', restaurant, site_id, 'legacy_id')
    if not conn.execute('SELECT 1 FROM alias WHERE alias_id=?', (alias,)).fetchone():
        add_alias(conn, 'restaurant', restaurant, site_id, 'legacy_id', now)
        _own(conn, 'alias', alias)


def _site_matches(seed, site, gaz, names):
    matches = {}
    keys = {name_key(n) for n in names}
    target_seeds = {json.loads(r['attrs_json']).get('seed_id') for n in names for r in gaz.lookup(n)
                    if r['kind'] == 'restaurant'}
    for name in names:
        matched = site.match({'restaurant_text': name})
        if matched:
            matches[matched[1]['id']] = ('restaurants' if matched[0] == 'restaurant' else 'kitchens', matched[1])
    for kind in ('restaurants', 'kitchens'):
        for holder in seed.get(kind, ()):
            aliases = (*_spellings(holder['name'], holder.get('name_en') or holder.get('en')),
                       *holder.get('prev_names', ()), *holder.get('aliases', ()))
            if holder['id'] in target_seeds or keys.intersection(map(name_key, aliases)):
                matches[holder['id']] = (kind, holder)
    return matches


def _chef_names(row):
    return _spellings(row.get('resolved_chef_text') or row['chef_text'], row.get('bilingual_name'))


def _branch(conn, seed, matches, row, names, gaz, support, now):
    redirect = identity.redirects(conn, 'branch')
    site, _ = site_projection._site_links(conn, seed, redirect)
    mapped = {branch for kind in site for branch, ids in site[kind].items() if set(ids) & matches.keys()}
    focused = mapped - {r[0] for r in conn.execute('SELECT branch_id FROM career_branch_context')}
    published = set().union(*(identity.branch_candidates(conn, n, gaz) for n in names), mapped)
    published = {identity.canonical(redirect, bid) for bid in published}
    for prev in support.values():
        known = {name_key(n) for n in _spellings(prev['venue_text'], prev.get('bilingual_venue'))}
        if known.intersection(map(name_key, names)):
            published.add(identity.canonical(redirect, prev['branch_id']))
    def compatible(branch):
        loc = conn.execute('SELECT city,country_code FROM branch WHERE branch_id=?', (branch,)).fetchone()
        return loc and all(not row[k] or not loc[k] or name_key(row[k]) == name_key(loc[k]) for k in ('city', 'country_code'))

    choices = {branch for name in names for branch, _ in [identity.resolve_branch(
        conn, name, row['quote'], gaz, focused, row['publisher_group'], row['src'], now,
        create=False, city=row['city'], country=row['country_code'])] if branch and compatible(branch)}
    for prev in support.values():
        bid = identity.canonical(redirect, prev['branch_id'])
        if bid not in published or not compatible(bid):
            continue
        # G3 scopes unknown-location names to their publisher. A city, explicit
        # same-article naming, or an attested bilingual spelling can bind another source.
        same_venue = {name_key(n) for n in names} & {name_key(n) for n in _spellings(
            prev['venue_text'], prev.get('bilingual_venue'))}
        bilingual_operator = (len(published) == 1 and (row.get('bilingual_venue') or prev.get('bilingual_venue'))
                              and {name_key(n) for n in _chef_names(row)}.intersection(map(name_key, _chef_names(prev))))
        if same_venue and (row['publisher_group'] == prev['publisher_group']
                or row['city'] and prev['city'] and name_key(row['city']) == name_key(prev['city'])
                or bilingual_operator):
            choices.add(bid)
    if len(choices) == 1 and (not mapped or choices <= mapped):
        return next(iter(choices)), site, None
    # Seed names which have no branch mapping yet can anchor a new branch only
    # when they point to one holder. Contests need evidence about the outlet.
    if published or len(matches) > 1:
        return None, site, 'branch_unresolved'
    if row['contest'] and not (matches or row['restaurant_wording']):
        return None, site, 'contest'
    label = next(iter(matches.values()))[1]['name'] if matches else names[0]
    holder = next(iter(matches.values()))[1] if matches else {}
    existing = {identity.canonical(redirect, bid) for bid in identity.branch_candidates(conn, label, gaz)}
    branch, _ = identity.resolve_branch(conn, label, row['quote'], gaz, set(),
                                       'seed:' + holder['id'] if holder else row['publisher_group'],
                                       row['src'], now, city=row['city'], country=row['country_code'])
    if branch:
        if branch not in existing:
            _own(conn, 'branch', branch)
            _own(conn, 'restaurant', conn.execute('SELECT restaurant_id FROM branch WHERE branch_id=?', (branch,)).fetchone()[0])
        if holder:
            conn.execute('DELETE FROM career_branch_context WHERE branch_id=?', (branch,))
            _legacy_link(conn, branch, holder['id'], now)
            site[next(iter(matches.values()))[0]][branch].append(holder['id'])
    return branch, site, None if branch else 'branch_unresolved'


def _chef(conn, branch, row, gaz, supports, now, context):
    name = row.get('resolved_chef_text') or row['chef_text']
    names = _chef_names(row)
    redirects = identity.redirects(conn, 'chef')
    candidates = {identity.canonical(redirects, cid) for cid in identity.chef_candidates(conn, names, gaz)}
    keys = set().union(*(identity.candidate_keys(n) for n in names))
    previous = {identity.canonical(redirects, prior['chef_id']) for prior in supports.values()
                if (prior['branch_id'] == branch or prior['canonical_url'] == row['canonical_url']) and keys.intersection(
                    set().union(*(identity.candidate_keys(n) for n in _chef_names(prior))))}
    if len(previous) == 1 and not candidates - previous:
        return next(iter(previous))
    local = {identity.canonical(redirects, r[0]) for r in conn.execute(
        "SELECT chef_id FROM employment WHERE branch_id=? AND state<>'rejected'", (branch,))}
    if len(local & candidates) == 1:
        return next(iter(local & candidates))
    # An attested affiliation to a curated bilingual chef can disambiguate an
    # opening elsewhere. Limit the search to the same local source passage.
    if context and candidates:
        text, position = context
        passage = text[max(0, position - 1800):position + len(row['quote']) + 190]
        matched = set()
        for candidate in candidates:
            for chef_ko, chef_en, venue, venue_en in conn.execute("""
                SELECT c.display_name,c.display_name_en,r.name,r.name_en
                FROM chef c JOIN employment e USING(chef_id) JOIN branch b USING(branch_id)
                JOIN restaurant r USING(restaurant_id)
                WHERE c.chef_id=? AND e.state<>'rejected' AND c.display_name_en IS NOT NULL
                """, (candidate,)):
                if not {name_key(chef_ko), name_key(chef_en)}.intersection(map(name_key, names)):
                    continue
                # Guide pages can state the chef in a labeled restaurant header.
                if venue_en and re.search(r'\b' + re.escape(venue_en)
                                          + r'\b[^.!?]{0,100}\bOn the Pass\s+' + re.escape(chef_en)
                                          + r'(?!\w)', passage, re.I):
                    matched.add(candidate)
                for start, end in _sentences(passage):
                    clause = passage[start:end]
                    if (start <= min(position, 1800) + len(row['quote']) and end >= min(position, 1800) - 250
                            and any(_named_subject(clause, person) for person in (chef_ko, chef_en))) and any(
                            any(_literals(outlet, clause)) for outlet in (venue, venue_en) if outlet):
                        matched.add(candidate)
        if len(matched) == 1:
            return matched.pop()
    if len(candidates | previous) > 1:
        return None
    if len(candidates) == 1 and row.get('binding_receipt', {}).get('rule') == 'source_roster':
        candidate = next(iter(candidates))
        # Reuse a name-only registry entry when the approved roster supplies
        # its first affiliation. Established identities still need local proof.
        if conn.execute("""SELECT 1 FROM chef c WHERE c.chef_id=? AND c.display_name=? COLLATE NOCASE
                AND NOT EXISTS (SELECT 1 FROM employment e WHERE e.chef_id=c.chef_id AND e.state<>'rejected')
                AND NOT EXISTS (SELECT 1 FROM career_assertion a WHERE a.chef_id=c.chef_id)""",
                        (candidate, name)).fetchone():
            return candidate
    exact = stable_id('chf', 'career', name_key(name), branch)
    if candidates == {exact} and conn.execute(
            "SELECT 1 FROM ai_promotion_identity WHERE kind='chef' AND entity_id=?", (exact,)).fetchone():
        return exact
    # The G3 resolver can also use a dated native stint to disambiguate.
    # Bilingual evidence is retained on this support. There is no need to
    # redirect another identity to publish it.
    chef, plausible = identity.resolve_chef(conn, {**row, 'chef_text': name, 'bilingual_name': None}, branch, gaz, row, None, now,
                                            allow_redirect=False)
    if chef and not plausible:
        _own(conn, 'chef', chef)
    if not chef and row.get('bilingual_name') and len(candidates) == 1:
        cid = next(iter(candidates))
        display = conn.execute('SELECT display_name,display_name_en FROM chef WHERE chef_id=?', (cid,)).fetchone()
        if {name_key(n) for n in names}.intersection(name_key(n) for n in display if n):
            return cid
    return chef


def _new_holder(conn, seed, state, kind, branch, now, *, alias=None):
    row = conn.execute('SELECT r.name,r.name_en,b.city,b.country_code FROM branch b JOIN restaurant r USING(restaurant_id) WHERE branch_id=?',
                       (branch,)).fetchone()
    reserved = {r['id'] for key in ('restaurants', 'kitchens') for r in seed[key]}
    site_id = site_projection._stable_site_id(conn, kind[:-1], branch, (), reserved)
    holder = {'id': site_id, 'name': row['name']}
    en = next((n for n in _spellings(row['name'], alias or row['name_en']) if not re.search('[가-힣]', n)), None)
    if en:
        holder['name_en'] = en
    if kind == 'restaurants':
        holder['type'] = 'established'
    state.setdefault('holders', {})[site_id] = {'kind': kind, 'branch': branch, 'fields': dict(holder)}
    seed[kind].append(holder)
    _legacy_link(conn, branch, site_id, now)
    return holder


def _move(seed, before, holder, entry):
    source = before.get('ai_reviewed', []) if before is not None else seed.get('ai_reviewed_unlinked', [])
    if before is holder or entry not in source:
        return
    target = holder.setdefault('ai_reviewed', [])
    if not any(r.get('review_id') == entry['review_id'] for r in target):
        target.append(entry)
    source.remove(entry)
    if not source:
        if before is None:
            seed.pop('ai_reviewed_unlinked', None)
        else:
            before.pop('ai_reviewed', None)


def _identifier(value):
    return value if isinstance(value, str) and value and value == value.strip() else None


def _eligibility(entry, denied, blocked):
    rid = _identifier(entry.get('review_id'))
    venue = entry.get('restaurant')
    if rid in denied or isinstance(venue, str) and _blocked_name(_spellings(venue), blocked):
        return 'tombstoned'
    if (not rid or not _identifier(entry.get('extraction_id'))
            or entry.get('badge') != BADGE or entry.get('provenance') != 'ai_reviewed'):
        return 'invalid_annotation'
    if not isinstance(entry.get('label'), str) or entry['label'] not in LABELS:
        return 'unsupported_label'
    if not _safe_source_url(entry.get('src')):
        return 'unsafe_source'
    if not isinstance(entry.get('quote'), str) or not site_publish._public_quote(entry['quote']):
        return 'unsafe_quote'
    return _endpoint_reason(entry.get('chef'), venue, entry['quote'])


def _write_skips(conn, entries, reasons):
    conn.execute('DELETE FROM ai_promotion_skip')
    rows = []
    for i, entry in enumerate(entries):
        rid, xid, quote_key = _skip_fingerprint(entry)
        rows.append((i, rid, reasons[id(entry)], xid, quote_key))
    conn.executemany('INSERT INTO ai_promotion_skip VALUES (?,?,?,?,?)', rows)


def _skip_fingerprint(entry):
    quote = entry.get('quote')
    return (_identifier(entry.get('review_id')), _identifier(entry.get('extraction_id')),
            digest(name_key(quote)) if isinstance(quote, str) else None)


def skip_histogram(conn):
    return dict(conn.execute('SELECT reason,COUNT(*) FROM ai_promotion_skip GROUP BY reason ORDER BY reason'))


def _venue_tombstones(seed):
    return {name_key(n) for tomb in seed.get(site_publish.TOMBSTONES, ())
            for n in (*tomb.get('aliases', ()), tomb.get('creation_event', {}).get('name') or '')}


def reconcile_withdrawal_skips(conn, previous, seed, raw_dir, log):
    """Reindex unchanged skip decisions after rollback shifts the backlog."""
    known = defaultdict(deque)
    old = previous.get('ai_reviewed_unlinked', ())
    for i, rid, reason, xid, quote_key in conn.execute('SELECT * FROM ai_promotion_skip ORDER BY unlinked_index'):
        if i < len(old) and _skip_fingerprint(old[i]) == (rid, xid, quote_key):
            known[json.dumps(old[i], sort_keys=True)].append(reason)
    denied = tombstones(log) | set(seed.get(KEY, {}).get('withdrawn', ()))
    blocked = _venue_tombstones(seed)
    entries = seed.get('ai_reviewed_unlinked', ())
    reasons = {}
    for entry in entries:
        prior = known.get(json.dumps(entry, sort_keys=True))
        reason = _eligibility(entry, denied, blocked)
        if reason != 'tombstoned' and prior:
            reason = prior.popleft()
        if not reason:
            original, reason = _original(conn, entry, raw_dir)
            if original:
                _, reason = _parse(entry, original[0])
        # An otherwise valid quote can return to the backlog when rollback
        # removes the only proof that supported its holder.
        reasons[id(entry)] = reason or 'branch_unresolved'
    _write_skips(conn, entries, reasons)


def _retained_binding(old, entry, details, source):
    receipt = old.get('binding_receipt')
    if (not receipt or source['snapshot_checked'] or not old.get('snapshot_checked')
            or not old.get('content_hash') or not details.get('evidence_id')
            or receipt['subject_id'] != entry['extraction_id'] or receipt['evidence_id'] != details['evidence_id']
            or receipt.get('evidence_hash') != details.get('evidence_hash')
            or receipt.get('chef_text') != details.get('chef_text')
            or any(old.get(key) != entry[annotation] for key, annotation in (
                ('quote', 'quote'), ('src', 'src'), ('chef_text', 'chef'), ('venue_text', 'restaurant')))
            or old['relation'] not in LABELS[entry['label']]):
        return None
    return {**old, 'contest': bool(CONTEST.search(entry['quote'])), 'restaurant_wording': bool(OUTLET.search(entry['quote']))}


def _promote(conn, seed, raw_dir, log, now):
    from .review import Site

    state = seed.get(KEY, {})
    supports = state.get('supports', {})
    denied = tombstones(log) | set(state.get('withdrawn', ()))
    gaz = Gazetteer(conn)
    entries = sorted(annotations(seed), key=lambda entry: entry[1] is None)
    touched, reasons, seen = {}, {}, set()
    blocked = _venue_tombstones(seed)
    invalid = set(supports) - {_identifier(entry.get('review_id')) for _, _, entry in entries}
    invalid.update(set(supports) & denied)
    for _, before, entry in entries:
        rid = _identifier(entry.get('review_id'))
        reason = _eligibility(entry, denied, blocked)
        if reason != 'tombstoned' and rid in seen:
            dest, first = touched.get(rid, (None, None))
            if dest is not None and first == entry:
                _move(seed, before, dest, entry)
            else:
                reasons[id(entry)] = 'duplicate_review_id'
            continue
        if rid:
            seen.add(rid)
        if reason:
            reasons[id(entry)] = reason
            invalid.add(rid)
            continue
        original, reason = _original(conn, entry, raw_dir)
        if reason:
            reasons[id(entry)] = reason
            invalid.add(rid)
            continue
        details, source = original
        old = supports.get(rid, {})
        row, reason = _parse(entry, details)
        if reason in ('no_original', 'no_relation', 'no_clause', 'chef_unresolved'):
            row = _retained_binding(old, entry, details, source)
            if row:
                reason = None
        if reason:
            reasons[id(entry)] = reason
            invalid.add(rid)
            continue
        row.update(source, src=entry['src'], assertion_id=rid, ai_reviewed=True)
        # Text retention must not lower a previously checked proof's quality.
        if old and old['quote'] == row['quote'] and old['src'] == row['src'] and not row['snapshot_checked']:
            for key in ('independent_eligible', 'content_hash', 'body_fingerprint', 'publisher_group', 'origin_group', 'snapshot_checked'):
                row[key] = old[key]
        names = _spellings(row['venue_text'], row.get('bilingual_venue'))
        excluded = ('tombstoned' if _blocked_name(names, blocked) else
                    _venue_reason(row['venue_text'], row.get('bilingual_venue')))
        if excluded:
            reasons[id(entry)] = excluded
            invalid.add(rid)
            continue
        site = Site(seed)
        matches = _site_matches(seed, site, gaz, names)
        conn.execute('SAVEPOINT promote_review')
        branch, by_kind, branch_reason = _branch(conn, seed, matches, row, names, gaz, supports, now)
        chef = _chef(conn, branch, row, gaz, supports, now, details.get('context')) if branch else None
        if not chef:
            reasons[id(entry)] = branch_reason or 'chef_unresolved'
            conn.execute('ROLLBACK TO promote_review')
            conn.execute('RELEASE promote_review')
            continue
        conn.execute('RELEASE promote_review')
        row.update(branch_id=branch, chef_id=chef)
        for field in ('contest', 'restaurant_wording'):
            row.pop(field)
        if row.get('mentor_text'):
            ids = identity.chef_candidates(conn, [row['mentor_text']], gaz) - {chef}
            if len(ids) == 1:
                row['mentor_id'] = next(iter(ids))
        rid_ids = by_kind['restaurants'][branch]
        kid_ids = by_kind['kitchens'][branch]
        preferred = rid_ids or kid_ids
        if preferred:
            holders = {r['id']: r for r in (*seed['restaurants'], *seed['kitchens'])}
            dest = before if before is not None and before['id'] in preferred else holders[preferred[0]]
        else:
            dest = _new_holder(conn, seed, state, 'restaurants', branch, now, alias=row.get('bilingual_venue'))
        row['holder'] = dest['id']
        supports[rid] = row
        touched[rid] = dest, entry
        reasons[id(entry)] = 'branch_unresolved'  # A later withdrawal can remove the holder.
        _move(seed, before, dest, entry)
    if supports:
        state['supports'] = supports
        seed[KEY] = state
    discarded = (invalid - touched.keys()) & supports.keys()
    if discarded:
        withdraw(seed, discarded, now, tombstone=False)
        prune_identities(conn, seed)
    # Venue-only attachment can deliver an eligible annotation without a graph edge.
    if state:
        site = Site(seed)
        for entry in list(seed.get('ai_reviewed_unlinked', ())):
            reason = reasons[id(entry)]
            if reason not in {'unsupported_label', 'no_chef', 'no_clause', 'no_relation',
                              'chef_unresolved', 'branch_unresolved'}:
                continue
            # A tentative or unbound opening/career sentence is not evidence
            # that this chef runs the venue; keep it in the backlog.
            if reason == 'no_relation' and entry.get('label') in ('opening', 'career'):
                continue
            venue = entry.get('restaurant')
            if (not isinstance(venue, str) or _venue_reason(venue)
                    or not _safe_source_url(entry.get('src'))
                    or not isinstance(entry.get('quote'), str) or not site_publish._public_quote(entry['quote'])
                    or _reject_reason(entry['quote'], lineage.language(entry['quote']))):
                continue
            matches = _site_matches(seed, site, gaz, _spellings(venue))
            if len(matches) == 1:
                _, source_reason = _original(conn, entry, raw_dir)
                if not source_reason:
                    _move(seed, None, next(iter(matches.values()))[1], entry)
                elif source_reason == 'tombstoned':
                    reasons[id(entry)] = source_reason
    _write_skips(conn, seed.get('ai_reviewed_unlinked', ()), reasons)


def prune_identities(conn, seed):
    """Delete unused AI identities. Foreign-key owners keep shared identities."""
    state = seed.get(KEY, {})
    supports = state.get('supports', {}).values()
    chef_redirects = identity.redirects(conn, 'chef')
    branch_redirects = identity.redirects(conn, 'branch')
    chefs = {r[k] for r in supports for k in ('chef_id', 'mentor_id') if r[k]}
    branches = {r[k] for r in supports for k in ('branch_id', 'previous_branch_id') if r[k]}
    branches.update(r['branch'] for r in state.get('holders', {}).values())
    kept_ids = {r['id'] for key in ('restaurants', 'kitchens') for r in seed[key]}
    wanted = {'chef': chefs | set(chef_redirects) | set(chef_redirects.values()),
              'branch': branches | set(branch_redirects) | set(branch_redirects.values()),
              'restaurant': set()}
    for kind in ('chef', 'branch', 'restaurant'):
        for eid, in conn.execute('SELECT entity_id FROM ai_promotion_identity WHERE kind=? ORDER BY entity_id', (kind,)).fetchall():
            if eid in wanted[kind]:
                continue
            if kind == 'branch' and any(a[0] in kept_ids for a in conn.execute(
                    """SELECT a.alias FROM alias a JOIN branch b ON a.entity_id=b.restaurant_id
                       WHERE a.kind='legacy_id' AND a.entity_type='restaurant' AND b.branch_id=?""", (eid,))):
                continue
            conn.execute('SAVEPOINT prune_promotion')
            try:
                if kind == 'chef':
                    conn.execute('DELETE FROM chef_name_key WHERE chef_id=?', (eid,))
                elif kind == 'branch':
                    conn.execute('DELETE FROM projection_site_id WHERE branch_id=?', (eid,))
                    conn.execute('DELETE FROM career_branch_context WHERE branch_id=?', (eid,))
                if conn.execute("""SELECT 1 FROM alias a LEFT JOIN ai_promotion_identity i
                    ON i.kind='alias' AND i.entity_id=a.alias_id WHERE a.entity_type=? AND a.entity_id=?
                    AND (i.entity_id IS NULL OR a.claim_id IS NOT NULL)""", (kind, eid)).fetchone():
                    conn.execute('ROLLBACK TO prune_promotion')
                    conn.execute('RELEASE prune_promotion')
                    continue
                conn.execute(f'DELETE FROM {kind} WHERE {kind}_id=?', (eid,))
                conn.execute('DELETE FROM ai_promotion_identity WHERE kind=? AND entity_id=?', (kind, eid))
            except sqlite3.IntegrityError:
                conn.execute('ROLLBACK TO prune_promotion')
            conn.execute('RELEASE prune_promotion')

    for alias_id, site_id in conn.execute("""SELECT a.alias_id,a.alias FROM alias a JOIN ai_promotion_identity i
        ON i.kind='alias' AND i.entity_id=a.alias_id WHERE a.kind='legacy_id' AND a.claim_id IS NULL""").fetchall():
        if site_id not in kept_ids:
            conn.execute('DELETE FROM alias WHERE alias_id=?', (alias_id,))
            conn.execute("DELETE FROM ai_promotion_identity WHERE kind='alias' AND entity_id=?", (alias_id,))


def ensure_origin_holders(conn, seed, snapshot, state):
    _, _, _, paths, _, reviewed = site_projection.graph(seed, snapshot)
    for destination, origin, _ in sorted(paths):
        if not {destination, origin} & reviewed or snapshot['site']['kitchens'].get(origin):
            continue
        holder = _new_holder(conn, seed, state, 'kitchens', origin, utcnow())
        snapshot['site']['kitchens'][origin] = [holder['id']]
        snapshot['site_ids']['kitchen'][origin] = holder['id']
        snapshot['aliases'] = [dict(r) for r in conn.execute(
            'SELECT branch_id,kind,site_id FROM projection_site_id ORDER BY kind,site_id')]



def refresh_holder_fields(seed, snapshot, branches):
    state = seed.get(KEY, {})
    curated = {h['id']: h for kind in ('restaurants', 'kitchens') for h in seed[kind]}
    for site_id, owner in state.get('holders', {}).items():
        holder = curated.get(site_id)
        branch = identity.canonical(snapshot['branch_redirects'], owner['branch'])
        if holder is None or branch not in branches:
            continue
        details = branches[branch]
        fields = owner['fields']
        for field, value in (('city', details['city']), ('country', details['country_code'])):
            if holder.get(field) != fields.get(field):
                continue
            if value:
                holder[field] = fields[field] = value
            else:
                holder.pop(field, None)
                fields.pop(field, None)


def _legacy_pair(seed, snapshot, branch, row):
    from kg_collect.targets import chef_names

    names = set().union(*(identity.candidate_keys(n) for n in _chef_names(row)))
    site = snapshot['site']
    restaurants = set(site['restaurants'].get(branch, ()))
    kitchens = set(site['kitchens'].get(branch, ()))
    for holder in seed['restaurants']:
        if not ((holder['id'] in restaurants and row['relation'] in ('opening', 'current')) or (
                holder.get('kitchen') in kitchens and row['relation'] in site_projection.PAST)):
            continue
        curated_names = [n for pair in chef_names(holder.get('chef'), holder.get('chef_en')) for n in pair if n]
        if not curated_names:
            curated_names = [n for n in (holder.get('chef'), holder.get('chef_en')) if n and '미기재' not in n]
        if any(names & identity.candidate_keys(n) for n in curated_names):
            return 'verified' if holder.get('lineage_status') == 'verified' else 'checking'
    return None


def record_links(seed, now):
    state = seed[KEY]
    snapshot = state.get('snapshot')
    if not snapshot:
        return 0
    _, _, _, _, affiliations, _ = site_projection.graph(seed, snapshot)
    native_seed = {**seed, KEY: {}}
    _, _, _, native_paths, native_jobs, _ = site_projection.graph(native_seed, snapshot)
    prior = state.get('links', {})
    active = {}
    new = 0
    day = site_publish.instant(now).date().isoformat()
    for (branch, chef), rows in sorted(affiliations.items()):
        for group in site_projection._stint_groups(r for r in rows if not r.get('seed_anchor')):
            reviewed = {r['assertion_id']: r for r in group if r.get('ai_reviewed')}
            if not reviewed:
                continue
            ids = sorted(reviewed)
            best = group[0]
            candidates = [(key, old) for key, old in prior.items() if key not in active
                          and identity.canonical(snapshot['chef_redirects'], old['chef']) == chef
                          and identity.canonical(snapshot['branch_redirects'], old['branch']) == branch
                          and site_projection._compatible(old, best)]
            overlap = [(key, old) for key, old in candidates if set(old['reviews']).intersection(ids)]
            choices = overlap or candidates
            key, old = choices[0] if len(choices) == 1 else (stable_id('ai-link', ids[0]), {})
            gate = 'verified' if site_projection._verified(group) else 'checking'
            record = {k: best[k] for k in ('relation', 'role_id', 'valid_from', 'valid_to')}
            record.update(branch=branch, chef=chef, reviews=ids, gate=gate, holder=reviewed[ids[0]]['holder'],
                          sources=sorted({r['src'] for r in group}))
            legacy = _legacy_pair(seed, snapshot, branch, reviewed[ids[0]])
            represented = list(native_jobs.get((branch, chef), ()))
            represented.extend(r for (_, employer, cid), career in native_paths.items()
                               if employer == branch and cid == chef for r in career)
            native = [r for r in represented if not r.get('seed_anchor') and site_projection._compatible(r, best)]
            previous_gate = old.get('gate') or ('verified' if site_projection._verified(native) else 'checking' if native else legacy)
            for field in ('created', 'upgraded'):
                if field in old:
                    record[field] = old[field]
            if previous_gate is None:
                record['created'] = day
                new += 1
            elif gate == 'verified' and previous_gate == 'checking':
                record['upgraded'] = day
            elif gate == 'checking':
                record.pop('upgraded', None)
            active[key] = record
    if active:
        state['links'] = active
    else:
        state.pop('links', None)
    return new


def events(seed, audit):
    """Reconcile both review passes against the publication ledger."""
    known = {row['event_id']: row for row in audit if row.get('event_id')}
    holders = {r['id']: r for key in ('restaurants', 'kitchens') for r in seed.get(key, ())}
    result = []
    for key, link in sorted(seed.get(KEY, {}).get('links', {}).items()):
        create_id = key + ':create'
        create = link.get('created') and create_id not in known
        prior_create = known.get(create_id, {})
        if create:
            kind, day, event_id = 'new_alumni', link['created'], create_id
        elif link.get('upgraded') and link['gate'] == 'verified' and prior_create.get('gate') != 'verified':
            # Suffix the upgrade day so a re-upgrade after a downgrade is counted
            # again; same-day retries still dedupe against the applied ledger.
            kind, day, event_id = 'upgrade_alumni', link['upgraded'], key + ':verify:' + link['upgraded']
        else:
            continue
        if event_id not in known and link['holder'] in holders:
            result.append(dict(id=link['holder'], name=holders[link['holder']]['name'], kind=kind,
                               applied_at=day, day_kst=day, event_id=event_id, gate=link['gate'], sources=link['sources']))
    return result


def backfill(conn, seed, raw_dir, log, now):
    """Promote saved approvals using only local data. Caller owns the write transaction."""
    _promote(conn, seed, raw_dir, log, now)
    state = seed.get(KEY, {})
    if state.get('withdrawn'):
        prune_identities(conn, seed)
    if not state.get('supports'):
        return 0
    site_projection.refresh(conn, seed)
    return record_links(seed, now)


def withdraw(seed, ids, now, *, tombstone=True):
    """Recompute from the cached native inputs for seed-only rollbacks."""
    state = seed.setdefault(KEY, {})
    if tombstone:
        state['withdrawn'] = sorted(set(state.get('withdrawn', ())) | ids)
    supports = state.get('supports', {})
    for review_id in ids:
        supports.pop(review_id, None)
    snapshot = state.get('snapshot')
    if not snapshot:
        return
    _, _, _, paths, _, reviewed = site_projection.graph(seed, snapshot)
    native = {r['branch_id'] for r in snapshot['careers']}
    restaurants = reviewed | native | {b for dest, source, _ in paths for b in (dest, source)}
    # The snapshot can predate branches created earlier in the same pass.
    restaurants.update(identity.canonical(snapshot['branch_redirects'], row['branch_id']) for row in supports.values())
    kitchens = {source for _, source, _ in paths}
    retained = {'restaurants': restaurants, 'kitchens': kitchens}
    for sid, owner in list(state.get('holders', {}).items()):
        kind, branch = owner['kind'], owner['branch']
        if identity.canonical(snapshot['branch_redirects'], branch) in retained[kind]:
            continue
        holder = next((r for r in seed[kind] if r['id'] == sid), None)
        if holder and {k: v for k, v in holder.items() if k != 'ai_reviewed' and k not in site_publish.ENRICHMENT} == {
                k: v for k, v in owner['fields'].items() if k not in site_publish.ENRICHMENT}:
            seed[kind].remove(holder)
            for entry in holder.get('ai_reviewed', ()):
                if not tombstone or entry.get('review_id') not in ids:
                    seed.setdefault('ai_reviewed_unlinked', []).append(entry)
        state['holders'].pop(sid)
    seed[site_projection.KEY] = site_projection.compose(seed, snapshot)
    record_links(seed, now)
    if not supports:
        state.pop('snapshot', None)
        state.pop('supports', None)
    if not state.get('holders'):
        state.pop('holders', None)
