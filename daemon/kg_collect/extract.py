"""Rule extractor -> LABELLED candidates. label = what the sentence says; epistemic = how far we may believe it.
Labels: career(경력) employment_claim(재직 주장) opening(오픈) relocation(이전) closure(폐업) rename menu(메뉴) collab(협업) mentorship_claim.
Epistemic: fact | claim | inference | unverified.   Rules first. Ambiguous sentences are flagged (extraction.ambiguity) and only then offered to
an optional model hook whose output is always epistemic='inference' and is never applied automatically.
Hard rules: guest events are 'collab', never employment. Co-working is 'collab/co_worker', never mentorship. Mentorship needs 사사/스승/제자 wording.
Nothing is filled in from outside the sentence; a value we cannot read stays NULL."""
import hashlib
import re
import datetime as dt

from . import EXTRACTOR_VERSION
from .review import NAME_STOP, reject_reason
from .targets import norm

SURNAMES = set('김이박최정강조윤장임한오서신권황안송류유홍전고문양손배백허남심노하곽성차주우구민나진지엄채원천방공현함변염여추도소석선설마길연위표명기반왕금옥육인맹제모탁국어은편용예경봉사부황보')
ROLE_RE = r'(총괄\s?셰프|총괄\s?쉐프|수\s?셰프|수석\s?셰프|헤드\s?셰프|헤드\s?쉐프|오너\s?셰프|오너\s?쉐프|부주방장|주방장|파티시에|소믈리에|셰프\s?드\s?파티|스태프|인턴|실습생|조리장|셰프|쉐프)'
ORG_SUFFIX = r'(?:레스토랑|식당|다이닝|키친|비스트로|호텔|스시|오마카세|그룹|바|카페|베이커리)'
CHEF_PAT = re.compile(r'(?<![가-힣])([가-힣]{2,5})\s?(?:오너\s?|총괄\s?|수\s?|헤드\s?|수석\s?)?(?:셰프|쉐프)')
CHEF_STOP = {'총괄', '헤드', '오너', '수석', '스타', '미쉐린', '미슐랭', '유명', '이탈리안', '프렌치', '한식', '일식', '중식', '양식', '파인', '신임', '신입', '라면', '요리', '레스토랑', '제과', '스시', '보조', '여성', '남성', '젊은', '현직', '전직', '그의', '그녀', '이들', '두', '세', '한', '이', '그', '저'}
SENT_SPLIT = re.compile(r'(?<=[.!?。])\s+|\n+')

PATTERNS = {
    'collab': re.compile(r'게스트\s?(?:셰프|쉐프|다이닝|디너)|콜라보|협업|초청|내한|원\s?나이트|투\s?나이트|스페셜\s?디너|합작|함께\s?선보|공동\s?개발|손잡고|손을\s?잡고|(?:갈라|와인|페어링|마리아주|스페셜|특별)\s?(?:디너|만찬|런치)|(?:디너|만찬|행사|이벤트)\s?(?:를\s?|을\s?)?(?:개최|진행|연다|열어|선보)'),
    'co_worker': re.compile(r'함께\s?(?:일했|근무|일한)|같이\s?(?:일했|근무)|동료|동기로|호흡을\s?맞춰|한솥밥'),
    'mentor': re.compile(r'사사|스승|제자|밑에서\s?(?:배웠|수련|배운)|가르침|멘토'),
    'career': re.compile(r'출신|에서\s?(?:(?:20\d{2}\s?년?\s?(?:부터|[~\-–])\s?(?:20\d{2}\s?년?|현재|지금)(?:까지)?\s?)?)(?:' + ROLE_RE + r'로?\s?)?(?:\d{1,2}\s?년(?:간|동안)?\s?)?(?:근무|일했|일한|경력|수련|근속|지냈|재직|배웠)|를\s?거쳐|을\s?거쳐|에서\s?' + ROLE_RE + r'(?:로|를)?\s?(?:맡|지냈|역임|활약|일)|경력을\s?(?:쌓|가진)|출신으로'),
    'employment': re.compile(r'(?:총괄|헤드|오너|수|수석)\s?(?:셰프|쉐프)(?:로|를)?\s?(?:부임|합류|영입|선임|취임|맡|이끌|활약|있|일하)|총괄(?:한다|하고\s?있|했|하며|하는|하고|해\s?온|해왔)|(?:이끌며|이끌어\s?온|이끌어왔|맡으며|맡아\s?온|맡아왔|운영하며|운영하는|운영\s?중인|운영하고\s?있)|(?:으)?로\s?(?:옮겼|이직했|이적했)|(?:이끌고\s?있|이끄는|맡고\s?있|합류했|합류한|부임했|영입했|영입된|선임됐)|(?<=[가-힣A-Za-z’\'”])의\s(?:총괄|헤드|오너|수|수석)?\s?(?:셰프|쉐프)'),
    'opening': re.compile(r'(?:그랜드\s?|가\s?|재\s?|신규\s?)?오픈(?:했|한다|할|하며|해|예정|을\s?앞|[\s,.…·!?’”\'"」』)\]]|$)|개업|문을\s?(?:연|열|열었)|[을를]\s?열었|개점|론칭'),
    'relocation': re.compile(r'이전\s?(?:오픈|개점|했|한다|할|예정)?|옮겨|옮긴다|새\s?둥지|자리를\s?옮|확장\s?이전|이사'),
    'closure': re.compile(r'폐업|폐점|문을\s?닫|영업\s?종료|영업을\s?종료|운영\s?종료|마지막\s?영업|영업을\s?마무리|영업을\s?마치|영업\s?마감|휴업|운영을\s?종료'),
    'rename': re.compile(r'리브랜딩|상호를\s?변경|이름을\s?(?:바꿨|바꿔|바꾸|변경)|새\s?이름|간판을\s?(?:바꿔|교체)|으로\s?재탄생'),
    'menu': re.compile(r'신메뉴|새\s?메뉴|메뉴를|메뉴로|코스\s?(?:메뉴|요리)|디너\s?코스|런치\s?코스|테이스팅\s?메뉴|시그니처\s?(?:메뉴|요리|디시)|\d{1,3},?\d{3}원'),
}
HEARSAY = re.compile(r'이야기가\s?있|얘기가\s?있|알려졌|전해졌|것으로\s?보인|추정|소문|관측|로\s?알려|듯하|알려진\s?바')
NEGATION = re.compile(r'폐업\s?(?:이\s?)?아닌|폐업설\s?(?:을\s?)?부인|폐업(?:하지|은)\s?않|오픈(?:하지|은)\s?않|계획이\s?없|사실이\s?아니|부인했')
AT_RISK = re.compile(r'폐업\s?(?:위기|직전|위험|기로|우려|절벽)|문\s?닫을\s?위기')
COND_CLOSE = re.compile(r'(?:면|하면|으면)\s?즉시\s?폐업')
FUTURE = re.compile(r'예정|계획|앞두고|앞둔|할\s?것이다|할\s?전망|준비\s?중|오픈\s?임박')
DEMONSTRATIVE = re.compile(r'^\s*(?:이곳|이\s?식당|이\s?레스토랑|이\s?업장|해당\s?(?:식당|레스토랑|업장)|이\s?가게)')
PRONOUN_SUBJ = re.compile(r'^\s*(?:그는|그녀는|그가|이\s?셰프는|해당\s?셰프는|그의)')
DATE_RE = re.compile(r'(?:(20\d{2})\s?년\s?)?(\d{1,2})\s?월(?:\s?(\d{1,2})\s?일)?|(20\d{2})\s?년(?!\s?\d{1,2}\s?월)')
ISO_DATE = re.compile(r'(?<![\d-])20\d{2}-\d{2}(?:-\d{2})?(?![\d-])')
TENURE_RE = re.compile(r'(20\d{2})\s?(?:년)?\s?(?:[~\-–]|부터)\s?(20\d{2}|현재|지금)')
DURATION_RE = re.compile(r'(\d{1,2})\s?년(?:\s?간|\s?동안|\s?가량|\s?여)')


