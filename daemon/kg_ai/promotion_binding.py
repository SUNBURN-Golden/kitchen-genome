"""Bind AI-reviewed chef and outlet names to verbs in the approved text."""
from dataclasses import dataclass
import re

from kg_collect import lineage, site_publish
from kg_collect.targets import name_key

LABELS = {'employment_claim': ('former', 'current'), 'career': ('former', 'training', 'current'),
          'mentorship_claim': ('training',), 'opening': ('opening',)}
INSTITUTION = re.compile(
    r'호텔|리조트|소사이어티|그룹|아카데미|대학교|대회|팝업|페스티벌|백화점|연구소|연구원|재단|협회|'
    r'브랜드|방송|예능|유튜브|채널|마스터\s*셰프|흑백요리사|더본코리아|오덴세|라\s*리스트|퀴진케이|'
    r'주식회사|법인|\b(?:hotels?|resorts?|group|university|academy|competition|festival|gala|pop[- ]?up|'
    r'catering|corporation|inc|ltd|llc|department store|research institute|foundation|association|'
    r'brand|tv|netflix|youtube|channel|masterchef|culinary class wars|odense|la liste|cuisine\.? k)\b', re.I)
TEMPORARY = re.compile(r'게스트\s*(?:셰프|시프트)|객원\s*셰프|한시적|단기\s*(?:근무|셰프)|일일\s*셰프|'
                       r'초청\s*셰프|임시\s*(?:셰프|근무)|하루\s*동안|\b(?:guest|visiting|temporary)\b', re.I)
POPUP = re.compile(r'팝업|\bpop[- ]?up\b', re.I)
EXCLUDED_POPUPS = frozenset(map(name_key, ('오닐', 'ONIL', "O'Nil", 'DOORI', '두리', '주052', 'JU052', 'JOO052')))
REJECT = re.compile(r'가오픈|개업\s*연기|(?:운영|오픈|개업|개점|총괄|이끌)\w*\s*지\s*않|'
                    r'열지\s*않|(?:운영|개업|개점)\s*할\s*예정|\b(?:soft opening|will|not|never|'
                    r'plans? to|expected to|scheduled to)\b', re.I)
NON_OUTLET = re.compile(r'(?:소비재|테이블웨어|분식)\s*브랜드|브랜드\s*$|'
                        r'(?:tableware|consumer|retail) brand\s*$|(?:\(주\)|㈜)\s*$', re.I)
CONTEST = re.compile(r'대회|참가|출품|우승|수상|선정|\d위|상\(.*Award|\b(?:contest|competition|award)\b', re.I)
OUTLET = re.compile(r'레스토랑|식당|다이닝|부티크|\b(?:restaurant|bistro|eatery)\b', re.I)
KO_WORK = (r'이끄는|이끈|이끌고\s+있\w*|이끈다|운영(?:하는|한다|하고\s+있\w*|해\s*왔\w*)|'
           r'총괄(?:하는|한다|하고\s+있\w*)|맡(?:고\s+있\w*|은|는다)|근무(?:하는|한다| 중)|일하는|재직 중')
KO_OPEN = r'문을\s*열(?:었\w*|었다)|(?:개업|오픈|창업|개점)\s*했\w*|열었다'
KO_PAST = r'근무(?:했\w*|하며|한|하던)|일(?:했\w*|하던)|몸담았\w*|지냈\w*|경력을\s*쌓(?:았\w*|으?며|은\s*뒤)|거쳤\w*|(?:으로|로)\s*합류했\w*'
FORMER_TITLE = re.compile(r'(?<![가-힣])전\s+(?=[가-힣])|출신|이전\s+|역임|예전에|과거에|전직|전임|운영했던|이끌었던|총괄했던|'
                          r'\b(?:former|previous|ex[- ]|formerly)\b', re.I)
TITLE = r'(?:(?:공동|총괄|오너|헤드|수석|책임|수|페이스트리)\s*)?(?:셰프|쉐프|주방장|요리사)'
EN_TITLE = r'(?:chef de cuisine|(?:(?:executive|head|owner|pastry)\s+|sous[- ])?chef)'
OTHER_ACTOR = re.compile(rf'(?<![가-힣])[가-힣]{{2,5}}\s*(?:{TITLE}|대표|사장|님)\s*[가는은이](?=\s|$)')
EVENT_ROSTER = re.compile(r'촬영|사진\s*[:=]|출처\s*[:=]|제공\s*[:=]|후원|협찬|래플|시상식|'
                          r'참가자|심사위원|게스트|임원|승진|선임|\b(?:photo|courtesy|credit[s]?|'
                          r'sponsor[s]?|contestants?|judges?|guests?|line[- ]?up|festival|gala)\b', re.I)
