"""Source-backed publication bundles and the seed publication transaction."""
import contextlib
import datetime as dt
from difflib import SequenceMatcher
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile
import unicodedata
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from kg_ai import pending
from kg_daemon import claims
from kg_daemon.db import ROOT, connect, stable_id

from . import EXTRACTOR_VERSION, discover, evaluate, extract, official
from .fetch import canonicalize_url
from .htmlx import parse_html
from .net import CFG, Response, host_of, is_banned, is_search_host
from .rawstore import RawStore
from .resolve import resolve_chef, resolve_restaurant
from .review import NAME_STOP, reject_reason
from .targets import Gazetteer, chef_names, norm, tid

if os.path.join(ROOT, 'crawler') not in sys.path:
    sys.path.insert(0, os.path.join(ROOT, 'crawler'))
from privacy_filter import sanitize_before_store  # noqa: E402

REASON = 'entity_publication'
SUBJECT = 'publication_bundle'
TOMBSTONES = 'publication_rollbacks'
KST = ZoneInfo('Asia/Seoul')
EDGES = ('lineage', 'affiliation', 'location')
UNTRUSTED = ('xportsnews', 'sports.khan', 'sports.donga', 'celebinfluencernews', 'wikitree',
             'wikipedia.org', 'facebook.com', 'msn.com', 'tiktok.com', 'twitter.com', 'x.com', 'linkedin.com')
CITIES = {
    '서울': r'서울(?:특별시)?(?=\s|에서|에|[,.·]|$)|\bSeoul\b',
    '부산': r'부산(?:광역시)?(?=\s|에서|에|[,.·]|$)|\bBusan\b',
    '울산': r'울산(?:광역시)?(?=\s|에서|에|[,.·]|$)|\bUlsan\b',
    '대구': r'대구(?:광역시)?(?=\s|에서|에|[,.·]|$)|\bDaegu\b',
    '인천': r'인천(?:광역시)?(?=\s|에서|에|[,.·]|$)|\bIncheon\b',
    '광주': r'광주광역시|\bGwangju Metropolitan City\b',
    '대전': r'대전(?:광역시)?(?=\s|에서|에|[,.·]|$)|\bDaejeon\b',
    '세종': r'세종특별자치시|\bSejong City\b',
    '제주': r'제주시|\bJeju City\b',
    '성남': r'성남시|\bSeongnam\b',
    '수원': r'수원시|\bSuwon\b',
    '뉴욕': r'뉴욕(?:시)?(?=\s|에서|에|[,.·]|$)|\b(?:New York City|NYC|Manhattan|Brooklyn)\b',
    '도쿄': r'도쿄(?=\s|에서|에|[,.·]|$)|\bTokyo\b',
}
CITY_PATTERNS = {city: re.compile(pattern, re.I) for city, pattern in CITIES.items()}
KOREAN_CITY = re.compile(r'(?:경기(?:도)?|강원(?:특별자치)?도|충청[남북]도|전라[남북]도|경상[남북]도)\s+([가-힣]{2,5})시\b')
SEOUL_ADDRESS = re.compile(r'(?:강남|서초|송파|강동|광진|성동|중랑|동대문|성북|강북|도봉|노원|은평|서대문|마포|양천|영등포|동작|관악|금천|구로|용산|종로)구\s+[가-힣0-9]+(?:로|길)\s+\d')
PRESS_CUE = re.compile(r'셰프|쉐프|레스토랑|식당|다이닝|\b(?:chef|restaurant|bistro)\b', re.I)
ENRICHMENT = {'name_en', 'name_en_official', 'name_en_type', 'name_en_src', 'chef_en', 'area_en', 'cuisine_en',
              'chef_en_src', 'chef_en_official', 'chef_en_type', 'role_en'}


def instant(now=None):
    if isinstance(now, str):
        now = dt.datetime.fromisoformat(now.replace('Z', '+00:00'))
    now = now or dt.datetime.now(dt.timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=dt.timezone.utc)
    return now.astimezone(KST)


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def safe_url(url):
    if not isinstance(url, str) or re.search(r'[\s\x00-\x1f\x7f\\#]', url):
        return None
    try:
        parts = urlsplit(url)
        if parts.scheme in ('http', 'https') and parts.hostname and parts.username is None and parts.port in (None, 80, 443):
            return url
    except ValueError:
        pass
    return None


def _under(host, domain):
    return host == domain or host.endswith('.' + domain)


def _date(value):
    try:
        return dt.date.fromisoformat((value or '')[:10])
    except ValueError:
        return None


def _recent(value, now):
    day = _date(value)
    today = now.date()
    return day is not None and (today.year - 2, today.month, today.day) <= (day.year, day.month, day.day) <= (today.year, today.month, today.day)


def _normalized(text):
    return re.sub(r'\s+', ' ', unicodedata.normalize('NFC', text)).strip()


def _public_quote(quote):
    if not quote or len(quote) > 200 or re.search(r'\[(?:REDACTED_|DATA_LITERAL_)', quote):
        return False
    _, flags = sanitize_before_store(quote)
    return not (flags['pii_redacted'] or flags['prompt_injection_detected'])


def _chef_ok(target):
    name = target['name_ko'] or target['name_en']
    if not name or norm(name) in NAME_STOP:
        return False
    return target['origin'] == 'seed' or (extract.clean_name(name) == name and not reject_reason('chef', name))


def _venue_ok(target):
    name = target['name_ko'] or target['name_en']
    if not name or norm(name) in NAME_STOP:
        return False
    return target['origin'] == 'seed' or (extract.clean_venue(name) == name and not reject_reason('restaurant', name))


