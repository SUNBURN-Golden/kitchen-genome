"""Turn retained AI approvals into quote-backed G3 career evidence."""
import json
from pathlib import Path
import re
import sqlite3
from types import SimpleNamespace
import unicodedata

from kg_ai.article import digest
from kg_collect import evaluate, lineage, lineage_identity as identity, site_projection, site_publish
from kg_collect.targets import Gazetteer, name_key
from kg_daemon.db import stable_id, utcnow

KEY = 'ai_promotion'
BADGE = 'AI 검토'
LABELS = {'employment_claim': ('former', 'current'), 'career': ('former', 'training', 'current'),
          'mentorship_claim': ('training',), 'opening': ('opening',)}
INSTITUTION = re.compile(
    r'호텔|리조트|소사이어티|그룹|아카데미|대학교|대회|팝업|페스티벌|(?:호텔|리조트)앤|'
    r'\b(?:hotels?|resorts?|group|university|academy|competition|festival|gala|pop[- ]?up|catering)\b', re.I)
TEMPORARY = re.compile(r'게스트\s*(?:셰프|시프트)|객원\s*셰프|한시적|단기\s*(?:근무|셰프)|일일\s*셰프|'
                       r'초청\s*셰프|임시\s*(?:셰프|근무)|하루\s*동안|\b(?:guest|visiting|temporary)\b', re.I)
CONTEST = re.compile(r'대회|참가|출품|우승|수상|선정|\d위|상\(.*Award|\b(?:contest|competition|award)\b', re.I)
OUTLET = re.compile(r'레스토랑|식당|다이닝|부티크|\b(?:restaurant|bistro|eatery)\b', re.I)
KO_WORK = r'이끄는|이끈|이끌고 있\w*|운영하는|운영하고 있\w*|총괄하\w*|맡(?:고 있\w*|은|는다)|근무(?:하는|한다| 중)|일하는|재직 중'
TITLE = r'(?:(?:공동|총괄|오너|헤드|수석|책임|수|페이스트리)\s*)?(?:셰프|쉐프|주방장|요리사)'
COUNTRIES = {'한국': 'KR', '대한민국': 'KR', 'South Korea': 'KR', '미국': 'US', 'United States': 'US',
             '일본': 'JP', 'Japan': 'JP', '프랑스': 'FR', 'France': 'FR', '영국': 'GB', 'United Kingdom': 'GB',
             '이탈리아': 'IT', 'Italy': 'IT', '독일': 'DE', 'Germany': 'DE', '스페인': 'ES', 'Spain': 'ES',
             '싱가포르': 'SG', 'Singapore': 'SG', '아랍에미리트': 'AE', 'United Arab Emirates': 'AE',
             '캐나다': 'CA', 'Canada': 'CA', '호주': 'AU', 'Australia': 'AU'}
OTHER_CITIES = {'두바이', 'Dubai', '샌프란시스코', 'San Francisco', '파리', '런던', '홍콩', 'Hong Kong',
                '싱가포르', 'Singapore'}


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