REL_DATE = re.compile(r'(지난해|작년|올해|올\s|금년|내년)\s?(?:(\d{1,2})\s?월)?')
EN_MONTHS = {m: i + 1 for i, m in enumerate(['january', 'february', 'march', 'april', 'may', 'june', 'july', 'august', 'september', 'october', 'november', 'december'])}
EN_DATE = re.compile(r'\b(January|February|March|April|May|June|July|August|September|October|November|December)(?:\s(\d{1,2}),?)?\s(20\d{2})\b|\b(?:in|since|from)\s(20\d{2})\b')


def relative_or_english_date(sent: str, pub_year=None) -> tuple[str | None, str | None, str | None]:
    """(text, iso, note) for '지난해 5월' / '올해' (anchored on the PUBLICATION year, never the fetch date) and 'March 2024' / 'since 2019'."""
    m = EN_DATE.search(sent)
    if m:
        if m.group(4):
            return m.group(0), m.group(4), None
        mo = EN_MONTHS[m.group(1).lower()]
        return m.group(0), (f'{m.group(3)}-{mo:02d}-{int(m.group(2)):02d}' if m.group(2) else f'{m.group(3)}-{mo:02d}'), None
    m = REL_DATE.search(sent)
    if m and pub_year and str(pub_year).isdigit():
        y = int(pub_year) + {'지난해': -1, '작년': -1, '내년': 1}.get(m.group(1).strip(), 0)
        mo = int(m.group(2)) if m.group(2) and 1 <= int(m.group(2)) <= 12 else None
        return m.group(0).strip(), (f'{y}-{mo:02d}' if mo else str(y)), 'relative_to_publication_date'
    return None, None, None


def iso_from_dateish(m, pub_year=None) -> tuple[str | None, str | None]:
    """(iso, note). Month without a stated year borrows the PUBLICATION year and says so; never guesses beyond that."""
    if not m:
        return None, None
    if m.group(4):
        return m.group(4), None
    y, mo, d = m.group(1), m.group(2), m.group(3)
    mo_i = int(mo)
    if not 1 <= mo_i <= 12:
        return None, None
    note = None
    if not y:
        if not pub_year:
            return None, None
        y, note = str(pub_year), 'year_from_publication_date'
    return (f'{y}-{mo_i:02d}-{int(d):02d}' if d and 1 <= int(d) <= 31 else f'{y}-{mo_i:02d}'), note


def _quote(sent: str, anchor: int) -> str:
    s = sent.strip()
    if len(s) <= 200:
        return s
    a = max(0, min(anchor - 90, len(s) - 200))
    return s[a:a + 200]


def sentences(text: str):
    pos = 0
    for part in SENT_SPLIT.split(text):
        if not part or not part.strip():
            continue
        i = text.find(part, pos)
        if i < 0:
            continue
        pos = i + len(part)
        s = part
        while len(s) > 400:      # very long run-on: cut on commas / ' 그리고 ' without losing offsets
            cut = max(s.rfind(',', 0, 400), s.rfind('. ', 0, 400))
            cut = cut if cut > 100 else 400
            yield i, s[:cut + 1]
            i += cut + 1
            s = s[cut + 1:]
        if s.strip():
            yield i, s


DOUBLE_SURNAMES = ('남궁', '선우', '황보', '제갈', '독고', '사공')
PARTICLE_END = '은는의도를을와과이가께'
NOT_A_NAME = {'기대출연', '이츠', '출연', '스타', '레전드', '유명'}


def clean_name(nm: str, after: str = '') -> str | None:
    """Real-run lesson: '최송이는 오너 셰프' (particle glued on), '기대출연 셰프들' (plural, not a person), '이츠 셰프 컬렉션' (brand). Accept 2-3 syllables,
    4 only with a double surname; strip one trailing particle; plural/collection wording means a category, not a person."""
    if nm in NOT_A_NAME or norm(nm) in NAME_STOP or after.lstrip().startswith(('들', '컬렉션', '클래스', '출신들')):
        return None
    if (len(nm) >= 4 and nm[-1] in '은는의도를을와과') or (len(nm) == 3 and nm[-1] in '는의를와과'):
        nm = nm[:-1]                  # 3-syllable names often END in 은/이/을 ('정다은'): only unambiguous particles are stripped there
    if len(nm) == 4 and not nm.startswith(DOUBLE_SURNAMES):
        return None
    if len(nm) == 5 or not 2 <= len(nm) <= 4:
        return None
    return nm


def new_chef_names(sent: str, known_spans) -> list[dict]:
    out = []
    for m in CHEF_PAT.finditer(sent):
        nm = clean_name(m.group(1), sent[m.end():m.end() + 4])
        if not nm or nm in CHEF_STOP or nm[0] not in SURNAMES or any(s <= m.start(1) < e for s, e in known_spans):
            continue
        out.append({'surface': nm, 'start': m.start(1), 'end': m.start(1) + len(nm)})
    return out


# ── rules/1.4: generalisation helpers ──
PAREN = re.compile(r'\s?\(([^()]{1,40})\)')
LIST_SEP = re.compile(r'^\s?[\'’"”」』]?\s?(?:(?:,|，|과|와|·|ㆍ|/|및|그리고|,\s?그리고|,\s?및)\s?(?:[\'‘"“「『]|(?:[가-힣A-Za-z]{1,10}\s){1,2}[\'‘"“「『])?|(?:(?:오너|총괄|헤드|수석)\s?)?(?:셰프|쉐프)?\s?겸\s?)\s?$')
# '이타닉가든'과 레스케이프 호텔 '라망 시크레' (descriptor words allowed only before a QUOTED next item) / 'A 헤드 셰프 겸 B'
PARTICLE_START = set('은는이가을를의에도와과로으만부까처랑나엔')
# '노을의 갈라 디너' names the venue. '재료 온기의 맛' does not: only a role or an event noun keeps a short name.
_EVENT_AFTER_UI = r'(?:스페셜|특별|와인|갈라|게스트)?\s?(?:갈라|디너|만찬|런치|협업|콜라보|게스트|페어링|마리아주|행사|이벤트)'
AFTER_UI_KEEP = re.compile(r'^\s?(?:' + ROLE_RE + r'|메뉴|코스|오너|대표|주방|디저트|매장|지점|분점|팝업|레스토랑|식당|공동|새|신임|전\s|前|창업자|설립자|운영자|헤드|총괄|수석|파티시에|' + _EVENT_AFTER_UI + r')')
SHORT_REF = re.compile(r'(?<![가-힣A-Za-z])([가-힣]{1,2})\s?(?:셰프|쉐프)(?![가-힣]{0,1}들)')


