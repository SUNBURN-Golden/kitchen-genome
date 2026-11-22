"""Offline language routing, exact-span validation and persistence for career evidence."""
from dataclasses import dataclass
from functools import lru_cache
import json
from pathlib import Path
import re
import unicodedata
from urllib.parse import urlsplit

import yaml

from kg_ai.article import article_key, digest
from kg_ai.deepseek import DeepSeekError, TaskReject, failure_code, stored_error, NON_FALLBACK_CODES
from kg_daemon import claims, ontology
from kg_daemon.collect_bridge import persist_career_span, persist_career_claim
from kg_daemon.db import ROOT, stable_id
from . import discover, evaluate, lineage_identity as identity
from .net import CFG, host_of, is_banned, is_search_host
from .targets import name_key

VERSION = 'career-span-1'
PROSE = {'html', 'jsonld', 'browser', 'pdf', 'ocr'}
WINDOW_CHARS = 6000
OVERLAP = 240
FIELDS = ('assertion_id', 'fetch_id', 'canonical_url', 'publisher_group', 'origin_group', 'independent_eligible',
          'published_at', 'content_hash', 'quote_key', 'span_start', 'span_end', 'quote', 'extractor_version',
          'model', 'request_id', 'language', 'chef_text', 'venue_text', 'relation', 'role_text', 'role_id',
          'from_text', 'to_text', 'valid_from', 'valid_to', 'state', 'chef_id', 'branch_id', 'previous_branch_id',
          'mentor_id', 'mentor_text', 'previous_venue_text', 'claim_id', 'evidence_id', 'employment_id')
RELATIONS = ('current', 'former', 'training', 'opening')
GENERIC_NAMES = {'chef', 'thechef', 'souschef', 'restaurant', 'therestaurant', 'kitchen', '셰프', '쉐프',
                 '요리사', '식당', '레스토랑', 'chefkoch', '料理長', 'シェフ', 'レストラン'}
INSTRUCTION = (
    'Extract explicit chef careers. Return {"assertions":[...]} with one item per named chef and restaurant stint. '
    'Each item has chef_text, venue_text, relation (current, former, training, or opening), optional role_text, '
    'from_text, to_text, period_precision (year, month, day or null), quote, and source_span [start,end]. '
    'Copy all text fields exactly from the same short clause in the publisher text. Quote at most 200 Unicode characters. '
    'Offsets are half-open relative to the supplied window. Include the named chef, venue and career verb in the quote. '
    'Include dates only with the stint they date. Include optional bilingual_name only for an equivalent chef spelling '
    'explicitly written in parentheses next to the name. Include optional bilingual_venue on the same basis for venues. '
    'Include optional previous_venue_text only when the quote says '
    'the chef worked there before the venue of this assertion. Include optional mentor_text only when the quote names '
    'the trainer in a training assertion. Include city_text if the venue’s city is explicitly stated in the clause. '
    'Copy those fields verbatim too. Do not infer affiliations from an award, '
    'roster, visit, pop-up, collaboration, adjacent names, or plans. Do not translate. Return an empty list if needed.'
)
ROLE = {
    'executive_chef': r'executive chef|chef exécutif|cheffe exécutive|chef ejecutivo|cheffe? esecutiv[oa]|küchendirektor|総料理長|총괄 ?셰프',
    'head_chef': r'head chef|chef de cuisine|cheffe? (?:principal|de cuisine)|chef principal|jef[ea] de cocina|capo cuoco|küchenchef|chefkoch|chefköchin|料理長|헤드 ?셰프|수석 ?셰프',
    'sous_chef': r'sous[- ]chef|second de cuisine|segundo chef|vice chef|stellvertretende[rs]? küchenchef|副料理長|수셰프',
    'pastry_chef': r'pastry chef|chef pâtissier|cheffe pâtissière|chef pasteler[oa]|pasticcer[ea]|konditor(?:in)?|パティシエ|파티시에',
    'trainee': r'trainee|intern|stagiaire|aprendiz|practicante|tirocinante|auszubildende[rs]?|見習い|연수생|수습',
}
ROLE_LINK = r'\b(?:as|is|was|became|como|comm[e]|als|in qualità di|est|era|fue|ist|war)\b|として|を務め|셰프로|으로|으로서'
DATE_TOKEN = r'(?<!\d)(?:18|19|20)\d{2}(?:-\d{2}(?:-\d{2})?)?(?!\d)'
MONTHS = (
    'january february march april may june july august september october november december',
    'janvier février mars avril mai juin juillet août septembre octobre novembre décembre',
    'enero febrero marzo abril mayo junio julio agosto septiembre octubre noviembre diciembre',
    'gennaio febbraio marzo aprile maggio giugno luglio agosto settembre ottobre novembre dicembre',
    'januar februar märz april mai juni juli august september oktober november dezember',
)
MONTH_NUMBER = {name_key(month): index for months in MONTHS for index, month in enumerate(months.split(), 1)}
CITY_COUNTRY = {
    'seoul': 'KR', '서울': 'KR', 'busan': 'KR', '부산': 'KR', 'incheon': 'KR', '인천': 'KR',
    'paris': 'FR', 'london': 'GB', 'rome': 'IT', 'roma': 'IT', 'madrid': 'ES', 'barcelona': 'ES',
    'berlin': 'DE', 'copenhagen': 'DK', 'københavn': 'DK', 'newyork': 'US', 'tokyo': 'JP', '東京': 'JP',
}
WIRE = {
    'reuters': r'\bReuters\b|로이터|ロイター',
    'ap': r'Associated Press|\(AP\)|\bAP News\b|AP통신|共同通信AP',
    'afp': r'Agence France[- ]Presse|\bAFP\b',
    'yonhap': r'Yonhap News|연합뉴스',
    'kyodo': r'Kyodo News|共同通信',
}
ATTRIBUTION = (r'by |via |from |source\s*:|according to |translated from |originally published|republished from|'
               r'd.après |selon |traduit de |fuente\s*:|según |segundo |tradotto da |fonte\s*:|'
               r'übersetzt aus |quelle\s*:|提供|配信|翻訳|出典|번역|출처|제공')