ROSTER = re.compile(r'예약\s*수\s*기준|(?:레스토랑|식당)\s*(?:및|&|[/·와과])\s*(?:담당\s*)?셰프|'
                    r'(?:레스토랑|식당)별\s*(?:담당\s*)?셰프|'
                    r'셰프\s*(?:별|및|&|[/·와과])\s*(?:운영\s*|대표\s*)?(?:레스토랑|식당)|'
                    r'\b(?:restaurants? and (?:their )?chefs|chefs and (?:their )?restaurants|'
                    r'restaurant[- ]chef (?:pairings|roster))\b', re.I)
TABLE_HEADER = re.compile(r'이름\s*\|[^|.!?]{1,30}\|\s*운영\s*레스토랑|'
                          r'(?:chef|name)\s*\|[^|.!?]{1,30}\|\s*(?:current )?restaurant', re.I)
SENTENCE_BREAK = re.compile(r'[(（][^()（）]*[)）]|(?<=[.!?。！？;；])\s+|\n+')
LATIN = r"[A-Za-zÀ-ÖØ-öø-ÿĀ-ž][A-Za-zÀ-ÖØ-öø-ÿĀ-ž .’'\-]*"
COUNTRIES = {'한국': 'KR', '대한민국': 'KR', 'South Korea': 'KR', '미국': 'US', 'United States': 'US',
             '일본': 'JP', 'Japan': 'JP', '프랑스': 'FR', 'France': 'FR', '영국': 'GB', 'United Kingdom': 'GB',
             '이탈리아': 'IT', 'Italy': 'IT', '독일': 'DE', 'Germany': 'DE', '스페인': 'ES', 'Spain': 'ES',
             '싱가포르': 'SG', 'Singapore': 'SG', '아랍에미리트': 'AE', 'United Arab Emirates': 'AE',
             '캐나다': 'CA', 'Canada': 'CA', '호주': 'AU', 'Australia': 'AU'}
OTHER_CITIES = {'두바이', 'Dubai', '샌프란시스코', 'San Francisco', '파리', '런던', '홍콩', 'Hong Kong',
                '싱가포르', 'Singapore'}


@dataclass(frozen=True)
class Endpoint:
    hit: re.Match
    start: int
    end: int
    alias: str | None = None


def _literals(name, text):
    if not name:
        return ()
    leading = r'(?<![\w])' if name[0].isalnum() else ''
    trailing = r'(?![A-Za-zÀ-ÖØ-öø-ÿĀ-ž0-9_])'
    if re.match('[가-힣]', name[-1]):
        trailing = r'(?=(?:에서(?:도|는)?|에게|으로|에는|까지|서|[은는이가을를의과와에도로께])?(?!\w)|\s*(?:셰프|쉐프)\b)'
    return re.finditer(leading + re.escape(name) + trailing, text, re.I)


def _bilingual(tail, name):
    match = re.match(r"\s*[’'”\"]?\s*[(（]([^()（）]{2,80})[)）]", tail)
    if not match:
        return None
    alias = match[1].strip()
    if ((re.search('[가-힣]', name) and re.fullmatch(LATIN, alias))
            or (re.search(LATIN, name) and re.fullmatch(r'[가-힣 ]+', alias))):
        return alias
    return None


def _spans(name, other, text):
    other_keys = {name_key(n) for n in _spellings(other)}
    seen = set()
    for spelling in reversed(_spellings(name)):
        for hit in _literals(spelling, text):
            start, end = hit.span()
            alias = _bilingual(text[end:], hit[0])
            if alias and name_key(alias) not in other_keys:
                end += re.match(r"\s*[’'”\"]?\s*[(（][^()（）]+[)）]", text[end:]).end()
            else:
                alias = None
                # An English review can name the spelling inside 세옥(SEOK).
                if re.match(r'\s*[)）]', text[end:]):
                    base = re.search(r"([가-힣]{2,}(?: [가-힣]+){0,2})\s*[(（]\s*$", text[:start])
                    if base and re.fullmatch(LATIN, hit[0]):
                        # Use the adjacent Korean name token. Never absorb a
                        # restaurant descriptor or the other typed endpoint.
                        alias = base[1].split()[-1]
                        if name_key(alias) not in other_keys:
                            start = base.end(1) - len(alias)
                            end += re.match(r'\s*[)）]', text[end:]).end()
                        else:
                            alias = None
            if (start, end) not in seen:
                seen.add((start, end))
                yield Endpoint(hit, start, end, alias)