class Corpus:
    """Join current rule rows to successful publisher snapshots and verify their spans."""

    def __init__(self, conn, raw, seeds=None, now=None):
        self.conn, self.raw, self.now = conn, raw, instant(now)
        self.seeds = seeds or discover.load_seeds()
        self.policy = self.seeds.get('publication', {}).get('press_hosts', {})
        self.gaz = Gazetteer(conn)
        self.targets = {t['target_id']: dict(t) for t in conn.execute("SELECT * FROM target WHERE status<>'rejected'")}
        self.aliases = {}
        for a in conn.execute('SELECT target_id,alias FROM target_alias'):
            self.aliases.setdefault(a['target_id'], set()).add(norm(a['alias']))
        self.blocked = {r[0] for r in conn.execute(
            "SELECT host FROM domain_profile WHERE state='blocked' UNION SELECT host FROM web_host_guard WHERE paused LIKE 'blocked:%'"
        )}
        self.disabled = {host_of('https://' + r[0]) for r in conn.execute("""SELECT DISTINCT host FROM source s
            LEFT JOIN source_policy p USING(source_id) WHERE host IS NOT NULL
            AND (s.enabled=0 OR p.collect_permission IN ('manual_only','prohibited'))""")}
        self.denied = tuple(CFG['unreliable_hosts']) + tuple(CFG['banned_hosts'])
        self.ignored = {canonicalize_url(u) for u in CFG.get('ignored_urls', [])}
        self.denied_domains = (*UNTRUSTED, *self.seeds.get('official_chain', {}).get('sns_hosts', []),
                               *self.seeds.get('official_chain', {}).get('booking_hosts', []))
        self.snapshots = {}
        self.proofs = None
        self.identity_conflicts = []

    def policy_for(self, row, subject):
        url, original = row['src'], row['fetch_url']
        if not safe_url(url) or not safe_url(original) or canonicalize_url(original) in self.ignored:
            return None
        h = host_of(url)
        if (h != host_of(original) or h != row['fetch_host'] or h != row['e_host']
                or is_banned(url) or is_search_host(h) or any(s in h for s in self.denied)
                or any(_under(h, d) for d in self.blocked | self.disabled)
                or any(_under(h, d) for d in self.denied_domains if d)
                or any(s in h for s in UNTRUSTED[:5]) or url in self.ignored):
            return None
        entry = next((p for d, p in sorted(self.policy.items(), key=lambda p: -len(p[0])) if _under(h, d)), {})
        scope = entry.get('scope')
        cj = _under(h, 'cjnews.cj.net')
        basis = official.site_basis_for(self.conn, subject, h) if subject else None
        # A guessed-domain probe may surface leads. Curated ownership or a verified publisher-linked site can establish ownership.
        seeded_owner = basis == 'seed' and (self.targets[subject]['origin'] == 'seed' or self.conn.execute(
            "SELECT 1 FROM official_site WHERE target_id=? AND host=? AND basis='seed' AND verified=1",
            (subject, h)).fetchone())
        if (seeded_owner or basis == 'article_link') and 'official' in claims.TRUSTED_TIERS:
            if not cj or (scope == 'verified_cj_program_only' and basis == 'seed' and re.search(r'CJ|씨제이', row['quote'], re.I)):
                return {'group': entry.get('group', h), 'official': True}
        if (not cj and 'press' in claims.TRUSTED_TIERS and scope in ('restaurant_reporting', 'original_food_reporting')
                and PRESS_CUE.search(row.get('scope_text', row['quote']))):
            path = urlsplit(url).path
            if path not in ('', '/') and not re.search(r'/(?:sports?|entertain(?:ment)?|celebrity)(?:/|$)', path, re.I):
                return {'group': entry['group'], 'official': False}
        return None

    def snapshot(self, row):
        key = row['fetch_id']
        if key not in self.snapshots:
            self.snapshots[key] = None
            if not row['text_ref'] or not row['raw_ref'] or not row['content_hash']:
                return None
            try:
                body = self.raw.read(row['raw_ref'])
                if self.raw.sha(body) != row['content_hash']:
                    return None
                stored = self.raw.read_text(row['text_ref'])
                visible = parse_html(Response(row['src'], 200, {}, body).text(), row['src'], include_jsonld_text=False)['text']
            except (OSError, UnicodeError, ValueError):
                return None
            self.snapshots[key] = (stored, _normalized(visible))
        return self.snapshots[key]

    def attributed_reference(self, row, subject):
        policy = self.policy_for(row, subject)
        if not policy:
            return None
        return {'extraction_id': row.get('extraction_id'), 'fetch_id': row['fetch_id'], 'src': row['src'],
                'host': row['fetch_host'], 'quote': '', 'published_at': row['fetch_published'],
                'quote_span': None, 'content_hash': row['content_hash'] or '', **policy}

    def reference(self, row, subject, *, verify_span=True):
        policy = self.policy_for(row, subject)
        quote = row['quote']
        if not policy or not _public_quote(quote):
            return None
        snapshot = self.snapshot(row)
        if not snapshot:
            return None
        stored, visible = snapshot
        if _normalized(quote) not in visible:
            return None
        if verify_span:
            start, end = row['span_start'], row['span_end']
            if start is None or end is None or not 0 <= start < end <= len(stored):
                return None
            offset = stored.find(quote, start, end)
            if offset < 0:
                return None
        else:
            offset = stored.find(quote)
            if offset < 0:
                return None
            start, end = offset, offset + len(quote)
        return {
            'extraction_id': row.get('extraction_id'), 'fetch_id': row['fetch_id'], 'src': row['src'],
            'host': row['fetch_host'], 'quote': quote, 'published_at': row['fetch_published'],
            'span': [start, end], 'quote_span': [offset, offset + len(quote)],
            'content_hash': row['content_hash'], **policy,
            **({'candidate_id': row['candidate_id']} if row.get('candidate_id') else {}),
            '_window': stored[max(0, offset - 130):offset + len(quote) + 130],
            '_body': visible,
        }

    def rows(self):
        if self.proofs is not None:
            return self.proofs
        self.proofs = []
        # Link service candidates back to their original extraction. No copied candidate assertion
        # or candidate status participates in the gate.
        rows = self.conn.execute("""WITH extraction_fetch AS (
            SELECT extraction_id,fetch_id FROM extraction WHERE extractor_version=?
            UNION
            SELECT l.extraction_id,l.fetch_id FROM article_extraction_fetch l JOIN extraction e USING(extraction_id)
              WHERE e.extractor_version=?
          ), candidate_locators AS (
            SELECT json_extract(evidence_json,'$.locator.extraction_id') extraction_id, MIN(candidate_id) candidate_id
            FROM candidate_fact WHERE asserted_by='source' AND json_valid(evidence_json)
            GROUP BY json_extract(evidence_json,'$.locator.extraction_id')
          )
          SELECT e.*, f.fetch_id snapshot_id, c.candidate_id, e.host e_host, f.host fetch_host, f.url fetch_url,
            f.canonical_url src,f.raw_ref,f.text_ref,f.content_hash,f.published_at fetch_published,f.fetched_at
            FROM extraction e JOIN extraction_fetch ef USING(extraction_id) JOIN fetch_record f ON f.fetch_id=ef.fetch_id
            LEFT JOIN candidate_locators c USING(extraction_id)
            WHERE e.asserted_by='source' AND e.pending_only=0
              AND e.epistemic IN ('claim','fact') AND e.ambiguity IS NULL AND e.extractor LIKE 'rule:%'
              AND f.outcome='ok' AND f.method IN ('html','browser') AND f.http_status BETWEEN 200 AND 299
              AND e.label IN ('career','employment_claim','opening','closure')
            ORDER BY e.canonical_url,e.extraction_id,f.fetched_at,f.fetch_id""", (EXTRACTOR_VERSION, EXTRACTOR_VERSION))
        for record in rows:
            row = dict(record)
            row['fetch_id'] = row.pop('snapshot_id')
            if pending(row) or canonicalize_url(row['canonical_url']) != row['src']:
                continue
            label = row['label']
            is_lineage = label == 'career'
            venue_name = row['org_text'] if is_lineage else row['restaurant_text']
            venue_id = row['resolved_org_target'] if is_lineage else row['resolved_restaurant_target']
            if label == 'closure' and not venue_id:
                venue_id = resolve_restaurant(self.conn, self.gaz, venue_name, False, '', '', context=row['quote'])[0]
            venue = self.targets.get(venue_id)
            chef_id = row['resolved_chef_target']
            chef = self.targets.get(chef_id)
            if 'homonym_unresolved' in (row.get('resolve_note') or '') and (not venue or not chef):
                ref = self.reference(row, venue_id)
                if ref:
                    self.identity_conflicts.append((row, ref))
            if not venue or not _venue_ok(venue) or (label != 'closure' and (not chef or not _chef_ok(chef))):
                continue
            if resolve_restaurant(self.conn, self.gaz, venue_name, False, '', '', context=row['quote'])[0] != venue_id:
                continue
            if label != 'closure':
                assertions = extract.publication_assertions(row['quote'], self.gaz)
                context = [venue_id]
                if not is_lineage:
                    context.extend(t for a in assertions if a['label'] == 'career'
                                   if (t := resolve_restaurant(self.conn, self.gaz, a['org'], False, '', '', context=row['quote'])[0]))
                if resolve_chef(self.conn, self.gaz, row['chef_text'], context, False, '', '')[0] != chef_id:
                    continue
                match = next((a for a in assertions
                              if a['label'] == label and norm(a['chef']) == norm(row['chef_text'])
                              and norm(a.get('org') or a.get('rest')) == norm(venue_name)), None)
                if not match:
                    continue
                row['role_text'] = match['role']
            elif not extract.confirmed_closure(row['quote'], venue_name):
                continue
            ref = self.reference(row, venue_id) or (self.attributed_reference(row, venue_id) if label != 'closure' else None)
            # A verified restaurant bio can state its own chef's earlier career.
            # Verify this association against a current affiliation during assembly.
            self.proofs.append((row, venue_id, ref))
        return self.proofs

    def qualified_targets(self):
        by_target = {}
        for row, venue, ref in self.rows():
            if ref and ref['quote'] and row['label'] != 'closure' and (row['label'] == 'career' or self.current(row, ref)):
                for target in (venue, row['resolved_chef_target']):
                    by_target.setdefault((target, row['resolved_chef_target'], venue, row['label'] == 'career'), []).append(ref)
        return {edge[0] for edge, refs in by_target.items() if verified(refs)}

    def current(self, row, ref):
        # Undated official profiles must actually assert a current lead position.
        return _recent(ref['published_at'], self.now) or (
            ref['official'] and not ref['published_at'] and row['label'] == 'employment_claim')

    def entity_contexts(self):
        chefs, cities = {}, {}
        for row, venue, ref in self.rows():
            if ref and row['label'] in ('opening', 'employment_claim') and self.current(row, ref):
                person = self.targets[row['resolved_chef_target']]
                chefs.setdefault(venue, set()).add(person['name_ko'] or person['name_en'])
                cities.setdefault(venue, set()).update(_location(ref['quote'], row['restaurant_text']))
        return {venue: {'chef': next(iter(names)), 'city': next(iter(cities[venue])) if len(cities[venue]) == 1 else None}
                for venue, names in chefs.items() if len(names) == 1}