UNTRUSTED = {'wikipedia.org', 'wikimedia.org', 'instagram.com', 'facebook.com', 'tiktok.com', 'x.com',
             'twitter.com', 'linkedin.com', 'catchtable.net', 'catchtable.co.kr', 'msn.com'}


@lru_cache(maxsize=1)
def lexicons():
    data = yaml.safe_load((Path(ROOT) / 'sources' / 'lineage_languages.yaml').read_text(encoding='utf-8'))
    return {lang: {key: frozenset(value.split()) if key == 'words' else re.compile(value, re.I)
                   for key, value in entry.items()} for lang, entry in data['languages'].items()}


def language(text, hint=''):
    vocab = lexicons()
    hint = (hint or '').lower().split('-')[0].split('_')[0]
    ko = len(re.findall('[가-힣]', text))
    ja = len(re.findall('[ぁ-ゖァ-ヺ]', text))
    words = re.findall(r'[^\W\d_]+', text.casefold())
    word_set = set(words)
    east = max(ko, ja)
    if east >= 3 and east * 3 >= len(re.findall(r'[A-Za-zÀ-ÖØ-öø-ÿĀ-ž]', text)):
        return 'und' if min(ko, ja) * 2 > east else ('ko' if ko > ja else 'ja')
    scores = sorted(((sum(word in hints['words'] for word in word_set)
                      + 4 * sum(hints[cue].search(text) is not None for cue in RELATIONS), code)
                     for code, hints in vocab.items() if 'words' in hints), reverse=True)
    (score, winner), (runner, _) = scores[:2]
    if score >= 3 and score >= runner + 2:
        return winner
    if hint in vocab and not (hint in ('ko', 'ja') and len(words) >= 7 and max(ko, ja) == 0):
        return hint
    return 'und'


def signals(text, lang):
    vocab = lexicons()
    return [vocab[lang]] if lang in vocab else list(vocab.values())


@dataclass(frozen=True)
class Window:
    start: int
    text: str
    language: str
    score: int


def windows(text, hint, gaz, contextual):
    """Rank bounded publisher paragraphs with career cues first."""
    from .deepseek import sanitize_before_store

    pending = None
    for block in re.finditer(r'[^\n]+', text):
        fragment = block.group()
        _, flags = sanitize_before_store(fragment)
        if flags['pii_redacted'] or flags['prompt_injection_detected']:
            continue
        lang = language(fragment, hint)
        cues = signals(fragment, lang)
        score = 2 * sum(value[rel].search(fragment) is not None for value in cues for rel in RELATIONS)
        culinary = any(value['culinary'].search(fragment) for value in cues)
        if not (score or culinary and (contextual or gaz.find(fragment))):
            continue
        for offset in range(0, len(fragment), WINDOW_CHARS - OVERLAP):
            part = fragment[offset:offset + WINDOW_CHARS]
            current = Window(block.start() + offset, part, lang, score)
            if pending and current.language == pending.language:
                between = text[pending.start + len(pending.text):current.start]
                size = current.start + len(current.text) - pending.start
                if not between.strip() and current.start >= pending.start + len(pending.text) and size <= WINDOW_CHARS:
                    pending = Window(pending.start, text[pending.start:pending.start + size], lang, pending.score + score)
                else:
                    yield pending
                    pending = current
            else:
                if pending:
                    yield pending
                pending = current
            if offset + len(part) == len(fragment):
                break
    if pending:
        yield pending