def _sentences(text):
    start = 0
    for match in SENTENCE_BREAK.finditer(text):
        if match[0][0] in '(（':
            continue
        if text[start:match.start()].strip():
            yield start, match.start()
        start = match.end()
    if text[start:].strip():
        yield start, len(text)


def _context_clauses(context, quote):
    text, offset = context
    start = max(0, offset - 1600)
    text, offset = text[start:offset + len(quote) + 500], offset - start
    spans = list(_sentences(text))
    for i, (start, end) in enumerate(spans):
        if start <= offset and offset + len(quote) <= end:
            previous = text[spans[i - 1][0]:start].strip() if i else ''
            following = text[spans[i + 1][0]:spans[i + 1][1]] if i + 1 < len(spans) else ''
            return previous, text[start:offset], text[offset + len(quote):end], following
    return '', '', '', ''


def _bind_text(work, subject, employer):
    """Remove local glosses and name quotes while preserving the typed offsets."""
    subjects = []
    pos = 0
    binding = ''
    for span in sorted(filter(None, (subject, employer)), key=lambda s: s.start):
        marks = "‘’'\"“”「」『』"
        left = span.start - int(span.start > pos and work[span.start - 1] in marks)
        right = span.end + int(span.end < len(work) and work[span.end] in marks)
        binding += work[pos:left]
        subjects.append((span, len(binding)))
        binding += span.hit[0]
        pos = right
    binding += work[pos:]
    matches = {id(span): re.compile(re.escape(span.hit[0])).match(binding, start) for span, start in subjects}
    return binding, matches.get(id(subject)), matches[id(employer)]


def _reject_reason(work, lang):
    if TEMPORARY.search(work) or POPUP.search(work):
        return 'temporary'
    if REJECT.search(work) or any(cue['reject'].search(work) for cue in lineage.signals(work, lang)):
        return 'reject_cue'
    return None


def _venue_reason(venue, alias=None):
    names = _spellings(venue, alias)
    if _blocked_name(names, EXCLUDED_POPUPS) or any(POPUP.search(n) for n in names):
        return 'temporary'
    if any(INSTITUTION.search(n) for n in names):
        return 'institution'
    return None


def _opening_object(tail):
    return re.match(rf'^[을를]\s*(?:새로\s+|정식(?:으로)?\s+)?(?:{KO_OPEN})', tail)


def _korean_venue_relation(work, employer):
    tail = work[employer.end():].lstrip()
    if (_opening_object(tail)
            or re.match(rf'^[이가은는]\s+(?:[^,.!?;]{{1,55}}?에(?:서)?\s+)?문을\s*열었다', tail)):
        return 'opening'
    if re.match(rf'^(?:[을를]\s*|의\s*주방을\s*)(?:{KO_WORK})', tail):
        return 'current'
    if re.match(rf'^에서(?:도)?\s*(?:(?:\d{{4}}년(?:까지)?|{TITLE}(?:로|으로))\s+)?(?:{KO_PAST})', tail):
        return 'former'
    if re.match(rf'^의\s*(?:총괄\s*)?{TITLE}\s*(?:로|으로)\s*합류', tail):
        return 'former'

    # Each named outlet must be an object of the final, shared predicate.
    quoted = r'[‘“\'"][^‘’“”\'".!?;]{1,65}[’”\'"](?:\s*\([^()]{1,65}\))?'
    item = r"(?:[^,!?.;|'“‘\"]{0,42}?)" + quoted
    rest = re.match(rf'^(?:\s*(?:[과와,]|및)\s*{item}){{1,3}}\s*(?:등\s*)?(?:을|를|에서)\s*', tail)
    if rest:
        if re.match(KO_WORK, tail[rest.end():]):
            return 'current'
        if re.match(KO_PAST, tail[rest.end():]):
            return 'former'
    if re.match(rf'^\s*(?:등(?:을|에서))\s*(?:{KO_WORK})', tail):
        return 'current'
    if re.match(rf'^\s*(?:등(?:을|에서))\s*(?:{KO_PAST})', tail):
        return 'former'
    return None