def _unique(refs):
    unique = {}
    for ref in sorted(refs, key=lambda r: (not r['official'], r['src'], r['quote'], r['content_hash'])):
        unique.setdefault((ref['src'], ref['quote']), ref)
    return list(unique.values())


def verified(refs):
    refs = _unique(r for r in refs if r['quote'])
    parents = list(range(len(refs)))

    def root(i):
        while parents[i] != i:
            i = parents[i]
        return i

    shingles = [evaluate.shingles(r['_body'][:3000]) for r in refs]
    for i, a in enumerate(refs):
        for j, b in enumerate(refs[:i]):
            if (a['group'] == b['group'] or a['src'] == b['src'] or a['quote'] == b['quote']
                    or evaluate.jaccard(shingles[i], shingles[j]) >= evaluate.REPOST_THRESHOLD
                    or not shingles[i] and not shingles[j] and SequenceMatcher(None, a['_body'], b['_body']).ratio() >= .88
                    or claims.independent_sources([(a['src'], a['quote']), (b['src'], b['quote'])]) < 2):
                parents[root(i)] = root(j)
    return len({root(i) for i in range(len(refs))}) >= 2


def _cities(value):
    hits = {city for city, pat in CITY_PATTERNS.items() if pat.search(value)}
    hits.update(KOREAN_CITY.findall(value))
    if SEOUL_ADDRESS.search(value):
        hits.add('서울')
    return hits