def _literal(value, text):
    if not isinstance(value, str) or not 1 < len(value) <= 120 or value.strip() != value:
        return None
    # CJK particles can adjoin names. Latin values need word boundaries.
    latin = r'[A-Za-zÀ-ÖØ-öø-ÿĀ-ž0-9_]'
    before = r'(?<!' + latin + ')' if re.fullmatch(latin, value[0]) else ''
    after = r'(?!' + latin + ')' if re.fullmatch(latin, value[-1]) else ''
    return re.search(before + re.escape(value) + after, text)


def _period(value, clause, employer, end=False, opening=False):
    if not isinstance(value, str) or len(value) > 45:
        return None
    hit = _literal(value, clause)
    if not hit:
        return None
    lead, tail = clause[:hit.start()], clause[hit.end():]
    distance = clause[min(hit.end(), employer.end()):max(hit.start(), employer.start())]
    if re.search(r'\b(?:won|awarded|interviewed|visited|featured|moved|joined|before|after|then)\b', distance, re.I):
        return None
    start_words = r'(?:from|since|since joining|between|de|depuis|desde|entre|dal|da|seit|von|ab)\s*$'
    end_words = r'(?:to|until|through|and|et|à|au|hasta|y|al|fino al|bis|und|[-–—~])\s*$'
    start_suffix, end_suffix = r'^\s*(?:부터|から|より)', r'^\s*(?:까지|まで)'
    opening_words = r'(?:in|on|en|nel|im|am)\s*$'
    single = bool(re.search(r'\b(?:in|en|im|nel)\s*$', lead, re.I)
                  or re.match(r'^(?:年に|년에|に|에(?!서))', tail))
    if end:
        if not (re.search(end_words, lead, re.I) or re.match(end_suffix, tail) or single):
            return None
    elif not (re.search(start_words, lead, re.I) or re.match(start_suffix, tail) or single
              or opening and re.search(opening_words, lead, re.I)
              or tail.startswith(('-', '–', '—', '~'))):
        return None
    if re.fullmatch(DATE_TOKEN, value):
        return ontology.parse_iso_date(value)
    east = re.fullmatch(r'(\d{4})[年년]\s*(?:(\d{1,2})[月월]\s*(?:(\d{1,2})[日일])?)?', value)
    if east:
        year, month, day = east.groups()
    else:
        day_first = re.fullmatch(r'(?:(\d{1,2})\.?\s+(?:de\s+)?)?([^\W\d_]+)\s+(?:de\s+)?(\d{4})', value, re.I)
        month_first = re.fullmatch(r'([^\W\d_]+)\s+(\d{1,2}),?\s+(\d{4})', value, re.I)
        if day_first:
            day, month, year = day_first.groups()
        elif month_first:
            month, day, year = month_first.groups()
        else:
            return None
        month = MONTH_NUMBER.get(name_key(month))
        if month is None:
            return None
    value = year + (f'-{int(month):02}' if month else '') + (f'-{int(day):02}' if day else '')
    return ontology.parse_iso_date(value)


def validate_item(raw, text, lang):
    if not isinstance(raw, dict):
        return {}, 'shape_invalid'
    quote = raw.get('quote')
    if not isinstance(quote, str) or not 1 <= len(quote) <= 200:
        return {}, 'quote_missing'
    span = raw.get('source_span')
    if (isinstance(span, list) and len(span) == 2 and all(type(i) is int for i in span)
            and 0 <= span[0] < span[1] <= len(text) and text[span[0]:span[1]] == quote):
        start = span[0]
    else:
        start = text.find(quote)
        if start < 0 or text.find(quote, start + 1) >= 0:
            return {}, 'quote_not_exact' if start < 0 else 'offset_ambiguous'
    row = {'quote': quote, 'span_start': start, 'span_end': start + len(quote), 'language': language(quote, lang)}
    relation = raw.get('relation')
    chef, venue = raw.get('chef_text'), raw.get('venue_text')
    for value in (chef, venue):
        if not isinstance(value, str) or name_key(value) in GENERIC_NAMES or not any(c.isalpha() for c in value):
            return row, 'unbound_career'
    # Reject evidence stitched from adjacent sentences. A named subject and both ends
    # of the relation must be present in a single clause of the exact quote.
    for clause in re.split(r'(?<=[.!?。！？;；])\s*|\n', quote):
        subject, employer = _literal(chef, clause), _literal(venue, clause)
        if not subject or not employer or name_key(chef) == name_key(venue):
            continue
        cues = signals(clause, row['language'])
        if relation not in RELATIONS or not any(cue[relation].search(clause) for cue in cues):
            continue
        if any(cue['reject'].search(clause) for cue in cues):
            continue
        if relation == 'current' and any(cue['former'].search(clause) for cue in cues):
            continue
        # A work verb must have the named venue as its object or be adjacent to
        # the venue's role. In a compound career sentence use the relevant clause.
        if not _binds(clause, cues, relation, subject, employer, raw.get('role_text')):
            continue
        row.update(chef_text=chef, venue_text=venue, relation=relation)
        _fields(raw, clause, row, subject, employer, cues)
        return row, None
    return row, 'unbound_career'