def strip_parens(sent: str):
    """'Lysée(리제)의 이은지(Eunji Lee)' -> 'Lysée의 이은지' with a map back to original offsets and the removed (head_end, inner) pairs.
    Bilingual glosses / aliases in brackets break adjacency rules; the inner text is kept for gazetteer lookup."""
    out, amap, removed, last = [], [], [], 0
    for m in PAREN.finditer(sent):
        out.append(sent[last:m.start()])
        amap.extend(range(last, m.start()))
        removed.append((len(amap), m.group(1).strip()))
        last = m.end()
    out.append(sent[last:])
    amap.extend(range(last, len(sent)))
    amap.append(len(sent))
    return ''.join(out), amap, removed


def is_common_word_use(sent: str, m: dict) -> bool:
    """Short Hangul restaurant names that are ordinary words ('본연', '온기', '제철') are only a venue when the context says so.
    Common use: glued to a non-particle syllable, or '<alias>의 <ordinary noun>' ('재료 본연의 맛')."""
    surf = m['surface']
    if not re.fullmatch(r'[가-힣]{1,3}', surf.replace(' ', '')):
        return False
    pre, post = sent[max(0, m['start'] - 1):m['start']], sent[m['end']:m['end'] + 14]
    if pre in '‘\'"“「『' and pre:
        return False
    if post[:1] and re.match(r'[가-힣]', post[:1]) and post[0] not in PARTICLE_START:
        return True
    if post.startswith('의') and not AFTER_UI_KEEP.match(post[1:]):
        nxt = re.match(r'\s?([가-힣]{2,4})', post[1:])
        if nxt and not (nxt.group(1)[0] in SURNAMES and re.match(r'\s?[가-힣]{2,4}\s?(?:\(|셰프|쉐프|오너|총괄|대표)', post[1:])):
            return True
    return False


def coordinated(items: list, anchor: dict, sent: str, cap: int = 5) -> list:
    """Items joined to `anchor` by list separators ('A, B와 C') — the whole coordinated group, in order."""
    items = sorted(items, key=lambda x: x['start'])
    if anchor not in items:
        return [anchor]
    i = items.index(anchor)
    lo = hi = i
    while lo > 0 and LIST_SEP.match(sent[items[lo - 1]['end']:items[lo]['start']]):
        lo -= 1
    while hi + 1 < len(items) and LIST_SEP.match(sent[items[hi]['end']:items[hi + 1]['start']]):
        hi += 1
    grp, seen = [], set()
    for it in items[lo:hi + 1]:
        if it['surface'] not in seen:
            seen.add(it['surface'])
            grp.append(it)
    return grp[:cap]


def coref_short_forms(sent: str, known_spans, doc_chefs: list) -> list[dict]:
    """'손 셰프' / '종원 셰프' -> the unique full name seen EARLIER in the same document. Ambiguous (two 손s) or unseen -> nothing."""
    out = []
    for m in SHORT_REF.finditer(sent):
        g = m.group(1)
        if any(s <= m.start(1) < e for s, e in known_spans):
            continue
        if len(g) == 1:
            if g not in SURNAMES:
                continue
            cands = {n for n in doc_chefs if len(n) >= 2 and n[0] == g and re.fullmatch(r'[가-힣]{2,4}', n)}
        else:
            cands = {n for n in doc_chefs if len(n) == 3 and n[1:] == g}
            if g in doc_chefs:
                continue
        if len(cands) == 1:
            out.append({'surface': cands.pop(), 'start': m.start(1), 'end': m.end(1), 'coref': g})
    return out


# '<hotel/restaurant>은 신임 총괄 셰프로 ‘기욤 콤파라(Guillaume Comparat)’를 선임했다': appointment of a (possibly foreign, never-seen) chef.
APPOINT = re.compile(r'(?:총괄|헤드|수석|오너|수)?\s?(?:셰프|쉐프|주방장)(?:로|에)\s?[‘\'"“]?([가-힣A-Za-z][가-힣A-Za-z\s·.\-]{1,24}?)\s?(?:\([^)]{2,40}\))?[’\'"”]?(?:를|을)\s?(?:신임\s?|새\s?)?(?:선임|영입|임명|발탁|맞이|모셨|모시|영입했|내정)')
# '한식당 ‘세옥(SEOK)’이 문을 열었다' / '레스토랑, ‘SEOK’이 ...': venue-type noun + quoted name
QUOTED_VENUE = re.compile(r'(?:한식당|양식당|일식당|중식당|레스토랑|식당|다이닝|비스트로|오마카세|바|카페|베이커리|펍|\d\s?스타)[,\s]*[‘\'“"]([^’\'”"]{2,30})[’\'”"]')
QUOTED_NAME = re.compile(r'[‘\'“"]([^’\'”"]{2,30})[’\'”"]')
# A chef must also be identified in the sentence; hotel/product launches alone are not collaborations.
PRODUCT_LAUNCH = re.compile(r'(?:(?:셰프|쉐프)\s?컬렉션|브랜드|제품|상품).{0,40}출시|(?:셰프|쉐프)(?:와|가).{0,40}출시')
# Only the framed venue object opens, not an event held by a restaurant mentioned elsewhere.
VENUE_OBJECT_OPEN = re.compile(r'(?:팝업|레스토랑|식당|다이닝)\s*(?:[‘\'“"]([^’\'”"]{2,30})[’\'”"]|([가-힣A-Za-z0-9·&\-]{2,24}?))[을를]\s?(?:연다|열고)')
BARE_SUBJECT_VENUE = re.compile(r'(?:^|\s)(?:레스토랑|식당|다이닝)\s+([가-힣A-Za-z][가-힣A-Za-z0-9·&\-]{1,23}?)(?:이|가|은|는)\s')


# a quoted name directly in front of the opening verb ('X 셰프 ‘R’ 오픈', '‘R’ 7일 오픈') names the venue even without a venue noun
QUOTED_OPEN = re.compile(r'[‘\'“"]([^’\'”"]{2,30})[’\'”"](?:\s?\([^)]{1,30}\))?\s?(?:[을를이가]\s?)?(?:\d{1,2}일\s?)?(?:그랜드\s?|정식\s?)?(?:오픈|개업|개점|문을\s?(?:연|열))')
CLAUSE_BREAK = re.compile(r'\s[—–|]\s|…')


def same_clause(sent: str, a: int, b: int) -> bool:
    """Headlines glue several clauses with '—', '…' or '|': a cue in one clause must not veto a statement in another."""
    lo, hi = sorted((a, b))
    return not CLAUSE_BREAK.search(sent[lo:hi])


# Type nouns / event nouns that look like venue slots but are never a restaurant name.
VENUE_NAME_STOP = {
    '레스토랑', '식당', '다이닝', '팝업', '호텔', '키친', '비스트로', '오마카세', '바', '카페', '베이커리', '펍',
    '출범', '오픈', '개점', '개업', '론칭', '리뉴얼', '확장', '이전', '폐업', '휴업', '협업', '콜라보',
    '메뉴', '코스', '디너', '런치', '만찬', '행사', '이벤트', '갈라', '후속', '신규', '기존',
}