def _destination_places(quote, venue):
    v = r'[‘\'"“「『]?' + re.escape(venue) + r'[’\'"”」』]?'
    patterns = (
        rf'(?P<city>[^,.!?]{{2,70}}?)(?:에서|에)\s+(?:새\s+)?(?:{extract.VENUE_TYPE}\s*)?{v}',
        rf'(?P<city>[^,.!?]{{2,65}}?)(?:의|에\s+있는)\s+{extract.VENUE_TYPE}\s*{v}',
        rf'{v}\s*(?:[은는이가]\s*)?(?:주소\s*[:：]?\s*|(?:is )?(?:located|based) (?:at|in) )(?P<city>[^.!?]{{2,100}})',
        rf'{v}\s*(?:[은는이가]\s*)?(?P<city>[^,.!?]{{2,65}}?)(?:에\s*(?:있다|위치)|에서\s*(?:영업|운영))',
        rf'{v}\s+in\s+(?P<city>[^,.!?]{{2,55}})',
    )
    for pattern in patterns:
        for match in re.finditer(pattern, quote, re.I):
            yield re.split(r'(?:셰프|쉐프)(?:가|는|이|은)\s+', match['city'])[-1]


def _location(quote, venue):
    """Accept a destination-bound locative or address, even when it is on a separate page."""
    return {city for place in _destination_places(quote, venue) for city in _cities(place)}


def kitchen_counts(seed):
    counts = {k['id']: set() for k in seed.get('kitchens', [])}
    for row in seed.get('restaurants', []):
        if row.get('type') == 'alumni' and row.get('kitchen') in counts:
            counts[row['kitchen']].add(row['id'])
    for row in seed.get('career_projection', {}).get('restaurants', ()):
        for edge in row.get('origins', ()):
            if edge['kitchen_id'] in counts:
                counts[edge['kitchen_id']].add(row['id'])
    return {k: len(ids) for k, ids in counts.items()}


def _site_names(row):
    return {norm(n) for n in (row.get('name'), row.get('name_en'), *row.get('prev_names', []), *row.get('aliases', [])) if n}


def _site_id(restaurant):
    return 'alumni-' + stable_id('venue', restaurant).split('_', 1)[1]


def _matches_destination(row, restaurant, aliases):
    return (row['id'] == _site_id(restaurant)
            or (row.get('publication') or {}).get('restaurant_target_id') == restaurant
            or bool(_site_names(row) & aliases))


def _tombstoned(seed, restaurant, aliases):
    return any(t['restaurant_target_id'] == restaurant or set(t.get('aliases', [])) & aliases for t in seed.get(TOMBSTONES, []))


def _public_refs(refs):
    return [{k: v for k, v in ref.items() if not k.startswith('_')} for ref in _unique(refs)]


def _fingerprint(values, evidence, gate):
    proofs = sorted((edge, ref['src'], ref['quote'], ref['content_hash'])
                    for edge, refs in evidence.items() for ref in _unique(refs))
    return digest([values, proofs, gate])


def _location_refs(corpus, restaurant, evidence_rows):
    conn = corpus.conn
    # Include the supporting articles and actual verified destination pages. Other linked documents may also
    # supply an explicitly attributed address after the extractor has seen the venue name.
    rows = {r['fetch_id']: r for r in evidence_rows}
    for f in conn.execute("""SELECT f.*, f.url fetch_url, f.host fetch_host, f.host e_host,
          f.canonical_url src, f.published_at fetch_published FROM fetch_record f
          WHERE f.outcome='ok' AND f.method IN ('html','browser') AND f.http_status BETWEEN 200 AND 299
          AND (EXISTS (SELECT 1 FROM official_site o WHERE o.target_id=? AND o.verified=1 AND o.host=f.host)
               OR EXISTS (SELECT 1 FROM doc_target d WHERE d.fetch_id=f.fetch_id AND d.target_id=?))""",
                          (restaurant, restaurant)):
        rows.setdefault(f['fetch_id'], dict(f))
    found = {}
    for row in rows.values():
        # Avoid reading the raw bodies of search results and unrelated hosts.
        subject = corpus.targets[restaurant]
        if not corpus.policy_for({**row, 'quote': '레스토랑'}, restaurant) and not _under(row['fetch_host'], 'cjnews.cj.net'):
            continue
        snapshot = corpus.snapshot(row)
        if not snapshot:
            continue
        for off, sentence in extract.sentences(snapshot[0]):
            quote = sentence.strip()
            if not _public_quote(quote) or extract.FUTURE.search(quote) or extract.NON_PUBLICATION_CONTEXT.search(quote):
                continue
            venues = {m['surface'] for m in corpus.gaz.find(quote) if m['target_id'] == restaurant}
            cities = {c for venue in venues for c in _location(quote, venue)}
            if not cities and not venues and re.match(r'^(?:주소\s*[:：]|address\s*:)', quote, re.I):
                if (official.site_basis_for(conn, restaurant, row['fetch_host']) in ('seed', 'article_link')
                        and official.names_match(snapshot[1], subject)):
                    cities = _cities(quote)
            if not cities:
                continue
            loc_row = {**row, 'quote': quote, 'span_start': off, 'span_end': off + len(sentence), 'scope_text': snapshot[1][:4000]}
            loc_row['extraction_id'] = row.get('extraction_id') if row.get('quote') == quote else None
            ref = corpus.reference(loc_row, restaurant)
            if ref:
                ref['_places'] = [place for venue in venues for place in _destination_places(quote, venue)] or [quote]
                for city in cities:
                    found.setdefault(city, []).append(ref)
    return found