# ko/ja are verb-final: the named venue must precede the relation verb with a
# locative/object marker, and the named chef must remain the subject up to that verb.
EAST_LOCATIVE = re.compile(r'\s*(?:에서|에|で|に|を|를|을|의|の)')
EAST_SUBJECT_MARK = re.compile(r'[가이는은がは]')
# Longest first so 総料理長/副料理長 anchor as a whole instead of matching 料理長 inside them.
EAST_APPOSITIVE = tuple(sorted(('셰프', '쉐프', '요리사', '주방장', '총주방장', '料理長', '総料理長', '副料理長',
                                'シェフ', '料理人', 'パティシエ', '씨', '님', 'さん', '氏'), key=len, reverse=True))
EAST_PARTICLE = ('에서', 'で', 'に', 'を')
# 가/이/는/은/が/は are subject/topic particles only on a name-like token. These syllables
# instead open an ordinary word (이후/이전/이상/은퇴…), so they are not a new subject.
EAST_NON_PARTICLE = ('이후', '이전', '이상', '이내', '이외', '은퇴', '은신', '은폐', '은하', '은혜')
# An adnominal (relative) clause with its own subject directly modifying the venue: its
# subject marker belongs to the modifier, not to the main-clause career verb.
EAST_ADNOMINAL = re.compile(r'[^\s]{1,24}[가이는은がは]\s+[가-힣]{1,16}(?:는|은|던|한|할|된)\s+$')


def _eastern_subject_kept(clause, subject, employer, verb_end):
    """A compound -고/-し clause can hand the verb to a different subject. The named chef
    keeps the verb only while no later 가/이/는/은/が/は marks another name as the subject.
    A title/honorific appositive counts only when it attaches to the named chef itself."""
    chef = subject.group()
    masked = clause
    if subject.end() <= employer.start() and employer.end() <= verb_end:
        start = employer.start()
        modifier = EAST_ADNOMINAL.search(clause[:employer.start()])
        if modifier:
            start = modifier.start()
        masked = clause[:start] + ' ' * (employer.end() - start) + clause[employer.end():]
    for mark in EAST_SUBJECT_MARK.finditer(masked, subject.end(), verb_end):
        pos = mark.start()
        before = masked[:pos].rstrip()
        if not before:
            continue                                   # the chef's own subject/topic marker
        if masked[pos:pos + 3].startswith(EAST_NON_PARTICLE):
            continue                                   # word-internal syllable, not a particle on a name
        if before.endswith(chef) and (len(before) == len(chef) or not before[-len(chef) - 1].isalnum()):
            continue                                   # the same chef named again
        title = next((word for word in EAST_APPOSITIVE if before.endswith(word)), None)
        if title:
            stem = before[:-len(title)].rstrip()
            if stem.endswith(chef) and (len(stem) == len(chef) or not stem[-len(chef) - 1].isalnum()):
                continue                               # a title/honorific appositive of the named chef
            return False                               # a title on a different name is a new subject
        if any(before.endswith(particle) for particle in EAST_PARTICLE):
            continue                                   # a marker on the venue phrase, not a new subject
        return False
    return True