def _korean_relation(clause, subject, employer):
    before_venue, tail = clause[:employer.start()], clause[employer.end():].lstrip()
    after_chef = clause[subject.end():]
    titled = re.match(rf'\s*{TITLE}', after_chef)
    if subject.end() <= employer.start():
        gap = clause[subject.end():employer.start()]
        if OTHER_ACTOR.search(gap):
            return None
        lead = rf"\s*(?:{TITLE}|부부)?(?:\s*\(사진\))?\s*[가는은이]\s*"
        crew = rf"(?:\s*[·ㆍ와과]\s*[가-힣]{{2,4}}(?:\s*{TITLE})?)*"
        if re.match(rf'^\s*{crew}{lead}(?:주방을\s+)?(?:{KO_WORK})\s+', gap):
            return 'current'
        if re.match(rf'^\s*{crew}{lead}(?:[^,.!?;]{{0,48}}?\s)?(?:새로\s+)?(?:문을\s*)?연\s+', gap):
            return 'opening'
        if (re.search(r'(?:문을\s*연|개업한|오픈한|개점한)\s*$', clause[:subject.start()])
                and re.fullmatch(rf'\s*(?:{TITLE})?의\s*', gap)):
            return 'opening'
        relation = _korean_venue_relation(clause, employer)
        if relation and re.match(rf'^\s*{crew}{lead}', gap):
            return relation
        if re.fullmatch(r"\s*(?:호텔\s*신라\s*)?", gap) and re.match(TITLE, tail):
            return 'current'
        return None

    gap = clause[employer.end():subject.start()].strip()
    if (re.fullmatch(r'(?:의\s*)?출신', gap)
            or re.fullmatch(rf'(?:의\s*)?전\s+{TITLE}', gap)):
        return 'former'
    if (re.fullmatch(rf"(?:의\s*)?{TITLE}", gap) or re.fullmatch(r'(?:의\s*)?(?:한국인)?', gap) and titled
            or re.fullmatch(rf"에서\s*(?:현재\s*)?일하는\s*(?:한국인\s*)?", gap) and titled):
        return 'former' if FORMER_TITLE.search(clause[max(0, employer.start()-3):subject.start()]) else 'current'
    if re.fullmatch(rf"[를을]\s*(?:{KO_WORK})\s*(?:(?:미쉐린\s*\d스타\s*)?{TITLE}\s*)?", gap):
        return 'current'
    if re.fullmatch(rf"의\s*{TITLE}[를을]\s*(?:{KO_WORK})\s*.{{0,32}}?의", gap) and titled:
        return 'current'
    if re.fullmatch(r"(?:과|와)\s+[^,.!?;]{1,60}?[를을]\s*이끄는", gap) and titled:
        return 'current'
    if re.fullmatch(r'은\s*[가-힣]{2,4}[·ㆍ]\s*', gap) and re.match(
            rf"^\s*{TITLE}[가는은이]\s*(?:{KO_WORK})\s*.{{0,15}}?(?:레스토랑|식당)", after_chef):
        return 'current'
    if re.fullmatch(r'[은는]', gap) and re.match(
            rf'\s*{TITLE}[가는은이]\s*(?:헤드|총괄|수석)\s*셰프로\s*있', after_chef):
        return 'current'
    if re.search(r'(?:문을\s*연|개업한|오픈한|개점한)\s*$', before_venue) and re.fullmatch(
            rf'[은는이가]\s*[^,.!?;]{{0,85}}?', gap) and re.match(
                rf'\s*{TITLE}[가는은이]\s*(?:{KO_WORK})', after_chef):
        # This predicate proves who runs the venue; it does not name who opened it.
        return 'current'
    return None