def _opened(affiliation):
    dates = {}
    for row, ref in affiliation:
        if row['label'] != 'opening':
            continue
        quote = ref['quote']
        if not quote:
            continue
        # Dates in another tenure clause cannot supply the opening date.
        quote = re.split(r'(?:셰프|쉐프)(?:가|는|이|은)\s+', quote)[-1]
        dates_in_quote = list(extract.ISO_DATE.finditer(quote)) + list(extract.DATE_RE.finditer(quote))
        iso = None
        if len(dates_in_quote) == 1:
            date_match = dates_in_quote[0]
            iso = date_match.group() if date_match.re is extract.ISO_DATE else extract.iso_from_dateish(date_match, (ref['published_at'] or '')[:4])[0]
        elif not dates_in_quote:
            _, iso, _ = extract.relative_or_english_date(quote, (ref['published_at'] or '')[:4])
        if iso:
            event_date = _date(iso + {4: '-01-01', 7: '-01'}.get(len(iso), ''))
            article_date = _date(ref['published_at'])
            if event_date and (not article_date or event_date <= article_date):
                dates.setdefault(iso, []).append(ref)
    return dates


def bundles(corpus, seed):
    kitchen_ids = {k['id'] for k in seed.get('kitchens', [])}
    counts = kitchen_counts(seed)
    lineage, affiliations, closed = {}, {}, set()
    for row, venue, ref in corpus.rows():
        chef = row['resolved_chef_target']
        if row['label'] == 'career':
            sid = json.loads(corpus.targets[venue]['attrs_json']).get('seed_id')
            if sid in kitchen_ids:
                lineage.setdefault(chef, {}).setdefault(sid, []).append((row, venue, ref))
        elif ref and row['label'] == 'closure':
            closed.add(venue)
        elif ref and corpus.current(row, ref):
            affiliations.setdefault((chef, venue), []).append((row, ref))

    for (chef, venue), aff in sorted(affiliations.items()):
        aliases = corpus.aliases.get(venue, set())
        if _tombstoned(seed, venue, aliases):
            continue
        existing = [r for r in seed.get('restaurants', []) if _matches_destination(r, venue, aliases)]
        generated = (existing[0].get('publication') or {}) if len(existing) == 1 else {}
        if existing and not (generated.get('restaurant_target_id') == venue and generated.get('chef_target_id') == chef):
            continue
        kitchens = []
        official_hosts = {ref['host'] for _, ref in aff if ref['official']}
        for kitchen, links in lineage.get(chef, {}).items():
            proofs = []
            for row, origin, ref in links:
                if not ref or ref['host'] in official_hosts:
                    owned = corpus.reference(row, venue)
                    if owned and owned['official'] and owned['host'] in official_hosts:
                        ref = owned
                if ref:
                    proofs.append((row, ref))
            if proofs:
                refs = [r for _, r in proofs]
                kitchens.append((not verified(refs), counts[kitchen], kitchen, proofs))
        if generated:
            kitchens = [k for k in kitchens if k[2] == existing[0]['kitchen']]
        past_weak, _, kitchen, past = min(kitchens, default=(True, 0, None, []))
        if not kitchen:
            yield {'partial': 'lineage', 'chef': corpus.targets[chef]['name_ko'], 'name': corpus.targets[venue]['name_ko'], 'restaurant_target_id': venue}
            continue
        where = _location_refs(corpus, venue, [row for row, _ in aff])
        city = next(iter(where)) if len(where) == 1 else None
        evidence = {'lineage': [r for _, r in past], 'affiliation': [r for _, r in aff],
                    'location': where.get(city, [])}
        current_weak = not verified(evidence['affiliation'])
        primary_date = max((r['published_at'] or '' for _, r in aff), default='')
        dates = _opened(aff)
        conflict = len(dates) > 1 or venue in closed or len(where) > 1 or any(
            (other_venue == venue and other_chef != chef)
            or (other_chef == chef and other_venue != venue and any(
                row['label'] == 'employment_claim' and (r['published_at'] or '') >= primary_date
                for row, r in other
            ))
            for (other_chef, other_venue), other in affiliations.items() if (chef, venue) != (other_chef, other_venue)
        )
        if existing and (existing[0].get('closed_at') or existing[0]['city'] != city and city):
            conflict = True
        if conflict:
            gate = 'conflict'
        elif not city:
            gate = 'missing_location'
        else:
            gate = 'checking' if past_weak or current_weak else 'verified'
        role_refs = {}
        for row, ref in past:
            if row['role_text'] and ref['quote']:
                role_refs.setdefault(row['role_text'], []).append(ref)
        role = '출신'
        checking = ['kitchen'] if past_weak else []
        if current_weak:
            checking.append('chef')
        if len(role_refs) == 1:
            title, refs = next(iter(role_refs.items()))
            if verified(refs):
                role = title
            elif gate == 'checking':
                role = title
                checking.append('role')
        opened = '미확인'
        if len(dates) == 1:
            date, refs = next(iter(dates.items()))
            if verified(refs):
                opened = date
            elif gate == 'checking':
                opened = date
                checking.append('opened')
        name, person = corpus.targets[venue], corpus.targets[chef]
        values = {'chef_target_id': chef, 'restaurant_target_id': venue, 'chef': person['name_ko'] or person['name_en'],
                  'name': name['name_ko'] or name['name_en'], 'kitchen': kitchen, 'city': city, 'role': role,
                  'opened': opened, 'career_checking': checking}
        if city and city in CITY_PATTERNS:
            pattern = rf'(?:{CITY_PATTERNS[city].pattern})\s+([가-힣]{{1,8}}(?:구|동|읍|면))(?=\s|에서|에|[,.]|$)'
            areas = {m[1] for r in evidence['location'] for place in r['_places'] for m in re.finditer(pattern, place, re.I)}
            if len(areas) == 1:
                values['area'] = next(iter(areas))
        cuisine_names = {'한식당': '한식', '일식당': '일식', '중식당': '중식'}
        cuisines = {cuisine_names[m[1]] for row, ref in aff for m in re.finditer(
            r'(한식당|일식당|중식당)\s+[‘\'"“「『]?' + re.escape(row['restaurant_text']), ref['quote'])}
        if len(cuisines) == 1:
            values['cuisine'] = next(iter(cuisines))
        if official_hosts:
            values['url'] = next(r['src'] for r in _unique(evidence['affiliation']) if r['official'])
        values['bundle_id'] = stable_id('pub', chef, venue, kitchen)
        yield {**values, 'gate': gate, 'fingerprint': _fingerprint(values, evidence, gate), 'evidence': evidence}

    with_affiliation = {chef for chef, _ in affiliations}
    for chef, kitchens in lineage.items():
        if chef not in with_affiliation and any(ref for links in kitchens.values() for _, _, ref in links):
            yield {'partial': 'affiliation', 'chef': corpus.targets[chef]['name_ko'], 'name': None,
                   'restaurant_target_id': None}