def _binds(clause, cues, relation, subject, employer, role):
    eastern = language(clause) in ('ko', 'ja')
    for cue in cues:
        for verb in cue[relation].finditer(clause):
            if subject.start() < employer.end() and subject.end() > employer.start():
                continue
            if eastern:
                # Bind the nearest relation verb after the venue: the venue carries a
                # locative/object marker and no intervening subject marker reassigns the chef.
                if not employer.end() <= verb.end() <= employer.end() + 66:
                    continue
                if not EAST_LOCATIVE.match(clause, employer.end()):
                    continue
                if re.search(r'[;；:：]|(?:ではなく|대신)', clause[employer.end():verb.end()]):
                    continue
                if not _eastern_subject_kept(clause, subject, employer, verb.end()):
                    continue
                return True
            if not subject.end() <= verb.start() <= employer.start():
                continue
            lead = clause[subject.end():verb.start()]
            if re.search(r',\s*(?:and|but)|\b(?:while|whereas|mais|pero|aber)\b', lead, re.I):
                continue
            between = clause[verb.end():employer.start()].strip()
            if re.fullmatch(r"(?:(?:the|le|la|il|lo|el|al|del|das|dem|der)\s*|l['’])?"
                            r"(?:restaurant|restaurante|ristorante|kitchen)?", between, re.I):
                return True
            if re.fullmatch(r"(?:.{0,32}\s)?(?:at|for|of|in|chez|à|de|du|en|del|presso|al|da|bei|im|von)"
                            r"\s*(?:the|le|la|il|el|das|dem|der|restaurant|restaurante|ristorante)?", between, re.I):
                # A preposition after a role can attach the verb to the venue.
                # Intervening career or visit clauses have their own subject/venue.
                if not re.search(r'\b(?:before|after|and|then|visit\w*|joined|opened|worked|for|at|chez|presso|bei)\b.*\s', between, re.I):
                    return True
            if relation == 'training' and re.fullmatch(r'.{1,45}\b(?:at|in|chez|presso|bei)\s*(?:the\s+)?', between, re.I):
                if not re.search(r'\b(?:before|after|visit\w*|then)\b', between, re.I):
                    return True
    return False


def _fields(raw, clause, row, subject, employer, cues):
    city = raw.get('city_text')
    if (_literal(city, clause) and name_key(city) in CITY_COUNTRY
            and re.match(r"\s*(?:[,（(]\s*|(?:in|en|à|a|im|bei)\s+)" + re.escape(city),
                         clause[employer.end():], re.I)):
        row.update(city=city, country_code=CITY_COUNTRY[name_key(city)])
    role = raw.get('role_text')
    hit = _literal(role, clause)
    if hit:
        before, after = clause[max(0, hit.start()-28):hit.start()], clause[hit.end():hit.end()+13]
        role_id = next((key for key, pattern in ROLE.items() if re.fullmatch(pattern, role, re.I)), None)
        stated_as = (re.search(r'(?:' + ROLE_LINK + r')\s+(?:(?:the|a|an|le|la|der|die)\s+)?$', before, re.I)
                     or re.match(r'^(?:として|を務め|로|으로)', after)
                     or (row['relation'] == 'current' and subject.end() < hit.start() < employer.start()
                         and re.search(r'^(?:\s+(?:at|of|de|du|at the|in|chez|im|bei)\b|로|として)', after, re.I)))
        if stated_as and (role_id or any(cue['culinary'].search(role) for cue in cues)):
            row.update(role_text=role, role_id=role_id)
    first = _period(raw.get('from_text'), clause, employer, opening=row['relation'] == 'opening')
    last = _period(raw.get('to_text'), clause, employer, end=True) if row['relation'] in ('former', 'training') else None
    if first and last and first[0] > last[0]:
        return
    if first:
        row.update(valid_from=first[0], from_text=raw['from_text'])
    if last:
        row.update(valid_to=last[0], to_text=raw['to_text'])
    alias = raw.get('bilingual_name')
    if (_literal(alias, clause) and name_key(alias) != name_key(row['chef_text'])
            and re.match(r'\s*[（(]\s*' + re.escape(alias) + r'\s*[）)]', clause[subject.end():])):
        row['bilingual_name'] = alias
    venue_alias = raw.get('bilingual_venue')
    if (_literal(venue_alias, clause) and name_key(venue_alias) != name_key(row['venue_text'])
            and re.match(r'\s*[（(]\s*' + re.escape(venue_alias) + r'\s*[）)]', clause[employer.end():])):
        row['bilingual_venue'] = venue_alias
    previous = raw.get('previous_venue_text')
    previous_hit = _literal(previous, clause)
    if (previous_hit and name_key(previous) != name_key(row['venue_text'])
            and any(_binds(clause, cues, rel, subject, previous_hit, None) for rel in ('former', 'training'))):
        marks = (mark for cue in cues for mark in cue['sequence'].finditer(clause))
        if any(min(previous_hit.end(), employer.end()) <= mark.start() <= max(previous_hit.start(), employer.start())
               and ((previous_hit.start() < employer.start()) != bool(re.match(r'after|après|después|dopo|nachdem', mark[0], re.I)))
               for mark in marks):
            row['previous_venue_text'] = previous
    mentor = raw.get('mentor_text')
    if (row['relation'] == 'training' and _literal(mentor, clause) and mentor != row['chef_text']
            and re.search(r'(?:under|with|auprès de|con|unter)\s+' + re.escape(mentor) + '|' + re.escape(mentor) + r'.{0,8}(?:師事|사사)', clause, re.I)):
        row['mentor_text'] = mentor