def _original(conn, entry, raw_dir):
    review_id = entry['review_id']
    record = conn.execute('SELECT * FROM review_queue WHERE review_id=?', (review_id,)).fetchone()
    details, text, links, tier = {}, None, (), None
    if record:
        approval = json.loads(record['payload_json'] or '{}').get('ai_review', {})
        if record['state'] != 'resolved' or approval.get('decision') != 'approve' or approval.get('rolled_back'):
            return None
        if entry.get('extraction_id') != record['subject_id']:
            return None
        if record['subject_type'] == 'candidate_fact':
            original = conn.execute("""SELECT c.payload_json,c.evidence_json,d.raw_ref,d.canonical_url,d.published_at
                FROM candidate_fact c LEFT JOIN document d USING(document_id) WHERE candidate_id=?""", (record['subject_id'],)).fetchone()
            if original:
                locator = json.loads(original['evidence_json'])
                details = {**json.loads(original['payload_json']), 'quote': locator.get('quote'),
                           'canonical_url': original['canonical_url'] or locator.get('source_url'),
                           'published_at': original['published_at']}
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
            original = conn.execute("""SELECT e.*,f.text_ref,f.raw_ref,f.method FROM extraction e
                LEFT JOIN fetch_record f USING(fetch_id) WHERE extraction_id=?""", (record['subject_id'],)).fetchone()
            if original:
                details = dict(original)
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
        if details and (details.get('quote') != entry['quote'] or details.get('canonical_url') != entry['src']
                    or details.get('label', entry['label']) != entry['label']):
            return None
    if text is not None:
        from privacy_filter import sanitize_before_store
        text = unicodedata.normalize('NFC', sanitize_before_store(text)[0] or '')
        if entry['quote'] not in text:
            return None
    doc = SimpleNamespace(url=entry['src'], canonical_url=entry['src'], links=links,
                          published_at=details.get('published_at') or entry.get('date'))
    metadata = lineage._source(conn, doc, text or entry['quote'], tier)
    if metadata is None:
        return None
    if text is None:
        metadata['independent_eligible'] = 0
    metadata['body_fingerprint'] = site_projection.retained_fingerprint(
        digest(shingle)[:12] for shingle in evaluate.shingles(text[:3000].casefold())) if text else '[]'
    metadata['quote_key'] = digest(name_key(entry['quote']))
    metadata['snapshot_checked'] = text is not None
    chef = entry.get('chef')
    if text and isinstance(chef, str) and re.fullmatch(r'[가-힣]{2,4}', chef) and chef in text and chef not in entry['quote']:
        named = set(re.findall(r'(?<![가-힣])(' + chef[0] + r'[가-힣]{1,3})\s*(?:셰프|쉐프)', text))
        short = re.search(r'(?<![가-힣])' + chef[0] + r'\s+(?:셰프|쉐프)', entry['quote'])
        if named == {chef} and short:
            details['short_chef'] = short[0]
    return details, metadata


def _bilingual(tail, name):
    match = re.match(r"\s*[’'”\"]?\s*[(（]([^()（）]{2,80})[)）]", tail)
    if match:
        alias = match[1].strip()
        korean = r'[가-힣]'
        latin = r"[A-Za-zÀ-ÖØ-öø-ÿĀ-ž][A-Za-zÀ-ÖØ-öø-ÿĀ-ž .’'\-]*"
        if ((re.search(korean, name) and re.fullmatch(latin, alias))
                or (re.search(latin, name) and re.fullmatch(r'[가-힣 ]+', alias))):
            return alias
    return None


def _venue_suffix(clause, hit, venue):
    tail = clause[hit.end():]
    alias = _bilingual(tail, venue)
    tail = re.sub(r"^\s*['’”\"]?\s*\([^()]{2,80}\)", '', tail, count=1) if alias else tail
    return tail.lstrip("’'”\"」』"), alias