def _proof_selection(bundle):
    return {edge: [{k: ref[k] for k in ('extraction_id', 'fetch_id', 'src', 'quote_span')}
                   for ref in refs] for edge, refs in bundle['evidence'].items()}


def _close_superseded(conn, current, now):
    for row in conn.execute("SELECT review_id,subject_id FROM review_queue WHERE reason=? AND state='open'", (REASON,)):
        if current.get(row['subject_id']) != row['review_id']:
            conn.execute("UPDATE review_queue SET state='dismissed',resolved_at=?,payload_json=json_set(payload_json,'$.dismissal','superseded') WHERE review_id=?",
                         (now, row['review_id']))


def _gap_query(conn, seeds, bundle):
    name, chef = bundle['name'], bundle['chef']
    exact = f'"{chef}" "{name}"' if name else f'"{chef}"'
    suffix = {'lineage': '출신 셰프 경력', 'affiliation': '셰프 오너 레스토랑',
              'missing_location': '셰프 소개 주소'}[bundle.get('partial') or bundle['gate']]
    query = f'{exact} {suffix}'
    return discover.enqueue(conn, path='reverse_gap', pool='backfill', url=discover.search_url(seeds, query),
                            query=query, target_id=bundle['restaurant_target_id'], relevance=.75, caps=seeds['caps'])


def prepare(conn, seed, raw, now=None, *, seeds=None, corpus=None, enqueue=True):
    corpus = corpus or Corpus(conn, raw, seeds, now)
    stamp = corpus.now.isoformat()
    current = {}
    stats = {'verified': 0, 'checking': 0, 'conflict': 0, 'missing_location': 0, 'partial': 0, 'gap_jobs': 0}
    for bundle in bundles(corpus, seed):
        if 'partial' in bundle:
            stats['partial'] += 1
            if enqueue:
                stats['gap_jobs'] += _gap_query(conn, corpus.seeds, bundle)
            continue
        gate, bid = bundle['gate'], bundle['bundle_id']
        stats[gate] += 1
        if gate == 'missing_location' and enqueue:
            stats['gap_jobs'] += _gap_query(conn, corpus.seeds, bundle)
        payload = {**bundle, 'evidence': {edge: _public_refs(refs) for edge, refs in bundle['evidence'].items()}}
        rid = claims.queue_review(conn, REASON, SUBJECT, bid, payload, dedupe_key=f"{bid}:{bundle['fingerprint']}", now=stamp)
        current[bid] = rid
        if gate in ('verified', 'checking'):
            approval = {'fingerprint': bundle['fingerprint'], 'at': stamp, 'proofs': _proof_selection(payload), 'decision': 'approve'}
            conn.execute("""UPDATE review_queue SET state='resolved',resolved_at=?,payload_json=?
                 WHERE review_id=? AND (state='open' OR
                  (json_extract(payload_json,'$.approval') IS NULL AND COALESCE(json_extract(payload_json,'$.dismissal'),'')<>'publication_rollback'))""",
                         (stamp, _json({**payload, 'approval': approval}, indent=None), rid))
    _close_superseded(conn, current, stamp)
    stats['identity_conflicts'] = 0
    for row, ref in corpus.identity_conflicts:
        key = stable_id('publication-identity', ref['src'], ref['quote'], row['resolve_note'])
        claims.queue_review(conn, 'publication_identity_conflict', 'extraction', row['extraction_id'],
                            {'reason': row['resolve_note'], 'evidence': _public_refs([ref])}, dedupe_key=key, now=stamp)
        stats['identity_conflicts'] += 1
    reconcile_tombstones(conn, seed, stamp)
    conn.commit()
    stats.update(publication_metrics(conn, seed, corpus.now))
    return stats


def publication_metrics(conn, seed, now):
    rows = seed.get('restaurants', [])
    gaz = Gazetteer(conn) if conn else None
    named = set()
    for row in rows:
        marker = row.get('publication') or {}
        for ko, en in chef_names(row.get('chef'), row.get('chef_en')):
            matches = {r['target_id'] for name in (ko, en) if gaz and name for r in gaz.lookup(name) if r['kind'] == 'chef'}
            named.add(marker.get('chef_target_id') or (next(iter(matches)) if len(matches) == 1 else tid('chef', ko)))
    from .lineage_identity import candidate_keys
    from .site_projection import KEY

    projection = seed.get(KEY, {})
    locations = {}
    for kind, branches in seed.get('ai_promotion', {}).get('snapshot', {}).get('site', {}).items():
        locations.update({sid: branch for branch, ids in branches.items() for sid in ids})
    by_key = {}
    projected_chefs = {}
    projected_names = {}
    for kind in ('restaurants', 'kitchens'):
        for node in projection.get(kind, ()):
            for edge in (*node.get('origins', ()), *node.get('career_affiliations', ())):
                chef = edge['chef_id']
                names = set().union(*(candidate_keys(name) for name in (edge.get('chef_name'), edge.get('chef_name_en')) if name))
                projected_chefs.setdefault(chef, set()).update(names)
                employer = edge.get('kitchen_id', node['id'])
                branch = locations.get(employer, employer)
                key = (chef, branch, edge['relation'], edge.get('role'), edge.get('period'))
                verified = edge.get('lineage_status') == 'verified'
                by_key[key] = by_key.get(key, False) or verified
                if edge['relation'] in ('former', 'training'):
                    projected_names.setdefault(branch, set()).update(names)
    legacy_verified = legacy_checking = 0
    for row in rows:
        if row.get('type') != 'alumni':
            continue
        employer = locations.get(row.get('kitchen'), row.get('kitchen'))
        aliases = set().union(*(candidate_keys(n) for pair in chef_names(row.get('chef'), row.get('chef_en')) for n in pair if n))
        if aliases & projected_names.get(employer, set()):
            continue
        legacy_verified += row.get('lineage_status') == 'verified'
        legacy_checking += bool(row.get('career_checking'))
    named_spellings = set().union(*(candidate_keys(n) for r in rows
                                    for pair in chef_names(r.get('chef'), r.get('chef_en')) for n in pair if n))
    metrics = {'named_chefs': len(named) + sum(not (names & named_spellings) for names in projected_chefs.values()),
               'alumni_by_kitchen': kitchen_counts(seed),
               'verified_total': legacy_verified + sum(by_key.values()),
               'checking_total': legacy_checking + sum(not verified for verified in by_key.values())}
    if conn:
        queue = list(conn.execute("""SELECT state,payload_json,created_at FROM review_queue WHERE reason=?""", (REASON,)))
        dates = [instant(r['created_at']) for r in queue if r['state'] == 'open']
        metrics['oldest_review_hours'] = round((now - min(dates)).total_seconds() / 3600, 1) if dates else None
        metrics['review_rejected'] = sum(json.loads(r['payload_json']).get('ai_review', {}).get('decision') == 'reject' for r in queue)
    return metrics