@lru_cache(maxsize=1)
def _publisher_settings():
    return discover.load_seeds()


def _source(conn, doc, text, tier):
    url = doc.canonical_url or doc.url
    host = host_of(url)
    parts = urlsplit(url)
    settings = _publisher_settings()
    deny = UNTRUSTED | set(settings.get('official_chain', {}).get('sns_hosts', ())) | set(settings.get('official_chain', {}).get('booking_hosts', ()))
    if (parts.scheme not in ('http', 'https') or not host or parts.username is not None
            or re.search(r'[\s\\#]', url) or is_banned(url) or is_search_host(host)
            or any(host == d or host.endswith('.' + d) for d in deny)
            or any(d in host for d in CFG.get('unreliable_hosts', ()))
            or tier in ('search_lead', 'social_lead', 'event_notice')):
        return None
    if conn.execute("""SELECT 1 FROM source s LEFT JOIN source_policy p USING(source_id)
        WHERE s.host=? AND (s.enabled=0 OR p.collect_permission IN ('manual_only','prohibited'))""", (host,)).fetchone():
        return None
    publishers = settings.get('publication', {}).get('press_hosts', {})
    entry = next((publishers[d] for d in sorted(publishers, key=len, reverse=True) if host == d or host.endswith('.' + d)), {})
    group = entry.get('group', host)
    # Credits survive translations, including same-publisher language editions.
    origins = {label for label, pattern in WIRE.items() if re.search(pattern, text, re.I)}
    if not origins:
        for link, label, _ in getattr(doc, 'links', ()):
            domain = host_of(link)
            linked = next((publishers[d]['group'] for d in sorted(publishers, key=len, reverse=True)
                           if domain == d or domain.endswith('.' + d)), domain)
            if domain != host and label and re.search(r'(?:' + ATTRIBUTION + r')\s*' + re.escape(label), text, re.I):
                origins.add(linked)
        for cfg in publishers.values():
            if re.search(r'(?:' + ATTRIBUTION + r')\s*' + re.escape(cfg['group']) + r'\b', text, re.I):
                origins.add(cfg['group'])
    owned = conn.execute('SELECT 1 FROM career_official_branch WHERE host=? LIMIT 1', (host,)).fetchone()
    official = tier == 'official' or bool(owned)
    known = entry.get('scope') in ('restaurant_reporting', 'original_food_reporting') or official
    return {'canonical_url': url, 'publisher_group': group, 'origin_group': min(origins) if origins else group,
            'independent_eligible': int(known and len(origins) < 2),
            'source_tier': 'official' if official else 'press', 'published_at': doc.published_at,
            'content_hash': digest(text)}


def _call(adapter, doc, window, snapshot):
    from .deepseek import article_data

    def validate(obj):
        if not isinstance(obj, dict) or not isinstance(obj.get('assertions'), list):
            raise TaskReject('shape_invalid')
        rows = []
        for raw in obj['assertions']:
            row, reason = validate_item(raw, window.text, window.language)
            rows.append({'row': row, 'reason': reason})
        return rows

    data = article_data({'url': doc.canonical_url or doc.url, 'published_at': doc.published_at,
                         'snapshot_key': snapshot, 'source_start': window.start, 'language': window.language}, window.text)
    return adapter.client.call(VERSION, INSTRUCTION, data, validate, max_tokens=4096, list_key='assertions')


def _review(conn, row, reason, now, **details):
    claims.queue_review(conn, reason, 'career_assertion', row['assertion_id'],
                        {'fetch_id': row['fetch_id'], 'url': row['canonical_url'],
                         'span': [row.get('span_start'), row.get('span_end')], **details},
                        dedupe_key=row['assertion_id'] + ':' + reason, now=now)