def _english_relation(work, subject, employer):
    if subject.end() <= employer.start():
        gap = work[subject.end():employer.start()]
        if re.fullmatch(rf'\s*,?\s*(?:(?:is|serves as) (?:the |an? )?)?{EN_TITLE}\s+(?:at|of)\s+(?:the )?', gap, re.I):
            return 'current'
        if re.fullmatch(rf'\s*,?\s*(?:former|previous) {EN_TITLE}\s+(?:at|of)\s+(?:the )?', gap, re.I):
            return 'former'
        if re.fullmatch(r'\s+(?:runs|operates|leads|heads)\s+(?:the )?(?:restaurant\s+)?', gap, re.I):
            return 'current'
        if re.fullmatch(r'\s+launched (?:his|her|their) [^:,.!?;]{1,65}:\s*', gap, re.I):
            return 'opening'
        if re.fullmatch(r'\s+trained under [^,;.!?]{1,65}\bbefore working at\s+', gap, re.I):
            return 'former'
    else:
        gap = work[employer.end():subject.start()]
        if re.fullmatch(r'\s*,?\s*(?:(?:is )?run|led|operated) by (?:chef\s+)?', gap, re.I):
            return 'current'
        if re.fullmatch(r'\s*,?\s*(?:opened|founded|launched) by (?:chef\s+)?', gap, re.I):
            return 'opening'
    return None


def _roster(work, subject, employer, context):
    left, right = sorted((subject, employer), key=lambda span: span.start)
    between = work[left.end:right.start]
    tail = work[right.end:]
    title = rf'(?:{TITLE}|{EN_TITLE})'
    cell = (re.fullmatch(rf'[’”\'"]?\s*(?:{title}\s*)?[(（]\s*[‘“\'"]?\s*(?:{title}\s+)?', between, re.I)
            and re.match(rf'\s*[’”\'"]?\s*(?:{title}\s*)?[)）](?!\s*\w)', tail, re.I))
    previous, prefix, suffix, _ = _context_clauses(context, work) if context else ('', '', '', '')
    enclosing = prefix + work + suffix
    if cell:
        reason = _reject_reason(enclosing, lineage.language(enclosing))
        if reason or EVENT_ROSTER.search(enclosing):
            return None, False, reason or 'reject_cue'
        if ROSTER.search(enclosing):
            return 'current', not bool(ROSTER.search(work)), None
        if ROSTER.search(previous) and not EVENT_ROSTER.search(previous) and not _reject_reason(previous, lineage.language(previous)):
            return 'current', True, None
    # A named operating-restaurant column is required for profile table cells.
    if (subject.start < employer.start and work.count('|') == 2
            and re.fullmatch(r'\s*', work[:subject.start])
            and re.fullmatch(r'\s*\|[^|]+\|\s*', between)
            and re.fullmatch(r'\s*(?:\([^()]+\))?\s*', tail)
            and TABLE_HEADER.search(previous + prefix[-650:]) and not EVENT_ROSTER.search(work)
            and not _reject_reason(enclosing, lineage.language(enclosing))):
        return 'current', True, None
    return None, False, None


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
    resolved_city = city[0] if len(city) == 1 else None
    mapped = {'파리': 'FR', '런던': 'GB', **lineage.CITY_COUNTRY}
    return resolved_city, next(iter(country)) if len(country) == 1 else mapped.get(name_key(resolved_city or ''))


def _endpoint_reason(chef, venue, quote):
    if not chef:
        return 'no_chef'
    if not venue:
        return 'no_venue'
    if not all(isinstance(s, str) and 1 <= len(s) <= 120 and s == s.strip()
               and name_key(s) not in lineage.GENERIC_NAMES and any(c.isalpha() for c in s) for s in (chef, venue)):
        return 'invalid_endpoint'
    if len(chef) == 1 or len(venue) == 1 and not any(span.alias for span in _spans(venue, chef, quote)):
        return 'invalid_endpoint'
    if name_key(chef) == name_key(venue):
        return 'invalid_endpoint'
    if re.fullmatch(r'(?:[가-힣]{2,5}\s+)(?:\d+층\s+)?(?:식당|레스토랑)', venue):
        return 'invalid_endpoint'
    return _venue_reason(venue)