def _matching_reviews(conn):
    return {r['dedupe_key']: (r, json.loads(r['payload_json']))
            for r in conn.execute("SELECT * FROM review_queue WHERE reason=? AND subject_type=?", (REASON, SUBJECT))}


def _ready(bundle, reviews):
    row_payload = reviews.get(f"{bundle['bundle_id']}:{bundle['fingerprint']}")
    if not row_payload or bundle['gate'] not in ('verified', 'checking'):
        return None
    row, payload = row_payload
    approval = payload.get('approval') or {}
    if row['state'] == 'resolved' and approval.get('decision') == 'approve' and approval.get('fingerprint') == bundle['fingerprint']:
        return (bundle, row, approval)
    return None


def _json(value, indent=1):
    return json.dumps(value, ensure_ascii=False, indent=indent, sort_keys=indent is None)


def atomic_json(path, value):
    """Replace in the same directory. A failed write leaves the previous file intact."""
    path = Path(path)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=f'.{path.name}.')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as fh:
            os.fchmod(fh.fileno(), stat.S_IMODE(path.stat().st_mode) if path.exists() else 0o644)
            fh.write(_json(value))
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(temporary, path)
        dfd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


@contextlib.contextmanager
def daily_lock(repo):
    result = subprocess.run(['git', 'rev-parse', '--path-format=absolute', '--git-common-dir'], cwd=repo,
                            capture_output=True, text=True, check=True)
    path = Path(result.stdout.strip()) / 'kg_daily.lock'
    inherited = os.environ.get('KG_DAILY_LOCK_FD')
    if inherited:
        fd = int(inherited)
        if not os.path.samestat(os.fstat(fd), path.stat()):
            raise ValueError('KG_DAILY_LOCK_FD does not refer to the daily lock')
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
        return
    with open(path, 'a', encoding='utf-8') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


@contextlib.contextmanager
def seed_lock(seed_path):
    """Use the daily lock for a checkout and a local lock for standalone seeds."""
    directory = Path(seed_path).resolve().parent
    git = subprocess.run(['git', 'rev-parse', '--is-inside-work-tree'], cwd=directory,
                         capture_output=True, text=True)
    if git.returncode == 0 and git.stdout.strip() == 'true':
        with daily_lock(directory):
            yield
    else:
        with open(directory / ('.' + Path(seed_path).name + '.lock'), 'a', encoding='utf-8') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            yield


@contextlib.contextmanager
def configured_db(path=None):
    path = path or os.environ.get('KG_SERVICE_DB')
    if not path or not Path(path).is_file():
        yield None, None
        return
    with contextlib.closing(connect(path)) as conn:
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='extraction'").fetchone():
            yield None, None
        else:
            yield conn, RawStore(Path(path).resolve().parent / 'raw')


def row_digest(row):
    return digest({k: v for k, v in row.items() if k != 'publication' and k not in ENRICHMENT})


def _event(row):
    p = row['publication']
    return {'id': row['id'], 'name': p.get('creation_name', row['name']), 'kind': 'new_alumni',
            'applied_at': p['day_kst'], 'day_kst': p['day_kst'], 'bundle_id': p['bundle_id'],
            'gate': p.get('creation_gate', 'verified' if row['lineage_status'] == 'verified' else 'checking'),
            'sources': p.get('creation_sources', sorted({e['src'] for e in row.get('lineage_evidence', [])}))}


def reconcile_events(seed, audit):
    known = {(r['id'], r['day_kst']) for r in audit if r.get('kind') == 'new_alumni'}
    events = []
    for row in seed['restaurants']:
        if row.get('publication'):
            event = _event(row)
            key = (event['id'], event['day_kst'])
            if key not in known:
                known.add(key)
                events.append(event)
    for tomb in seed.get(TOMBSTONES, []):
        event = tomb.get('creation_event')
        if event and (event['id'], event['day_kst']) not in known:
            known.add((event['id'], event['day_kst']))
            events.append(event)
    return events


def _row_fields(bundle):
    past = _unique(bundle['evidence']['lineage'])
    evidence = [{'kind': edge, 'src': ref['src'], **({'quote': ref['quote']} if ref['quote'] else {})}
                for edge in EDGES for ref in _unique(bundle['evidence'][edge])]
    fields = {k: bundle[k] for k in ('name', 'chef', 'kitchen', 'city', 'role', 'opened')}
    fields.update(type='alumni', lineage_src=past[0]['src'], lineage_evidence=evidence, price=None,
                  lineage_status='verified' if bundle['gate'] == 'verified' else 'unverified')
    fields.update({k: bundle[k] for k in ('area', 'cuisine', 'url') if bundle.get(k)})
    if bundle.get('career_checking'):
        fields['career_checking'] = bundle['career_checking']
    press = sorted({r['src'] for edge in ('lineage', 'affiliation') for r in bundle['evidence'][edge] if not r['official']})
    if press:
        fields['press'] = press
    return fields