def _korean_relation(clause, subject, employer, tail):
    chef_first = subject.end() < employer.start()
    gap = clause[min(subject.end(), employer.end()):max(subject.start(), employer.start())]
    marks = " '’‘\"“”"
    if chef_first:
        if re.search(r'[가-힣]{2,4}\s+(?:셰프|쉐프|대표|사장)\s*[가는은이]', gap):
            return None
        # The named subject immediately governs this outlet and its predicate.
        lead = rf"\s*(?:{TITLE})?(?:\s*\(사진\))?\s*[가는은이]\s*"
        outlet = r"(?:[^,.!?;]{0,75}?[‘'\"]|(?:[^,.!?;]{0,38}?(?:식당|레스토랑|다이닝|부티크)\s+)?)"
        if re.fullmatch(lead + outlet, gap) and re.match(rf'^[을를]\s*(?:문을 열었|개업했|오픈했|창업했)', tail):
            return 'opening'
        if re.fullmatch(lead, gap) and re.match(rf'^의\s*주방을\s*(?:{KO_WORK})', tail):
            return 'current'
        if re.fullmatch(r"\s*(?:호텔\s*신라\s*)?", gap) and re.match(TITLE, tail):
            return 'current'
        crew = r"(?:[·ㆍ]\s*[가-힣]{2,4}(?:\s*셰프)?)*"
        # Adnominal leadership precedes the restaurant name, possibly with
        # a city, hotel or culinary description between the verb and outlet.
        if re.match(rf"^\s*{crew}{lead}\s*(?:이끄는|운영하는)\s+", gap):
            return 'current'
        if re.fullmatch(lead + outlet, gap) and re.match(rf'^[을를]\s*(?:{KO_WORK})', tail):
            return 'current'
        if re.match(rf"^\s*(?:{TITLE})?\s*(?:는|은)\s+", gap):
            # Both named venues are objects of one shared leadership verb.
            if re.match(r"^(?:과|와)\s+[^,.!?;]{1,60}[’'”\"]?[를을]\s*이끌고 있", tail):
                return 'current'
    else:
        gap = gap.strip(marks)
        gap = re.sub(r"^\([^()]+\)[’'”\"]?", "", gap).lstrip(marks)
        after_chef = clause[subject.end():]
        titled = re.match(rf'\s*{TITLE}', after_chef)
        if re.fullmatch(r'출신\s*', gap):
            return 'former'
        if re.fullmatch(rf"의\s*(?:{TITLE}\s*)?", gap) and (titled or re.search(TITLE, gap)):
            return 'current'
        if re.fullmatch(rf"[를을]\s*(?:{KO_WORK})\s*(?:(?:미쉐린\s*\d스타\s*)?{TITLE}\s*)?", gap):
            return 'current'
        if re.fullmatch(rf"의\s*{TITLE}[를을]\s*(?:{KO_WORK})\s*.{{0,32}}?의\s*", gap) and titled:
            return 'current'
        if re.fullmatch(r"(?:과|와)\s+[^,.!?;]{1,60}?[’'”\"]?[를을]\s*이끄는\s*", gap) and titled:
            return 'current'
        if re.fullmatch(r'은\s*[가-힣]{2,4}[·ㆍ]\s*', gap) and re.match(
                rf"^\s*{TITLE}[가는은이]\s*(?:{KO_WORK})\s*.{{0,15}}?(?:레스토랑|식당)", after_chef):
            return 'current'
        if re.fullmatch(r'[은는]\s*', gap) and re.match(
                rf'\s*{TITLE}[가는은이]\s*(?:헤드|총괄|수석)\s*셰프로\s*있', after_chef):
            return 'current'
        if (re.match(r'^(?:에서|의\s*셰프로서?)\s*(?:\w+\s+){0,3}일하는\b', tail) and titled):
            return 'current'
    return None


def _location(clause, employer, relation):
    before, after = clause[:employer.start()], clause[employer.end():]
    cities = set(site_publish.CITIES) | OTHER_CITIES
    cities.update(re.findall(r'[가-힣]{2,5}시(?=에서|의|\s)', clause))
    cities.update(label for label in re.findall(r"\b(?:in|at) ([A-Z][a-z]+(?: [A-Z][a-z]+)?)", after)
                  if label not in COUNTRIES)
    cities.update(re.findall(r'(?<![가-힣])(?:서울|광주|부산|인천|울산|대전|대구|뉴욕|도쿄)(?=에서|의|\s)', before))
    # Only the venue modifier or an opening locative can supply geography.
    def attached(label):
        escaped = re.escape(label)
        between = r"(?:\s+(?:한식당|식당|레스토랑|다이닝|restaurant|bistro|[‘'\"]))?\s*[‘'\"“]?"
        return (re.search(r'(?<!\w)' + escaped + r'(?:의|에서)?' + between + '$', before, re.I)
                or re.match(r"\s*[’'”\"]?[\s,]*(?:in\s+|[,（(]\s*)" + escaped + r'(?![\w-])', after, re.I)
                or relation == 'opening' and re.search(r'(?<!\w)' + escaped + r'에서\s+[^.!?;]{0,55}$', before))
    city = [value for value in sorted(cities) if attached(value)]
    country = {code for label, code in COUNTRIES.items() if attached(label)}
    return (city[0] if len(city) == 1 else None, next(iter(country)) if len(country) == 1 else None)