def clean_venue(nm: str) -> str | None:
    nm = re.sub(r'\s*\([^)]*\)\s*$', '', nm).strip()
    if not 2 <= len(nm) <= 24 or nm.isdigit():
        return None
    hangul = re.sub(r'\([^)]*\)', '', nm).strip()
    if not hangul or hangul in VENUE_NAME_STOP or norm(hangul) in NAME_STOP:
        return None
    return hangul


def norm_key(x: str) -> str:
    return re.sub(r'[\s\-·.]', '', x).lower()


DESCRIPTORS = {'프렌치', '이탈리안', '럭셔리', '특급', '5성급', '글로벌', '국내', '유명', '호텔', '신규'}
SUBJ_BEFORE = re.compile(r'^(?:[“‘\'"\[(].{0,40}?[”’\'"\])]\s*)?([가-힣A-Za-z0-9·&\s]{2,25}?)(?:은|는|이|가)\s')


def nearest_before(items: list, pos: int):
    """Chef mention closest before `pos` (else the first one after). Mentions are dicts with start/end/surface."""
    before = [i for i in items if i['end'] <= pos]
    return before[-1] if before else (items[0] if items else None)


_COORD_GAP = re.compile(r'^(?:\s?(?:셰프|쉐프|오너|총괄|헤드|수석|씨))?\s*(?:와|과|,|，|·|및|그리고)\s*(?:(?:오너|총괄|헤드|수석)\s?)?(?:셰프|쉐프)?\s*$')


def surface_at(items, pos: int, sent: str | None = None):
    """Name the cue is about.

    A coordinated subject ('한도윤 셰프와 서하람 셰프가') keeps its first name, which is the
    participant the record has always stored. A person in an earlier clause ('김하늘이 사회를
    보고 임지훈이 협업한다') is a bystander and is not substituted for the name beside the verb.
    """
    ordered = sorted(items, key=lambda x: x['start'])
    hit = nearest_before(ordered, pos)
    if not hit:
        return None
    i = ordered.index(hit)
    if sent is not None:
        while i > 0 and _COORD_GAP.match(sent[ordered[i - 1]['end']:ordered[i]['start']]):
            i -= 1
    return ordered[i]['surface']


BARE_ORG_STOP = {'호텔', '해외', '국내', '한식', '양식', '일식', '중식', '프렌치', '이탈리안', '유명', '대형', '현지', '명문', '요리', '학교', '조리', '대학', '업계', '외식', '미쉐린', '미슐랭',
                 '스타', '레스토랑', '식당', '같은', '다른', '이전', '특급', '서울', '부산', '제주', '대기업', '출신', '셰프', '쉐프', '요리사', '유학', '스페인', '프랑스', '이탈리아', '일본', '미국', '리옹', '파리', '런던', '도쿄', '뉴욕', '오사카', '교토', '로마', '밀라노', '바르셀로나', '베이징', '상하이', '홍콩', '방콕', '싱가포르', '본고장', '화교', '교포', '재일교포', '조선족', '귀화'}


def bare_org_before(sent: str, idx: int) -> str | None:
    """Last resort for a name we have never seen ('가온재 출신'): a bare 2-6 syllable Hangul token right before the marker. Never trusted: the caller flags it unverified."""
    m = re.search(r'(?:^|[\s,(\'"“‘])([가-힣]{2,6})\s?$', sent[:idx])
    if not m or m.group(1) in BARE_ORG_STOP or re.search(r'(?:은|는|도|의|을|를|에|로|과|와)$', m.group(1)):
        return None
    return m.group(1)


def org_phrase_before(sent: str, idx: int) -> str | None:
    """Unknown organisation immediately before a '출신'/'에서' marker: quoted name, Latin capitalised name, or Hangul phrase ending in a restaurant suffix."""
    head = sent[:idx].rstrip()
    m = re.search(r"[『「'‘\"“]([^'’\"”』」]{2,20})['’\"”』」]$", head)
    if m:
        return m.group(1).strip()
    m = re.search(r'((?:[A-Z][A-Za-z\'’&.]+\s?){1,4})$', head)
    if m and len(m.group(1).strip()) >= 3:
        return m.group(1).strip()
    m = re.search(r'([가-힣A-Za-z]{1,8}\s?' + ORG_SUFFIX + r')$', head)
    if m:
        return m.group(1).strip()
    return None


POPUP = re.compile(r'팝업(?:\s?(?:레스토랑|스토어|매장|다이닝))?')
POPUP_RAN = re.compile(r'(?:운영했|운영한|이끌었|맡았|진행했)')
# chef and restaurant written side by side with no relation verb ("'밍글스' 강민구 셰프", "강민구 셰프 '밍글스'"): a weak current-affiliation claim
SIDE_GAP = re.compile(r'^\s?(?:(?:오너|총괄|헤드|수석|수|파티시에|페이스트리)?\s?(?:셰프|쉐프)(?:의)?)?\s?[‘\'"“]?\s?$|^[’\'"”]?\s?(?:의\s(?:(?:오너|총괄|헤드|수석|수)\s?)?(?:셰프\s|쉐프\s|파티시에\s)?)?$|^[’\'"”]?\s(?:오너|총괄|헤드|수석|수)?\s?(?:셰프|쉐프)(?:인|이자)\s$')
# English press headlines
EN_OPEN = re.compile(r'\b(?:opens|opened|to open|is opening|will open|debuts|launches|first opened)\b', re.I)
EN_FORMER = re.compile(r'\b(?:former|ex-|formerly (?:of|at)|alum(?:nus|na)? of|previously (?:at|of))\b', re.I)
EN_CHEF = r'(?:executive |head |pastry |sous |chef-owner |owner )?(?:chef|pastry chef|star)'

VENUE_TYPE = r'(?:한식당|양식당|일식당|중식당|레스토랑|식당|다이닝|비스트로|오마카세|restaurant|bistro)'
DELIMITED_VENUE = re.compile(rf'{VENUE_TYPE}\s*(?:[‘\'"“「『]([^’\'"”」』]{{2,30}})[’\'"”」』]|([가-힣A-Za-z0-9·&-]{{2,24}}?)(?=(?:에서|[을를의])\s|\s+[—–]))', re.I)
LEAD_ROLE = r'(?:(?:오너|헤드|수석|총괄)\s?(?:셰프|쉐프)|주방장|대표)'
CHEF_TITLE = rf'(?:{LEAD_ROLE}|셰프|쉐프)'
COMPLETED_OPEN = r'(?:열었다|열었으며|열었다고 밝혔다|개업했다|(?:정식\s*)?오픈했다|개점했다|문을\s*열었다)'
NON_PUBLICATION_CONTEXT = re.compile(r'팝업|게스트|가상|모의|드라마|영화|예능\s*(?:속|에서)|경연\s*(?:용|미션)|\b(?:pop-up|guest shift|fictional)\b', re.I)