def reconcile_tombstones(conn, seed, now):
    for tomb in seed.get(TOMBSTONES, []):
        conn.execute("""UPDATE review_queue SET state='dismissed',resolved_at=?,
            payload_json=json_set(payload_json,'$.dismissal','publication_rollback')
            WHERE reason=? AND state<>'dismissed' AND
            (subject_id=? OR json_extract(payload_json,'$.restaurant_target_id')=?)""",
                     (now, REASON, tomb['bundle_id'], tomb['restaurant_target_id']))


def apply_ready(conn, seed, raw, now=None):
    """Revalidate prepared approvals and mutate the caller's locked seed snapshot."""
    now = instant(now)
    stats = {'added': 0, 'upgraded': 0, 'edited': 0, 'ready': 0}
    if not conn:
        return stats
    stamp = now.isoformat()
    reconcile_tombstones(conn, seed, stamp)
    conn.commit()
    reviews = _matching_reviews(conn)
    if not reviews:
        from .site_projection import materialize

        if materialize(conn, seed):
            stats['projection_changed'] = True
        return stats
    corpus = Corpus(conn, raw, now=now)
    live = seed['restaurants']
    counts = kitchen_counts(seed)
    current = [ready for bundle in bundles(corpus, seed) if bundle.get('bundle_id')
               if (ready := _ready(bundle, reviews))]
    existing = {r['id']: r for r in live}
    new = []
    for bundle, review, approval in current:
        rid = _site_id(bundle['restaurant_target_id'])
        row = existing.get(rid)
        if row is None:
            new.append((bundle, review, approval))
            continue
        marker = row.get('publication')
        if not marker or row.get('lineage_status') != 'unverified' or bundle['gate'] != 'verified':
            continue
        if marker['row_digest'] != row_digest(row):
            stats['edited'] += 1
            claims.queue_review(conn, 'publication_row_edited', 'restaurant', rid,
                                {'restaurant_id': rid, 'bundle_id': bundle['bundle_id']},
                                dedupe_key=f"published-edit:{rid}:{row_digest(row)}", now=stamp)
            continue
        for field in ('career_checking', 'press', 'url', 'area', 'cuisine'):
            row.pop(field, None)
        row.update(_row_fields(bundle), checked=now.date().isoformat())
        marker['upgrade_review_id'] = review['review_id']
        marker['verified_at'] = stamp
        marker['row_digest'] = row_digest(row)
        stats['upgraded'] += 1

    day = now.date().isoformat()
    represented = {norm(ko) for row in live for ko, _ in chef_names(row.get('chef'), row.get('chef_en'))}
    represented_ids = {r['target_id'] for row in live for ko, en in chef_names(row.get('chef'), row.get('chef_en'))
                       for name in (ko, en) if name for r in corpus.gaz.lookup(name) if r['kind'] == 'chef'}
    covered = {k for k, count in counts.items() if count}
    stats['ready'] = len(new)
    while new:
        def order(item):
            b, _, approval = item
            already = b['chef_target_id'] in represented_ids or norm(b['chef']) in represented
            return (b['kitchen'] in covered, b['city'] != '서울', b['gate'] != 'verified',
                    already, instant(approval['at']), b['bundle_id'])

        selected = min(new, key=order)
        new.remove(selected)
        bundle, review, _ = selected
        venue = bundle['restaurant_target_id']
        aliases = corpus.aliases.get(venue, set())
        if any(_matches_destination(r, venue, aliases) for r in live):
            continue
        row = {'id': _site_id(venue), **_row_fields(bundle), 'checked': day}
        row['publication'] = {'bundle_id': bundle['bundle_id'], 'chef_target_id': bundle['chef_target_id'],
                              'restaurant_target_id': venue, 'review_id': review['review_id'], 'published_at': stamp,
                              'day_kst': day, 'row_digest': row_digest(row), 'creation_name': row['name'],
                              'creation_gate': bundle['gate'], 'creation_sources': sorted({e['src'] for e in row['lineage_evidence']})}
        live.append(row)
        stats['added'] += 1
        covered.add(bundle['kitchen'])
        represented.add(norm(bundle['chef']))
        represented_ids.add(bundle['chef_target_id'])
    conn.commit()
    from .site_projection import materialize

    if materialize(conn, seed):
        stats['projection_changed'] = True
    return stats


def rollback(seed_path, selector, reason, *, db_path=None, now=None):
    """Remove a generated row and reserve its publication slot and identity."""
    seed_path = Path(seed_path)
    if not reason.strip():
        raise ValueError('A rollback reason is required')
    now = instant(now).isoformat()
    with daily_lock(seed_path.parent.parent):
        seed = json.loads(seed_path.read_text(encoding='utf-8'))
        matches = [r for r in seed['restaurants'] if (m := r.get('publication')) and
                   selector in (r['id'], m['bundle_id'], m['review_id'], m.get('upgrade_review_id'))]
        tombs = seed.get(TOMBSTONES, [])
        old = [t for t in tombs if selector in (t['id'], t['bundle_id'], t['review_id'], t.get('upgrade_review_id'))]
        if len(matches) + len(old) > 1:
            raise ValueError('Ambiguous publication selector')
        if old:
            return None
        if not matches:
            raise ValueError('Generated publication not found')
        row = matches[0]
        p = row['publication']
        if p['row_digest'] != row_digest(row):
            raise ValueError('Publication has subsequent edits. Use a manual correction PR.\n' + _json(row))
        with configured_db(db_path) as (conn, _):
            aliases = _site_names(row)
            if conn:
                aliases.update(norm(r['alias']) for r in conn.execute('SELECT alias FROM target_alias WHERE target_id=?',
                                                                     (p['restaurant_target_id'],)))
            tomb = {k: p[k] for k in ('bundle_id', 'chef_target_id', 'restaurant_target_id', 'review_id', 'day_kst')}
            if p.get('upgrade_review_id'):
                tomb['upgrade_review_id'] = p['upgrade_review_id']
            tomb.update(id=row['id'], aliases=sorted(aliases), rolled_back_at=now, reason=reason, creation_event=_event(row))
            seed['restaurants'].remove(row)
            seed.setdefault(TOMBSTONES, []).append(tomb)
            atomic_json(seed_path, seed)
            if conn:
                reconcile_tombstones(conn, seed, now)
                conn.commit()
        return tomb