def _short_subject(chef, quote, context):
    """Return a full name and the short spelling attested in this discourse."""
    ko_short = re.fullmatch(r'([가-힣])\s*(?:셰프|쉐프)', chef)
    ko_full = re.fullmatch(r'[가-힣]{2,4}', chef)
    if ko_short or ko_full:
        family = chef[0]
        short = re.search(rf'(?<!\w){family}\s*(?:셰프|쉐프)', quote)
        if not (short or ko_short):
            return None, None
        full = set(re.findall(rf'(?<!\w)({family}[가-힣]{{1,3}})\s*(?:{TITLE})', context))
        titled = full | ({chef} if ko_full else set())
        for name in re.findall(rf'(?<!\w)(?:{TITLE})\s+({family}[가-힣]{{1,3}})(?![가-힣])', context):
            # In 셰프 손종원이 the attached particle is part of the token.
            full.add(name[:-1] if name[-1] in '이가은는' and name[:-1] in titled else name)
        if ko_full and full and full != {chef}:
            return None, short[0] if short else chef
    else:
        english = re.fullmatch(r'(?:Chef\s+)?(' + LATIN + r')', chef)
        if not english:
            return None, None
        tokens = english[1].split()
        family = tokens[-1]
        full_spans = list(_literals(chef, quote)) if len(tokens) > 1 and not chef.lower().startswith('chef ') else []
        short = next((hit for hit in _literals(family, quote)
                      if not any(span.start() <= hit.start() and hit.end() <= span.end() for span in full_spans)), None)
        if not short:
            return None, None
        title = r'[A-ZÀ-ÖØ-Þ][a-zà-öø-ÿ]+(?:[-’\'][A-ZÀ-ÖØ-Þa-zà-öø-ÿ]+)?'
        full = {n for n in re.findall(rf'(?<!\w)({title}\s+{re.escape(family)})(?!\w)', context)
                if n.split()[0] not in ('Chef', 'Mr', 'Ms', 'Dr')}
        if len(tokens) > 1 and full and full != {chef}:
            return None, family
    return (next(iter(full)) if len(full) == 1 else None), (short[0] if short else chef)


def _named_subject(text, name):
    for hit in _literals(name, text):
        before, after = text[:hit.start()], text[hit.end():]
        if (re.match(rf'\s*(?:{TITLE}|부부)?\s*[가는은이](?=\s|$)', after)
                or re.search(rf'(?:{TITLE}|\bchef)\s*$', before, re.I)
                or re.match(rf'\s*{TITLE}', after)
                or not before.strip() and re.match(r"\s+(?:opened|returned|runs|was|is|trained|worked)\b", after, re.I)):
            return True
    return False


def _english_elision(work, employer):
    lead, tail = work[:employer.start()], work[employer.end():]
    if re.fullmatch(r'\s*(?:(?:he|she|they)\s+)?(?:then\s+)?(?:opened|launched|founded|co-founded)\s+(?:the )?(?:restaurant )?', lead, re.I):
        return 'opening'
    if re.fullmatch(r'\s*(?:(?:he|she|they)\s+)?(?:runs|operates)\s+(?:the )?(?:restaurant )?', lead, re.I):
        return 'current'
    if re.fullmatch(r'\s*(?:he|she|they)\s+(?:previously\s+)?worked\s+(?:at|for|in)\s+(?:the )?', lead, re.I):
        return 'former'
    if re.fullmatch(r'\s*(?:he|she|they)\s+(?:trained|apprenticed)\s+at\s+(?:the )?', lead, re.I):
        return 'training'
    if re.fullmatch(r'\s*(?:the restaurant\s+)?', lead, re.I) and re.match(r'\s+opened its doors\b', tail, re.I):
        return 'opening'
    return None


def _competing_subject(text, name):
    if any(not any(_literals(name, m[0])) for m in OTHER_ACTOR.finditer(text)):
        return True
    # A continuation can start with an elided subject or a pronoun, or with
    # an adverbial. A marked person before the venue is a new actor.
    return bool(re.search(r"(?<!\w)[가-힣]{2,4}\s*[가이]\s+(?=(?:직접|새로|단독으로)\b)", text)
                or re.search(r'\b(?:his|her|their) (?:wife|husband|partner|colleague)\b', text, re.I)
                or re.search(r'\b(?:said|replied|according to)\b|라고\s*말|기자\s*[:：]', text, re.I))


def _different_actor(lead, name):
    if _competing_subject(lead, name):
        return True
    token = re.match(r'^\s*([가-힣]{2,4})[가는은이]\s+', lead)
    return bool(token and token[1] != name)


def _unique_antecedent(text, name):
    if not _named_subject(text, name) or _competing_subject(text, name):
        return False
    korean = re.findall(rf'(?<!\w)([가-힣]{{2,4}})\s+{TITLE}', text)
    english = re.findall(r'\b[Cc]hef\s+([A-Z][a-z]+\s+[A-Z][a-z]+)\b', text)
    return all(name_key(chef) == name_key(name) for chef in (*korean, *english))