def confirmed_closure(quote, venue):
    if any(p.search(quote) for p in (NEGATION, FUTURE, AT_RISK, COND_CLOSE, NON_PUBLICATION_CONTEXT)):
        return False
    return bool(re.search(re.escape(venue) + r'[’\'"”」』]?\s*(?:[은는이가]\s*)?(?:(?:이미|공식적으로)\s*)?(?:폐업(?:했다|했으며)|폐점했다|영업(?:을)?\s*종료했다|문을\s*닫았다|(?:has )?closed\b)', quote, re.I))


def publication_assertions(sentence, gaz):
    """Explicit subject/venue grammar. Offsets use the original publisher sentence."""
    if (len(sentence.strip()) > 200 or NON_PUBLICATION_CONTEXT.search(sentence) or HEARSAY.search(sentence)
            or PATTERNS['collab'].search(sentence) or PATTERNS['co_worker'].search(sentence)):
        return []
    mentions = gaz.find(sentence)
    chefs = [m for m in mentions if m['kind'] == 'chef']
    new = new_chef_names(sentence, [(m['start'], m['end']) for m in mentions])
    people = {(m['start'], m['end']): m['surface'] for m in chefs + new}
    venues = {(m['start'], m['end']): m['surface'] for m in mentions if m['kind'] == 'restaurant'}
    for m in DELIMITED_VENUE.finditer(sentence):
        g = 1 if m[1] else 2
        name = clean_venue(m[g])
        if name and (not reject_reason('restaurant', name) or gaz.lookup(name)):
            venues[m.span(g)] = name
    for m in QUOTED_NAME.finditer(sentence):
        name = clean_venue(m[1])
        if name and not reject_reason('restaurant', name) and re.match(r'\s*(?:출신|에서)', sentence[m.end():]):
            venues[m.span(1)] = name
    for _, end in venues:
        prefixed = re.match(rf'^[’\'"”」』 ]*(?:의|에서)\s*{LEAD_ROLE}\s+([가-힣]{{2,4}}?)(?=(?:은|는|이|가)\s|[ ,.。!?]|$)', sentence[end:])
        if prefixed and (name := clean_name(prefixed[1])) == prefixed[1] and name[0] in SURNAMES:
            people.setdefault((end + prefixed.start(1), end + prefixed.end(1)), name)
    # Several named people need a more specific attribution than a shared sentence.
    if len(people) != 1:
        return []
    (cs, ce), chef = next(iter(people.items()))
    if norm(chef) in NAME_STOP:
        return []
    out = []
    korean = bool(re.search(r'[가-힣]', sentence))
    for (vs, ve), venue in venues.items():
        before = sentence[:vs]
        after = sentence[ve:].lstrip('’\'"”」』 ')
        left = before.rstrip('‘\'"“「『 ')
        between = sentence[ce:vs].rstrip('‘\'"“「『') if ce <= vs else ''
        role = None
        lineage = False
        if korean and ve <= cs:
            gap = sentence[ve:cs].lstrip('’\'"”」』 ')
            previous = re.fullmatch(rf'(?:출신\s*|(?:에서|의)?\s*(?:전\s*{ROLE_RE}|{ROLE_RE}\s*출신)\s*)', gap)
            if previous:
                lineage, role = True, previous[1] or previous[2]
        if korean and ce <= vs and re.match(rf'^\s*{CHEF_TITLE}\s*(?:는|은|가|이)\s+', between):
            previous = re.match(rf'^(?:에서\s*{ROLE_RE}\s*(?:로|으로)\s*(?:근무했다|근무했으며|일했다|일했으며|일한|지냈다|지냈으며|역임했다)|출신(?:이다|이며|으로)|을\s*거쳤다|를\s*거쳤다)', after)
            if previous:
                lineage, role = True, previous[1]
        if not korean:
            previous = re.search(r'\b(?:former(?:\s+(executive chef|head chef|sous chef|pastry chef|chef))?\s+(?:at|of)|alum(?:nus|na)? of|formerly (?:at|of))\s*$', left, re.I)
            if previous and (ve <= cs and re.fullmatch(r'[,’\'"”\s]*(?:chef\s+)?', sentence[ve:cs], re.I)
                             or ce <= previous.start() and re.fullmatch(r'[ ,]+', sentence[ce:previous.start()])):
                lineage, role = True, previous[1]
        if lineage:
            out.append(dict(label='career', chef=chef, org=venue, role=role))
        if FUTURE.search(sentence) or NEGATION.search(sentence):
            continue
        if korean:
            subject = re.match(rf'^\s*{CHEF_TITLE}(?:은|는|이|가)\s+', between)
            # Keep the subject in control of the verb and permit a directly attached location phrase.
            prefix = between[subject.end():] if subject else ''
            date = r'(?:(?:20\d{2}년\s*)?(?:\d{1,2}월\s*(?:\d{1,2}일\s*)?)|20\d{2}년\s*|20\d{2}-\d{2}(?:-\d{2})?\s+|(?:지난해|올해)\s+)'
            opens = bool(subject and re.fullmatch(rf'\s*{date}?(?:[가-힣A-Za-z0-9 -]{{1,65}}(?:에서|에)\s+)?(?:새\s+)?(?:{VENUE_TYPE}\s*)?', prefix)
                         and re.match(rf'^[을를]\s*{COMPLETED_OPEN}(?=$|[.!?,。\s])', after))
            current = bool(ve <= cs and re.fullmatch(rf'[’\'"”」』 ]*(?:의|에서)\s*{LEAD_ROLE}\s*', sentence[ve:cs])
                           and not re.search(r'^(?:\s*(?:셰프\s*)?(?:였|이었|이었다|전\s|가\s*아니))|(?:사임|퇴임|떠났다|그만뒀|그만두)', sentence[ce:]))
            current |= bool(subject and re.fullmatch(rf'\s*(?:현재\s*)?(?:{VENUE_TYPE}\s*)?', prefix)
                            and re.match(rf'^(?:의|에서)\s*{LEAD_ROLE}\s*(?:이다|로\s*(?:일하고\s*있다|재직\s*중이다|있다))', after))
            current |= bool(subject and re.fullmatch(rf'\s*(?:현재\s*)?(?:{VENUE_TYPE}\s*)?', prefix)
                            and re.match(r'^(?:[을를]|(?:의\s*)?주방을)\s*(?:현재\s*)?(?:이끌고|운영하고)\s*있다', after.lstrip()))
        else:
            opens = bool(ce <= vs and re.fullmatch(r'(?:,\s*formerly (?:of|at) [^,]+,)?\s+(?:has )?(?:opened|opens|launched)\s+(?:(?:the )?(?:restaurant|bistro)\s+)?', between, re.I))
            current = bool(ce <= vs and re.fullmatch(r'\s*,?\s*(?:is|serves as)\s+(?:the\s+)?(?:owner|head chef|executive chef|chef-owner)\s+(?:of|at)\s+(?:restaurant\s+)?', between, re.I))
        if opens or current:
            out.append(dict(label='opening' if opens else 'employment_claim', chef=chef, rest=venue, role=None))
    return out