def _parse(entry, details):
    quote, chef, venue = (entry.get(k) for k in ('quote', 'chef', 'restaurant'))
    if not all(isinstance(s, str) and 2 <= len(s) <= 120 and s == s.strip()
               and name_key(s) not in lineage.GENERIC_NAMES and any(c.isalpha() for c in s) for s in (chef, venue)):
        return None
    if name_key(chef) == name_key(venue) or INSTITUTION.search(venue):
        return None
    subject_name = details.get('short_chef') or chef
    lang = lineage.language(quote)
    allowed = LABELS[entry['label']]
    for clause in re.split(r'(?<=[.!?。！？;；])\s+|\n', quote):
        subject, employer = lineage._literal(subject_name, clause), lineage._literal(venue, clause)
        if not subject or not employer or (subject.start() and re.match('[가-힣]', clause[subject.start() - 1])):
            continue
        # A completed pop-up can precede a different, permanent opening.
        work = re.sub(r'^.*?팝업을\s*마무리한\s+', '', clause, count=1) if allowed == ('opening',) else clause
        if TEMPORARY.search(work):
            continue
        cues = lineage.signals(work, lang)
        if any(cue['reject'].search(work) for cue in cues):
            continue
        subject, employer = lineage._literal(subject_name, work), lineage._literal(venue, work)
        if not subject or not employer:
            continue
        tail, venue_alias = _venue_suffix(work, employer, venue)
        # Strip a local venue gloss for the G3 verb/particle binder, keeping the original quote.
        binding = work[:employer.end()] + tail
        chef_alias = _bilingual(work[subject.end():], chef)
        if chef_alias:
            binding = re.sub(r'([（(])' + re.escape(chef_alias) + r'[）)]', '', binding, count=1)
        bs, be = lineage._literal(subject_name, binding), lineage._literal(venue, binding)
        relation = next((rel for rel in allowed if bs and be
                         and not (rel == 'current' and any(cue['former'].search(binding) for cue in cues))
                         and lineage._binds(binding, cues, rel, bs, be, details.get('role_text'))), None)
        if not relation and lang == 'ko':
            relation = _korean_relation(work, subject, employer, tail.lstrip())
        if relation not in allowed:
            continue
        raw = {k: details.get(k) for k in ('role_text', 'from_text', 'to_text', 'mentor_text', 'previous_venue_text')}
        if relation == 'training' and not raw.get('mentor_text'):
            mentor = re.search(r'(?:under|with)\s+([A-Z][a-z]+(?:[- ]+[A-Z][a-z]+)+)\s+(?:at|in)\b', work)
            if not mentor:
                mentor = re.search(r'(?<![가-힣])([가-힣]{2,4})\s*(?:셰프|쉐프)\s*(?:를|을)?\s*사사', work)
            if mentor:
                raw['mentor_text'] = mentor[1]
        dates = details.get('date_text')
        if dates and dates in work:
            if re.search(re.escape(dates) + r'\s*(?:까지|まで)', work):
                raw['to_text'] = dates
            else:
                raw['from_text'] = dates
        city, country = _location(work, employer, relation)
        row = dict.fromkeys(('role_id', 'role_text', 'valid_from', 'valid_to', 'mentor_id', 'previous_branch_id'))
        row.update(chef_text=chef, venue_text=venue, relation=relation, quote=quote,
                   city=city, country_code=country, bilingual_venue=venue_alias,
                   bilingual_name=chef_alias,
                   restaurant_wording=bool(OUTLET.search(work)), contest=bool(CONTEST.search(work)))
        lineage._fields(raw, work, row, subject, employer, cues)
        return row
    return None


def _spellings(name, alias=None):
    base = re.sub(r'\s*\([^()]+\)$', '', name)
    embedded = _bilingual(name[len(base):], base)
    return list(dict.fromkeys(filter(None, (name, base, alias, embedded))))


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
        if same_venue and (row['publisher_group'] == prev['publisher_group']
                or row['city'] and prev['city'] and name_key(row['city']) == name_key(prev['city'])
                or row.get('bilingual_venue') and prev.get('bilingual_venue')
                and name_key(row['bilingual_venue']) == name_key(prev['bilingual_venue'])):
            choices.add(bid)
    if len(choices) == 1 and (not mapped or choices <= mapped):
        return next(iter(choices)), site
    # Seed names which have no branch mapping yet can anchor a new branch only
    # when they point to one holder. Contests need evidence about the outlet.
    if published or len(matches) > 1 or (row['contest'] and not (matches or row['restaurant_wording'])):
        return None, site
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
    return branch, site