def _source_attribution(entry, details, name, venue, *, venue_subject):
    context = details.get('context')
    if not context or name_key(details.get('chef_text') or '') != name_key(entry['chef']):
        return False
    previous, prefix, suffix, following = _context_clauses(context, entry['quote'])
    if _different_actor(prefix, name) or _competing_subject(suffix, name):
        return False
    if EVENT_ROSTER.search(prefix + suffix) or _reject_reason(prefix + suffix, lineage.language(prefix + suffix)):
        return False
    if _named_subject(prefix, name):
        return True
    # An adjacent sentence may explicitly identify the chef who presented
    # this outlet even when the restaurant is subject of the quoted sentence.
    if (_named_subject(following, name)
            and re.search(r'(?:선보인|개업한|오픈한)\s*(?:레스토랑|식당)(?:이다|입니다)|'
                          r'\b(?:opened|founded|launched) (?:it|the restaurant)\b', following, re.I)):
        return True
    if _unique_antecedent(previous, name) and not _reject_reason(previous, lineage.language(previous)):
        if not venue_subject or any(_literals(venue, previous)):
            return True
    # “Together” and “the couple” are grounded only by a nearby explicit
    # pair of chefs operating this same outlet.
    if re.search(r'부부|두\s*셰프|함께', previous + prefix + entry['quote']):
        body, offset = context
        passage = body[max(0, offset - 620):offset]
        nearby = ''
        for start, end in _sentences(passage):
            sentence = passage[start:end]
            pair = re.search(r'([가-힣]{2,4})[·ㆍ]([가-힣]{2,4})\s*부부', sentence)
            if (pair and name in pair.groups() and any(_literals(venue, nearby + ' ' + sentence))
                    and re.search(r'운영|이끄|셰프', sentence)):
                return True
            nearby = sentence
    return False


def _direct_relation(work, subject, employer, allowed, cues, details):
    if subject.end() <= employer.start():
        gap = work[subject.end():employer.start()]
        if (_competing_subject(gap, subject[0]) or re.search(r'\b(?:that|whereas|while)\b', gap, re.I)
                or re.search(r'(?:했고|했지만|하지만|했으며|하고 나서)\s+[가-힣]{2,4}[가는은이]\s+', gap)):
            return None
    old = any(cue['former'].search(work) for cue in cues) or FORMER_TITLE.search(work)
    relation = next((rel for rel in allowed if not (rel == 'current' and old)
                     and lineage._binds(work, cues, rel, subject, employer, details.get('role_text'))), None)
    if not relation:
        relation = (_korean_relation(work, subject, employer) if re.search('[가-힣]', work)
                    else _english_relation(work, subject, employer))
    return None if relation == 'current' and old else relation


def _bound_row(entry, details, work, subject, employer, relation, cues, chef_alias, venue_alias):
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
    row.update(chef_text=entry['chef'], venue_text=entry['restaurant'], relation=relation, quote=entry['quote'],
               city=city, country_code=country, bilingual_venue=venue_alias, bilingual_name=chef_alias,
               restaurant_wording=bool(OUTLET.search(work)), contest=bool(CONTEST.search(work)))
    if subject:
        lineage._fields(raw, work, row, subject, employer, cues)
    else:
        date = lineage._period(raw.get('from_text'), work, employer, opening=relation == 'opening')
        if date:
            row.update(valid_from=date[0], from_text=raw['from_text'])
        if relation in ('former', 'training'):
            date = lineage._period(raw.get('to_text'), work, employer, end=True)
            if date:
                row.update(valid_to=date[0], to_text=raw['to_text'])
    return row