def _en_rules(sent, chefs, rests, mk):
    """General English-headline rules. Only fire when BOTH a known chef and a known restaurant are in the sentence."""
    if not chefs or not rests or not re.search(r'[A-Za-z]{3}', sent):
        return
    c, r = chefs[0], rests[0]
    seg_c = sent[max(0, c['start'] - 40):c['start']]
    former = EN_FORMER.search(sent[max(0, r['start'] - 20):r['start'] + 1]) or EN_FORMER.search(seg_c)
    op = EN_OPEN.search(sent)
    if former:
        mk('career', chef=c['surface'], org=r['surface'], anchor=r['start'])
    if op and op.start() > c['start']:
        target = next((x for x in rests if x['start'] > op.start()), None)
        if target:
            mk('opening', chef=c['surface'], rest=target['surface'], anchor=op.start())
            return
    if former:
        return
    # "R's Pastry Chef X" / "R's ... Chef X" / "At R, ... Chef X" / "X, chef at R"
    poss = re.search(re.escape(r['surface']) + r'[’\']s\b', sent)
    at = re.search(r'\bat\s+' + re.escape(r['surface']) + r'\b', sent, re.I)
    chef_word = re.search(r'\b' + EN_CHEF + r'\b', sent, re.I)
    if (poss or at) and chef_word:
        mk('employment_claim', chef=c['surface'], rest=r['surface'], anchor=chef_word.start())