def _store(conn, fetch, text, metadata, found, gaz, focus, now):
    found.sort(key=lambda item: (bool(item[1]), item[0].get('relation') not in ('current', 'opening'),
                                 item[0].get('span_start', -1)))
    document_people = {}
    seen = set()
    for parsed, reason, model, request, token in found:
        row = dict.fromkeys(FIELDS)
        row.update(metadata, **parsed, state='review', fetch_id=fetch['fetch_id'], extractor_version=VERSION,
                   model=model, request_id=request, fetched_at=fetch['fetched_at'])
        row['language'] = row['language'] or 'und'
        identity_fields = tuple(str(row.get(k) or '') for k in
                                ('span_start', 'span_end', 'chef_text', 'venue_text', 'relation', 'role_text', 'valid_from', 'valid_to'))
        aid = stable_id('career', row['canonical_url'], row['content_hash'], VERSION, *identity_fields, reason or '', token if reason else '')
        if aid in seen:
            continue
        seen.add(aid)
        row['assertion_id'] = aid
        row['quote_key'] = digest(name_key(row['quote'])) if row.get('quote') else None
        existing = conn.execute('SELECT chef_id,state FROM career_assertion WHERE assertion_id=?', (aid,)).fetchone()
        if existing:
            if existing['state'] == 'source_claim':
                document_people[name_key(row['chef_text'])] = existing['chef_id']
            continue
        if reason:
            _review(conn, row, reason, now)
        else:
            _resolve(conn, fetch, text, row, gaz, focus, document_people, now)
        conn.execute(f'INSERT INTO career_assertion({",".join(FIELDS)}) VALUES ({",".join("?" for _ in FIELDS)})',
                     tuple(row.get(field) for field in FIELDS))


def _resolve(conn, fetch, text, row, gaz, focus, people, now):
    chef_name, venue = row['chef_text'], row['venue_text']
    key = name_key(chef_name)
    provenance = row['publisher_group'], row['canonical_url'], now
    branch, venues = identity.resolve_branch(conn, venue, row['quote'], gaz, focus, *provenance,
                                             create=not row.get('bilingual_venue'), city=row.get('city'), country=row.get('country_code'))
    if branch is None and not venues and row.get('bilingual_venue'):
        branch, venues = identity.resolve_branch(conn, row['bilingual_venue'], row['quote'], gaz, focus, *provenance,
                                                 city=row.get('city'), country=row.get('country_code'))
    if not branch:
        _review(conn, row, 'ambiguous_career_branch', now, branch_ids=sorted(venues), venue=venue)
        return
    row['branch_id'] = branch
    row['evidence_id'] = persist_career_span(conn, fetch, text, row, now)
    chef, candidates = people.get(key), set()
    if chef is None:
        chef, candidates = identity.resolve_chef(conn, row, branch, gaz, row, row['evidence_id'], now)
    if not chef:
        _review(conn, row, 'career_homonym', now, chef_ids=sorted(candidates), chef=chef_name)
        return
    people[key] = chef
    row['chef_id'] = chef
    row['state'] = 'source_claim'
    if row.get('previous_venue_text'):
        row['previous_branch_id'], _ = identity.resolve_branch(
            conn, row['previous_venue_text'], row['quote'], gaz, focus, *provenance, create=False)
    if row.get('mentor_text'):
        mentors = identity.chef_candidates(conn, [row['mentor_text']], gaz)
        mentors.discard(chef)
        if len(mentors) == 1:
            row['mentor_id'] = next(iter(mentors))
    row['claim_id'], row['employment_id'] = persist_career_claim(conn, row, now)
    alias = row.get('bilingual_name')
    if alias:
        _store_alias(conn, row, 'chef', chef, alias, now)
        identity.index_name(conn, chef, alias)
        people[name_key(alias)] = chef
    if row.get('bilingual_venue'):
        for label in (venue, row['bilingual_venue']):
            _store_alias(conn, row, 'branch', branch, label, now)
    _check_conflicts(conn, row, now)


def _store_alias(conn, row, kind, entity_id, spelling, now):
    lang = 'ko' if re.search(r'[가-힣]', spelling) else language(spelling)
    if lang == 'und' and re.search(r'[A-Za-z]', spelling):
        lang = 'und-Latn'
    claim = claims.create_claim(conn, 'alias', 'source_claim', 'collect:career-span',
                               claim_id=stable_id('clm', row['assertion_id'], 'alias', spelling), now=now)
    claims.link_evidence(conn, claim, row['evidence_id'])
    conn.execute("""INSERT OR IGNORE INTO alias(alias_id,entity_type,entity_id,alias,kind,lang,claim_id,created_at)
        VALUES (?,?,?,?,'romanization',?,?,?)""",
                 (stable_id('alias', kind, entity_id, spelling), kind, entity_id, spelling, lang, claim, now))