def _chef(conn, branch, row, gaz, supports, now):
    names = _spellings(row['chef_text'], row.get('bilingual_name'))
    redirects = identity.redirects(conn, 'chef')
    candidates = {identity.canonical(redirects, cid) for cid in identity.chef_candidates(conn, names, gaz)}
    keys = set().union(*(identity.candidate_keys(n) for n in names))
    previous = {identity.canonical(redirects, prior['chef_id']) for prior in supports.values()
                if (prior['branch_id'] == branch or prior['canonical_url'] == row['canonical_url']) and keys.intersection(
                    set().union(*(identity.candidate_keys(n) for n in _spellings(prior['chef_text'], prior.get('bilingual_name')))))}
    if len(previous) == 1 and not candidates - previous:
        return next(iter(previous))
    local = {identity.canonical(redirects, r[0]) for r in conn.execute(
        "SELECT chef_id FROM employment WHERE branch_id=? AND state<>'rejected'", (branch,))}
    if len(local & candidates) == 1:
        return next(iter(local & candidates))
    if len(candidates | previous) > 1:
        return None
    exact = stable_id('chf', 'career', name_key(row['chef_text']), branch)
    if candidates == {exact} and conn.execute(
            "SELECT 1 FROM ai_promotion_identity WHERE kind='chef' AND entity_id=?", (exact,)).fetchone():
        return exact
    # The G3 resolver can also use a dated native stint to disambiguate.
    # Bilingual evidence is retained on this support. There is no need to
    # redirect another identity to publish it.
    chef, plausible = identity.resolve_chef(conn, {**row, 'bilingual_name': None}, branch, gaz, row, None, now,
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


def _promote(conn, seed, raw_dir, log, now):
    from .review import Site

    state = seed.get(KEY, {})
    supports = state.get('supports', {})
    denied = tombstones(log) | set(state.get('withdrawn', ()))
    gaz = Gazetteer(conn)
    entries = sorted(annotations(seed), key=lambda entry: entry[1] is None)
    touched = {}
    invalid = set(supports) - {entry.get('review_id') for _, _, entry in entries}
    invalid.update(set(supports) & denied)
    for _, before, entry in entries:
        rid = entry.get('review_id')
        if rid in touched:
            _move(seed, before, touched[rid], entry)
            continue
        if (not rid or rid in denied or entry.get('label') not in LABELS
                or entry.get('badge') != BADGE or entry.get('provenance') != 'ai_reviewed'
                or not site_publish.safe_url(entry.get('src')) or not site_publish._public_quote(entry.get('quote'))):
            continue
        original = _original(conn, entry, raw_dir)
        if original is None:
            invalid.add(rid)
            continue
        details, source = original
        row = _parse(entry, details)
        if not row:
            invalid.add(rid)
            continue
        row.update(source, src=entry['src'], assertion_id=rid, ai_reviewed=True)
        # Text retention must not lower a previously checked proof's quality.
        old = supports.get(rid)
        if old and old['quote'] == row['quote'] and old['src'] == row['src'] and not row['snapshot_checked']:
            for key in ('independent_eligible', 'content_hash', 'body_fingerprint', 'publisher_group', 'origin_group', 'snapshot_checked'):
                row[key] = old[key]
        names = _spellings(row['venue_text'], row.get('bilingual_venue'))
        if {name_key(n) for n in names} & {
                name_key(n) for tomb in seed.get(site_publish.TOMBSTONES, ())
                for n in (*tomb.get('aliases', ()), tomb.get('creation_event', {}).get('name') or '')}:
            continue
        site = Site(seed)
        matches = _site_matches(seed, site, gaz, names)
        conn.execute('SAVEPOINT promote_review')
        branch, by_kind = _branch(conn, seed, matches, row, names, gaz, supports, now)
        chef = _chef(conn, branch, row, gaz, supports, now) if branch else None
        if not chef:
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
        touched[rid] = dest
        _move(seed, before, dest, entry)
    if supports:
        state['supports'] = supports
        seed[KEY] = state
    if not state:
        return
    discarded = (invalid - touched.keys()) & supports.keys()
    if discarded:
        withdraw(seed, discarded, now, tombstone=False)
        prune_identities(conn, seed)
    # A venue can resolve an annotation independently of whether its claim
    # qualifies for a graph edge. Chef-only matches cannot move it.
    site = Site(seed)
    for entry in list(seed.get('ai_reviewed_unlinked', ())):
        if not entry.get('restaurant') or entry.get('review_id') in denied:
            continue
        matches = _site_matches(seed, site, gaz, _spellings(entry['restaurant']))
        if len(matches) == 1:
            _move(seed, None, next(iter(matches.values()))[1], entry)


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

    names = set().union(*(identity.candidate_keys(n) for n in _spellings(row['chef_text'], row.get('bilingual_name'))))
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