def extract_text(text: str, *, gaz, host: str, published_at: str | None = None, tier: str = 'press', ctx: dict | None = None,
                 ocr_conf: float | None = None, official_hosts: set | None = None, content_hash: str = '') -> list[dict]:
    """ctx: {'chef': target-row?, 'restaurant': target-row?} the job's target (used ONLY when the sentence has no subject; marks epistemic=inference)."""
    out = []
    pub_year = (published_at or '')[:4] or None
    official_hosts = official_hosts or set()
    doc_chefs: list = []
    for off, orig in sentences(text):
        if len(orig) < 8:
            continue
        sent, amap, removed = strip_parens(orig)
        mentions = gaz.find(sent)
        for pos, inner in removed:              # alias given only inside the brackets: attach it to the head word before the bracket
            if any(m['start'] <= pos <= m['end'] + 1 for m in mentions):
                continue
            hit = gaz.lookup(inner)
            hm = re.search(r'([A-Za-z가-힣][A-Za-z가-힣\'’.&\-]*(?:\s[A-Z][A-Za-z\'’.&\-]*)?)$', sent[:pos])
            if hit and hm:
                for ent in hit:
                    mentions.append({**ent, 'start': hm.start(1), 'end': pos, 'surface': inner})
        mentions = sorted([m for m in mentions if not (m['kind'] == 'restaurant' and is_common_word_use(sent, m))], key=lambda x: x['start'])
        known = [(m['start'], m['end']) for m in mentions]
        chefs = [m for m in mentions if m['kind'] == 'chef']
        rests = [m for m in mentions if m['kind'] == 'restaurant']
        new_chefs = new_chef_names(sent, known)
        if re.search(ROLE_RE, sent):
            for pos, inner in removed:          # '이은지(Eunji Lee)': a Hangul personal name glossed with a romanised one, in a sentence about chefs
                hm = re.search(r'(?<![가-힣])([가-힣]{2,4})$', sent[:pos])
                if hm and re.fullmatch(r"[A-Z][a-z]+(?:[\s\-][A-Z][a-z]+){1,2}", inner) and hm.group(1)[0] in SURNAMES and hm.group(1) not in CHEF_STOP \
                        and not any(a <= hm.start(1) < b for a, b in known) and not any(c['start'] == hm.start(1) for c in new_chefs):
                    nm = clean_name(hm.group(1))
                    if nm:
                        new_chefs.append({'surface': nm, 'start': hm.start(1), 'end': hm.start(1) + len(nm)})
        corefs = coref_short_forms(sent, known + [(c['start'], c['end']) for c in new_chefs], doc_chefs)
        given = {n[1:]: n for n in doc_chefs if len(n) == 3}
        new_chefs = [({**c, 'surface': given[c['surface']]} if len(c['surface']) == 2 and c['surface'] in given else c) for c in new_chefs] + corefs
        new_chefs.sort(key=lambda x: x['start'])
        for c in chefs + new_chefs:
            if 'coref' not in c and c['surface'] not in doc_chefs:
                doc_chefs.append(c['surface'] if c in new_chefs else (c.get('name_ko') or c['surface']))
        hits = {k: p.search(sent) for k, p in PATTERNS.items()}
        if chefs or new_chefs:
            hits['collab'] = hits['collab'] or PRODUCT_LAUNCH.search(sent)
        object_open = VENUE_OBJECT_OPEN.search(sent)
        object_venue = clean_venue(object_open.group(1) or object_open.group(2)) if object_open else None
        if object_open and not object_venue:
            object_open = None                  # bare type/event noun ('팝업 레스토랑을 연다', '디너를 연다') is not a venue opening
        if object_venue and re.search(r'디너|만찬|행사|이벤트|갈라', object_venue):
            object_open = object_venue = None
        hits['opening'] = hits['opening'] or object_open
        ar = AT_RISK.search(sent) or COND_CLOSE.search(sent)
        if ar and hits['closure'] and ar.start() <= hits['closure'].start() <= ar.end():
            hits['closure'] = None                    # 'closure crisis' / 'closes if sales fail' is a risk or a rule, not a closure
        popup = POPUP.search(sent)
        if popup and hits['collab'] is None:
            pass
        side = None
        if (chefs or new_chefs) and rests and not hits['career'] and not hits['collab'] and not hits['opening']:   # 'X 셰프 ‘R’ 오픈': the opening record carries the chef
            for c in sorted(chefs + new_chefs, key=lambda x: x['start']):
                for r in rests:
                    a, b = (c, r) if c['end'] <= r['start'] else (r, c)
                    if SIDE_GAP.match(sent[a['end']:b['start']]) and not re.match(r'\s?(?:출신|에서)', sent[b['end']:b['end'] + 4]):
                        side = (c, r)
                        break
                if side:
                    break
        assertions = publication_assertions(orig, gaz)
        if hits['closure'] and not rests:
            for m in DELIMITED_VENUE.finditer(orig):
                venue = clean_venue(m[1] or m[2])
                if venue and confirmed_closure(orig, venue):
                    assertions.append(dict(label='closure', chef=None, rest=venue, role=None))
        if not assertions and not any(hits.values()) and not APPOINT.search(sent) and not side and not (popup and POPUP_RAN.search(sent)) and not (chefs and rests and re.search(r'[A-Za-z]{3}', orig)):
            continue
        base_unverified = bool(HEARSAY.search(sent)) or tier in ('social_lead', 'search_lead') or (ocr_conf is not None and ocr_conf < 60)
        negated = bool(NEGATION.search(sent))
        future = bool(FUTURE.search(sent))
        dm = DATE_RE.search(sent)
        date_iso, date_note = iso_from_dateish(dm, pub_year)
        date_txt = dm.group(0) if dm else None
        if dm and REL_DATE.search(sent[max(0, dm.start() - 5):dm.start() + 1]) and not dm.group(1) and not dm.group(4):
            dm = None                              # '지난해 5월': the bare month must not borrow the publication year
        if not dm:
            date_txt, date_iso, date_note = relative_or_english_date(sent, pub_year)
            im = ISO_DATE.search(sent) if not date_txt else None
            if im:
                try:
                    dt.date.fromisoformat(im.group(0) + ('-01' if len(im.group(0)) == 7 else ''))
                except ValueError:
                    pass
                else:
                    date_txt = date_iso = im.group(0)
        tm = TENURE_RE.search(sent)
        role = re.search(ROLE_RE, sent)

        def mk(label, *, chef=None, rest=None, org=None, ambiguity=None, epi=None, anchor=0, extra=None, inferred=False):
            e = epi or ('unverified' if base_unverified else 'claim')
            if inferred and e == 'claim':
                e = 'inference'
            if label in ('opening', 'closure', 'relocation', 'rename') and host in official_hosts and e == 'claim':
                e = 'fact'                 # the establishment's own channel announcing its own status
            amb = ambiguity
            if negated and label in ('closure', 'opening'):
                amb, e = (amb or 'negated_statement'), 'unverified'
            elif future and label in ('opening', 'closure', 'relocation') and not amb:
                amb = 'planned_not_done'
            rec = {'label': label, 'epistemic': e, 'chef_text': chef, 'restaurant_text': rest, 'org_text': org,
                   'role_text': role.group(1) if role and label in ('career', 'employment_claim') else None,
                   'date_text': date_txt if label != 'career' else None, 'date_iso': date_iso if label != 'career' else None, 'date_note': date_note if label != 'career' else None,
                   'tenure_from': tm.group(1) if tm and label in ('career', 'employment_claim') else None,
                   'tenure_to': (tm.group(2) if tm and tm.group(2) not in ('현재', '지금') else None) if tm and label in ('career', 'employment_claim') else None,
                   'span_start': off, 'span_end': off + len(orig), 'quote': _quote(orig, amap[min(anchor, len(amap) - 1)]), 'extractor': f'rule:{label}',
                   'extractor_version': EXTRACTOR_VERSION, 'ambiguity': amb, 'published_at': published_at, **(extra or {})}
            rec['extraction_id'] = 'ext_' + hashlib.sha1('|'.join([content_hash, str(rec['span_start']), label, str(chef), str(rest), str(org), EXTRACTOR_VERSION]).encode()).hexdigest()[:14]
            out.append(rec)

        for assertion in assertions:
            label, asserted_role = assertion['label'], assertion['role']
            mk(label, chef=assertion['chef'], rest=assertion.get('rest'), org=assertion.get('org'),
               extra={'extractor': f'rule:publication_{label}', 'role_text': asserted_role})

        chef_names = [c['surface'] for c in chefs] + [c['surface'] for c in new_chefs]
        if re.search(r'[A-Za-z]{3}', sent) and not re.search(r'[가-힣]', sent):
            _en_rules(sent, chefs, rests, mk)
            continue
        if popup and chef_names and POPUP_RAN.search(sent) and not hits['collab']:
            pv = QUOTED_VENUE.search(sent) or re.search(r'[‘\'“"]([^’\'”"]{2,30})[’\'”"]', sent[popup.start():])
            pname = next((r['surface'] for r in rests if r['start'] >= popup.start()), None) or (clean_venue(pv.group(1)) if pv else None)
            if pname:
                mk('career', chef=surface_at(list(chefs) + list(new_chefs), popup.start(), sent), org=pname, anchor=popup.start(), extra={'venue_format': 'popup'})
                continue
        # ── collab / co-working first: they veto employment readings ──
        is_collab = bool(hits['collab'])
        scv = BARE_SUBJECT_VENUE.search(sent) if is_collab and not rests else None
        if is_collab:
            qcv = None if rests else QUOTED_VENUE.search(sent)      # 'X 2스타 레스토랑 ‘R’의 Y 셰프와 협업': an unregistered quoted venue is still the partner restaurant
            if not rests and not qcv:
                qcv = next((m for m in QUOTED_NAME.finditer(sent) if any(m.end() <= c['start'] and SIDE_GAP.match(sent[m.end():c['start']]) for c in chefs + new_chefs)), None)
            collab_rest = surface_at(rests, hits['collab'].start(), sent) if rests else (clean_venue(qcv.group(1)) if qcv else (clean_venue(scv.group(1)) if scv else None))
            # Need a chef and/or a real venue name; '레스토랑 출범이' / bare type nouns are not partners.
            if chef_names or collab_rest:
                mk('collab', chef=surface_at(list(chefs) + list(new_chefs), hits['collab'].start(), sent), rest=collab_rest, anchor=hits['collab'].start(),
                   extra={'collab_kind': 'guest_event'})
        if hits['co_worker'] and not hits['mentor'] and (chef_names or rests):
            mk('collab', chef=surface_at(list(chefs) + list(new_chefs), hits['co_worker'].start(), sent), rest=surface_at(rests, hits['co_worker'].start(), sent), anchor=hits['co_worker'].start(),
               extra={'collab_kind': 'co_worker'}, ambiguity=None)
        if hits['mentor'] and (chefs or new_chefs):
            allc = sorted([c for c in chefs] + new_chefs, key=lambda x: x['start'])
            mm = hits['mentor']
            mentor = nearest_before(allc, mm.start())
            others = [c for c in allc if mentor and c['surface'] != mentor['surface']]
            mentee = others[0] if others else None
            if mentor and mentee and mentee['start'] < mentor['start'] and re.search(r'(?:밑에서|에게\s?사사\s?받|스승)', sent[mentee['end']:]):
                mentor, mentee = mentee, mentor
            clear = bool(mentor and mentee)
            mk('mentorship_claim', chef=mentee['surface'] if mentee else None, org=mentor['surface'] if mentor else None, anchor=mm.start(),
               ambiguity=None if clear else 'mentor_or_mentee_unclear', epi=None if clear else 'unverified',
               extra={'mentor_text': mentor['surface'] if mentor else None})
        career_org = None
        if side and not hits['employment'] and not APPOINT.search(sent):
            c, r = side
            if is_collab:
                pass                       # handled by the collab record (chef+restaurant now both attached below)
            else:
                for rr in coordinated(rests, r, sent):
                    mk('employment_claim', chef=c['surface'], rest=rr['surface'], anchor=c['start'], ambiguity='side_by_side_mention')
        # ── career: X came from ORG ──
        if hits['career'] and hits['career'].group(0).startswith('출신') and re.match(r'\s?(?:의\s?)?(?:소믈리에|바텐더|파티시에|직원|관계자|스태프|매니저|작가|기자)', sent[hits['career'].end():hits['career'].end() + 8]):
            hits['career'] = None             # '리츠칼튼 출신 소믈리에': the career belongs to an unnamed staff member, not to a chef in the sentence
        if hits['career'] and not is_collab:
            m = hits['career']
            before = sent[:m.start()]
            cand_orgs = [r for r in rests if r['end'] <= m.start() + 1]
            org = cand_orgs[-1]['surface'] if cand_orgs else org_phrase_before(sent, m.start())
            bare = False
            if not org and m.group(0).startswith('출신'):
                org = bare_org_before(sent, m.start())
                bare = bool(org)
            # chefs that merely lead the ORG ("안성재 셰프의 모수") are not the career subject
            subj = [c for c in chefs if not (org and re.match(r'\s?(?:셰프|쉐프)?\s?의\s?' + re.escape(org[:2]), sent[c['end']:c['end'] + 12]))]
            names = [c['surface'] for c in subj] + [c['surface'] for c in new_chefs if not (org and re.match(r'\s?(?:셰프|쉐프)?\s?의\s?' + re.escape(org[:2]), sent[c['end']:c['end'] + 12]))]
            amb, inferred, chef = None, False, None
            if len(set(names)) == 1:
                chef = names[0]
            elif len(set(names)) > 1:
                near = [n for n in subj + new_chefs if n['end'] <= m.start()]
                chef, amb = (near[-1]['surface'], 'multiple_chefs_in_sentence') if near else (None, 'multiple_chefs_in_sentence')
            elif ctx and ctx.get('chef') and (PRONOUN_SUBJ.search(sent) or not names):
                chef, inferred, amb = ctx['chef']['name_ko'], True, 'subject_from_job_context'
            career_org = org
            if org and chef and cand_orgs and not bare and not amb:
                for extra_org in coordinated(rests, cand_orgs[-1], sent)[:-1] if cand_orgs[-1] in rests else []:
                    if extra_org['surface'] != org:
                        mk('career', chef=chef, org=extra_org['surface'], anchor=m.start(), inferred=inferred)
            if org and chef and bare:
                mk('career', chef=chef, org=org, anchor=m.start(), ambiguity=amb or 'org_name_unverified', epi='unverified')
            elif org and chef:
                mk('career', chef=chef, org=org, anchor=m.start(), ambiguity=amb, inferred=inferred)
            elif org and not bare:
                mk('career', chef=chef, org=org, anchor=m.start(), ambiguity=amb or 'org_or_chef_missing', epi='unverified')
        # ── current employment ──
        if hits['employment'] and not is_collab:
            m = hits['employment']
            allc = sorted([c for c in chefs] + new_chefs, key=lambda x: x['start'])
            led = lambda c: any(re.match(r'\s?(?:셰프|쉐프)?\s?의\s?' + re.escape(r['surface'][:2]), sent[c['end']:c['end'] + 12]) for r in rests)  # noqa: E731
            free = [c for c in allc if not led(c)]
            pick = nearest_before(free or allc, m.start())
            amb = None if pick else 'no_chef_in_sentence'
            if len({c['surface'] for c in free}) > 1 and not any(c['end'] <= m.start() for c in free):
                amb = 'multiple_chefs_in_sentence'
            cur = [r for r in rests if r['surface'] != career_org]
            if re.match(r'\s?(?:였|이었|였던|이었던)', sent[m.end():m.end() + 4]):
                cur = []                  # '가온재의 헤드셰프였으며' is a past role (career), not a current employment
            if m.group(0).startswith('의') and not any(r['end'] == m.start() or (r['end'] == m.start() - 1 and sent[r['end']] in '’\'"”」』') for r in cur):
                cur = []                  # 'X의 셰프' only means employment when X is a known restaurant right before 의
            if cur and (pick or amb):
                near = nearest_before(cur, m.start())
                led_rests = coordinated(cur, near, sent)
                for r in led_rests:
                    mk('employment_claim', chef=pick['surface'] if pick else None, rest=r['surface'], anchor=m.start(), ambiguity=amb)
        # ── appointment of a new chef ('X은 신임 총괄 셰프로 Y를 선임') ──
        ap = APPOINT.search(sent)
        if ap and not is_collab:
            nm = ap.group(1).strip()
            sj = SUBJ_BEFORE.search(sent[:ap.start()] + ' ')
            org_nm = (rests[0]['surface'] if rests else (sj.group(1).strip() if sj else None))
            if org_nm and not rests:
                toks = org_nm.split()
                while len(toks) > 1 and toks[0] in DESCRIPTORS:
                    toks.pop(0)
                org_nm = ' '.join(toks)
            if 2 <= len(nm) <= 24 and org_nm and nm != org_nm:
                mk('employment_claim', chef=nm, rest=org_nm, anchor=ap.start(), ambiguity=None if rests else 'org_name_unverified', extra={'appointment': True})
        # ── status events about a restaurant ──
        status_rests = [r for r in rests if r['surface'] != career_org]
        qv = (QUOTED_VENUE.search(sent) or QUOTED_OPEN.search(sent)) if hits['opening'] else None
        new_venue = object_venue or (clean_venue(qv.group(1)) if qv else None)
        if new_venue and any(norm_key(new_venue) == norm_key(r['surface']) for r in rests):
            new_venue = None
        if object_venue:
            subject_rest = object_venue
        elif new_venue:
            subject_rest = new_venue                  # a quoted, never-seen venue opened: it becomes a candidate target downstream
        elif status_rests:
            subject_rest = status_rests[0]['surface']
        else:
            subject_rest = ctx['restaurant']['name_ko'] if ctx and ctx.get('restaurant') and DEMONSTRATIVE.search(sent) else None
        inferred_subject = not status_rests and not new_venue and subject_rest is not None
        for label in ('opening', 'relocation', 'closure', 'rename'):
            if hits[label] and (subject_rest or chef_names):
                if label == 'opening' and is_collab and same_clause(sent, hits['collab'].start(), hits['opening'].start()):
                    continue          # a collab cue vetoes an 'opening' only inside the same headline clause
                if label == 'opening' and re.search(r'이전\s?(?:오픈|개점|했)', sent):
                    continue          # 'moved and reopened' is a relocation, not a new opening
                if label == 'closure' and negated and hits['relocation']:
                    continue          # '폐업 아닌 이전' denies closure and states relocation          # a guest-chef / pop-up 'opening' is an event, not a restaurant opening
                if label == 'opening' and hits['career'] and hits[label].group(0).startswith('선보'):
                    continue
                allc = sorted([c for c in chefs] + new_chefs, key=lambda x: x['start'])
                cn = nearest_before([c for c in allc if c['surface'] != career_org], hits[label].start())
                cn = cn['surface'] if cn else None
                if label == 'closure' and hits['closure'].group(0) == '휴업':
                    mk(label, chef=cn, rest=subject_rest, anchor=hits[label].start(), ambiguity='temporary_closure', inferred=inferred_subject)
                    continue
                mk(label, chef=cn, rest=subject_rest, anchor=hits[label].start(), inferred=inferred_subject,
                   ambiguity=None if (status_rests or inferred_subject) else 'restaurant_not_identified',
                   extra={'venue_format': 'popup'} if (label == 'opening' and popup) else None)
        if hits['menu'] and rests and not is_collab and not any(h for k, h in hits.items() if k in ('opening', 'closure', 'relocation', 'rename', 'career')):
            mk('menu', rest=rests[0]['surface'], anchor=hits['menu'].start(), epi='unverified' if (ocr_conf is not None and ocr_conf < 60) else None)
    # de-dup identical ids
    seen, uniq = set(), []
    for r in out:
        if r['extraction_id'] not in seen:
            seen.add(r['extraction_id'])
            uniq.append(r)
    return uniq


def needs_model(rec: dict) -> bool:
    """Only ambiguous cases may be offered to a model; everything else is settled by rules."""
    return bool(rec.get('ambiguity')) and rec['ambiguity'] not in ('planned_not_done', 'negated_statement', 'temporary_closure')