def _check_conflicts(conn, row, now):
    if row['relation'] not in ('current', 'former'):
        return
    redirect = identity.redirects(conn, 'chef')
    owner = identity.canonical(redirect, row['chef_id'])
    ids = {owner, row['chef_id'], *(key for key in redirect if identity.canonical(redirect, key) == owner)}
    for chef in ids:
        others = conn.execute("""SELECT * FROM career_assertion WHERE chef_id=? AND branch_id=?
            AND state='source_claim' AND relation IN ('current','former')""", (chef, row['branch_id']))
        for old in others:
            # Different jobs/roles and retrospective reporting can coexist.
            # A shared start and role is a concrete assertion about the same stint.
            if not (row.get('role_id') and row['role_id'] == old['role_id'] and (
                    row.get('valid_from') and row['valid_from'] == old['valid_from']
                    or row.get('valid_to') and row['valid_to'] == old['valid_to'])):
                continue
            precision_conflict = False
            for field in ('valid_from', 'valid_to'):
                a, b = row.get(field), old[field]
                if a and b:
                    first, second = sorted((ontology.date_range(a), ontology.date_range(b)))
                    precision_conflict |= first[1] < second[0]
            current, former = (row, old) if row['relation'] == 'current' else (old, row)
            ended_while_current = (current['relation'] == 'current' and former['relation'] == 'former'
                                   and former['valid_to'] and current['published_at']
                                   and ontology.date_range(former['valid_to'])[1] < current['published_at'][:10])
            if precision_conflict or ended_while_current:
                claims.open_conflict(conn, 'employment', row['employment_id'], 'period', old['claim_id'], row['claim_id'], now)
                _review(conn, row, 'career_conflict', now, other_assertion=old['assertion_id'])


def process(conn, doc, gaz, tier, adapter, now, *, context=False, focus=(), required=False):
    """Return completion; transient model failures leave the existing queue resumable."""
    if not doc.text:
        return True
    fetch = conn.execute('SELECT * FROM fetch_record WHERE fetch_id=?', (doc.fetch_id,)).fetchone()
    if not fetch or fetch['method'] not in PROSE or fetch['outcome'] != 'ok' or not fetch['text_ref']:
        return True
    if conn.execute('SELECT 1 FROM career_document WHERE fetch_id=? AND extractor_version=?',
                    (doc.fetch_id, VERSION)).fetchone():
        return True
    text = unicodedata.normalize('NFC', doc.text)
    lang = language(text, getattr(doc, 'language', ''))
    metadata = _source(conn, doc, text, tier)
    if not metadata:
        with conn:
            _finish(conn, doc, lang, {'content_hash': digest(text)}, 'prefilter_miss', text)
        return True
    selected = sorted(windows(text, lang, gaz, context or bool(focus) or metadata['source_tier'] == 'official'),
                      key=lambda window: (-window.score, window.start))
    if not selected:
        with conn:
            _finish(conn, doc, lang, metadata, 'prefilter_miss', text)
        return True
    if adapter is None or not hasattr(adapter, 'extract_lineage'):
        return not required
    adapter.client.use_task(VERSION)
    adapter.client.budget.workload = 'fresh'
    if adapter.client.status != 'enabled':
        return not required and adapter.client.status == 'disabled'
    snapshot = article_key(metadata['canonical_url'], text)
    found = []
    failed = False
    for window in selected:
        try:
            rows = adapter.extract_lineage(doc, window, snapshot)
        except DeepSeekError as exc:
            code = failure_code(stored_error(exc))
            if code not in {*NON_FALLBACK_CODES, 'attempts_exhausted', 'task_previously_rejected',
                            'cached_result_purged', 'input_limit', 'response_limit', 'invalid_response_or_usage'}:
                return False
            failed = True
            found.append(({}, code, None, None, str(window.start)))
            # The client keeps parser/provider lane stops across documents.
            if adapter.client.stopped:
                break
            continue
        meta = adapter.client.last_meta
        for index, result in enumerate(rows):
            row = dict(result['row'], language=result['row'].get('language', window.language))
            if 'span_start' in row:
                row['span_start'] += window.start
                row['span_end'] += window.start
            found.append((row, result['reason'], meta.get('model'), meta.get('request_id'), f'{window.start}:{index}'))
    from .global_queue import write
    with write(conn):
        _store(conn, fetch, text, metadata, found, gaz, set(focus), now)
        _finish(conn, doc, lang, metadata, 'failed' if failed else 'completed', text)
        if found:
            from .scoreboard import record_career_outcomes

            record_career_outcomes(conn, doc.fetch_id, VERSION, now)
    return True


def _finish(conn, doc, lang, metadata, disposition, text):
    shingles = evaluate.shingles(text[:3000].casefold()) if disposition != 'prefilter_miss' else ()
    fingerprint = json.dumps(sorted(digest(shingle)[:12] for shingle in shingles))
    conn.execute('INSERT OR IGNORE INTO career_document VALUES (?,?,?,?,?)',
                 (doc.fetch_id, VERSION, metadata['content_hash'], lang, fingerprint))
    conn.execute('INSERT OR IGNORE INTO model_document_sample VALUES (?,?,?)', (doc.fetch_id, VERSION, disposition))