def _parse(entry, details):
    quote, chef, venue = (entry[k] for k in ('quote', 'chef', 'restaurant'))
    allowed = LABELS[entry['label']]
    reason = 'no_clause'
    rank = ('no_clause', 'no_relation', 'no_original', 'chef_unresolved', 'contest', 'reject_cue', 'temporary', 'institution')
    context = details.get('context')
    local = context[0][max(0, context[1] - 1800):context[1] + len(quote) + 280] if context else quote
    full_chef, short = _short_subject(chef, quote, local)
    names = [chef, short, full_chef] if full_chef and short else [chef]
    previous = ''
    sentences = list(_sentences(quote))
    for index, (start, end) in enumerate(sentences):
        work = quote[start:end]
        if allowed == ('opening',):
            # A completed pop-up may precede a separate permanent outlet.
            work = re.sub(r'^.*?팝업을\s*마무리한\s+', '', work, count=1)
        lang = lineage.language(work)
        guarded = _reject_reason(work, lang)
        if guarded:
            reason = max(reason, guarded, key=rank.index)
            previous = ''
            continue
        employers = list(_spans(venue, chef, work))
        subjects = [span for name in dict.fromkeys(names) for span in _spans(name, venue, work)
                    if name != short or full_chef]
        cues = lineage.signals(work, lang)
        for employer in employers:
            reject = _venue_reason(venue, employer.alias)
            tail = work[employer.end:].lstrip(" ’'\"”")
            if not reject and (NON_OUTLET.search(work[:employer.start].rstrip(" ‘'\"“"))
                               or re.match(r'\s*(?:호텔|그룹|백화점|브랜드|호텔앤리조트|'
                                           r'\b(?:hotels?|resorts?|group|inc|ltd|llc)\b)', tail, re.I)):
                reject = 'institution'
            if reject:
                reason = max(reason, reject, key=rank.index)
                continue
            for subject in (*subjects, None):
                if subject and max(subject.start, employer.start) < min(subject.end, employer.end):
                    continue
                bound, bs, be = _bind_text(work, subject, employer)
                contextual = False
                if bs:
                    rel = _direct_relation(bound, bs, be, allowed, cues, details)
                    if not rel and 'current' in allowed and not FORMER_TITLE.search(bound):
                        rel, contextual, roster_skip = _roster(work, subject, employer, context if work == quote else None)
                        if roster_skip:
                            reason = max(reason, roster_skip, key=rank.index)
                            continue
                    contextual |= bool(short and full_chef and name_key(subject.hit[0]) in {
                        name_key(short), name_key(chef) if chef != full_chef else ''})
                else:
                    rel = _korean_venue_relation(bound, be) if lang == 'ko' else _english_elision(bound, be)
                    if subjects or rel not in allowed or _different_actor(bound[:be.start()], full_chef or chef):
                        if short and not full_chef and rel in allowed:
                            reason = max(reason, 'chef_unresolved', key=rank.index)
                        continue
                    previous_end = sentences[index - 1][1] if index else 0
                    inherited = (previous and '\n' not in quote[previous_end:start]
                                 and _unique_antecedent(previous, full_chef or chef))
                    venue_subject = (bool(re.match(r'[이가은는]', bound[be.end():].lstrip())) if lang == 'ko'
                                     else bool(re.match(r'\s+opened its doors\b', bound[be.end():], re.I)))
                    if inherited and venue_subject:
                        inherited = any(_literals(venue, previous))
                    if not inherited:
                        contextual = (len(sentences) == 1 and rel in ('opening', 'current')
                                      and _source_attribution(entry, details, full_chef or chef, venue,
                                                              venue_subject=venue_subject))
                        if not contextual:
                            reason = max(reason, 'no_original' if not context else 'chef_unresolved', key=rank.index)
                            continue
                if rel not in allowed:
                    reason = max(reason, 'no_relation', key=rank.index)
                    continue
                row = _bound_row(entry, details, bound, bs, be, rel, cues, subject.alias if subject else None, employer.alias)
                if full_chef:
                    row['resolved_chef_text'] = full_chef
                if contextual and context and details.get('evidence_id'):
                    row['binding_receipt'] = dict(
                        rule='short_subject' if full_chef else 'source_attribution' if not bs else 'source_roster',
                        subject_id=entry['extraction_id'], evidence_id=details['evidence_id'],
                        evidence_hash=details.get('evidence_hash'),
                        occurrence=[context[1], context[1] + len(quote)], chef_text=details.get('chef_text'))
                return row, None
            if short and not full_chef:
                reason = max(reason, 'chef_unresolved', key=rank.index)
            elif not subjects:
                reason = max(reason, 'no_relation', key=rank.index)
        previous = work
    return None, reason


def _spellings(name, alias=None):
    base = re.sub(r'\s*[(（][^()（）]+[)）]$', '', name)
    embedded = _bilingual(name[len(base):], base)
    return list(dict.fromkeys(filter(None, (name, base if embedded else None, alias, embedded))))


def _blocked_name(names, blocked):
    return any(name_key(name) in blocked for name in names)
