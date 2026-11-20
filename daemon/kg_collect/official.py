"""Official-site + booking-page profile extractor (rules only, no model).

Reads a restaurant's OWN page (its site, its JSON-LD, or a public booking page where robots allow) and emits field-level profile facts:
opened date, chef, address, status (operating / closed), relocation. Each value carries a <=200-char quote from the page.

Epistemic rules
- the restaurant announcing its own status/opening/chef on a site we have tied to it (seed or a press article linking it) -> `fact`
- a site tied to it only by a verified domain probe, or a booking platform's listing -> `claim` (a source asserting it)
- negated / future / 'since <year> Michelin' style statements -> nothing (we would rather leave the gap open)
"""
import hashlib
import re

from . import EXTRACTOR_VERSION
from .extract import clean_name

OFFICIAL_VERSION = 'official/1.0'
MONTHS = {m: i for i, m in enumerate(['jan', 'feb', 'mar', 'apr', 'may', 'jun', 'jul', 'aug', 'sep', 'oct', 'nov', 'dec'], 1)}

OPEN_KO = re.compile(r'((?:19|20)\d{2})\s*년(?:\s*(\d{1,2})\s*월)?(?:\s*(\d{1,2})\s*일)?\s*(?:에\s*)?(?:처음\s*)?(?:문을\s*열|오픈|개업|개점|개관|탄생|첫\s*선)')
OPEN_EN = re.compile(r'\b(?:since|est\.?|established(?:\s+in)?|opened(?:\s+its\s+doors)?\s+in|founded\s+in|first\s+opened\s+in)\s+(?:in\s+)?(?:(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+)?((?:19|20)\d{2})\b', re.I)
AWARD = re.compile(r'michelin|미쉐린|미슐랭|star|스타|award|어워드|선정|50\s*best|blue\s*ribbon|블루리본', re.I)
CHEF_EN_A = re.compile(r'\b(?:Executive\s+|Head\s+|Owner\s+|Founding\s+)?Chef(?:\s*/\s*Owner)?\s*[:\-–]?\s+([A-Z][a-z]+(?:[ -][A-Z][a-z]+){1,2})\b')
CHEF_EN_B = re.compile(r'\b([A-Z][a-z]+(?:[ -][A-Z][a-z]+){1,2}),?\s+(?:is\s+the\s+|our\s+)?(?:Executive|Head|Owner|Founding)\s+Chef\b')
CHEF_KO_A = re.compile(r'(?:오너|총괄|헤드|수석|대표|주방장\s*겸)\s*셰프\s*[:：]?\s*([가-힣]{2,4})(?![가-힣])')
CHEF_KO_B = re.compile(r'([가-힣]{2,4})\s+(?:오너|총괄|헤드|수석|대표)\s*셰프')
# Headline-style text ('Chef Eunji Lee Opens Lysée', 'Pastry Chef Makes Realistic Corn', 'Chef James Beard Awards') made the capitalised-run
# pattern swallow verbs and titles: trailing verbs/fillers are cut, and runs containing a non-name word or starting with one are rejected.
EN_NOT_NAME = {'Awards', 'Award', 'Prize', 'Guide', 'Restaurant', 'Restaurants', 'Bakery', 'Kitchen', 'Menu', 'Dining', 'Best', 'Star', 'Stars', 'Michelin',
               'Her', 'His', 'Their', 'Own', 'New', 'Our', 'The', 'Of', 'And', 'Pastry', 'Executive', 'Head', 'Sous', 'Team', 'Table'}
EN_VERBISH = re.compile(r'^(?:[A-Z][a-z]+(?:s|ed|ing))$|^(?:Makes|Opens|Creates|Debuts|Brings|Returns|Leads|Takes|Joins|Launches|Is|Was|Will|To|At|In|On|For|With)$')


def clean_en_name(nm: str):
    toks = nm.strip().split()
    if any(t in EN_NOT_NAME for t in toks):
        return None
    while len(toks) > 2 and EN_VERBISH.match(toks[-1]):
        toks.pop()                                     # 'Eunji Lee Creates' -> 'Eunji Lee'
    if len(toks) < 2 or EN_VERBISH.match(toks[-1]) and toks[-1] not in {'Lees', 'Jones', 'Andrews', 'Edwards', 'Williams', 'Roberts'} \
            or toks[0] in {'Makes', 'Opens', 'Creates', 'Debuts', 'Brings', 'Returns', 'Leads', 'Takes', 'Joins', 'Launches', 'Is', 'Was', 'Will', 'To', 'At', 'In', 'On', 'For', 'With'}:
        return None
    return ' '.join(toks)


NOT_PERSON_EN = {'The Chef', 'Our Chef', 'Tasting Menu', 'New York', 'Michelin Guide', 'Chef Table', 'Private Dining'}
ADDR_KO = re.compile(r'(?:서울(?:특별시|시)?|부산(?:광역시)?|경기도?|울산(?:광역시)?|제주(?:특별자치도)?|인천(?:광역시)?|대구(?:광역시)?|대전(?:광역시)?|광주(?:광역시)?)\s+'
                     r'[가-힣]+(?:구|시|군)\s+(?:[가-힣]+(?:동|읍|면)\s+)?[가-힣0-9]+(?:로|길)\s*\d+(?:-\d+)?(?:\s*,?\s*(?:지하\s*)?\d+\s*층)?')
ADDR_EN = re.compile(r'\b\d{1,5}\s+(?:[NSEW]\.?\s+)?(?:\d+(?:st|nd|rd|th)|[A-Z][a-z]+)(?:\s+[A-Z][a-z]+){0,3}\s+(?:St|Street|Ave|Avenue|Blvd|Road|Rd|Place|Pl|Broadway)\b\.?'
                     r'(?:,?\s*(?:[A-Z][a-z]+\s*){1,2})?,?\s*(?:New\s+York|NY|Brooklyn)\b(?:,?\s*NY)?(?:\s*\d{5})?')
CLOSED = re.compile(r'영업을?\s*(?:종료|마감)(?:합니다|하였|했|하게\s*되었)|폐업(?:합니다|하였|했|하게)|문을\s*닫(?:습니다|았|게\s*되었)|permanently\s+closed|closed\s+permanently|'
                    r'has\s+(?:now\s+)?closed|we\s+have\s+closed|(?:our|the)\s+(?:final|last)\s+service\s+(?:was|will\s+be)')
TEMP = re.compile(r'임시|휴무|휴업|명절|연휴|holiday|temporar|for\s+the\s+season|vacation|정기\s*휴|break', re.I)
MOVED = re.compile(r'(?:으로|로)\s*이전(?:했|하였|합니다|하게\s*되었|\s*오픈)|이전\s*(?:안내|오픈)|새로운\s*(?:공간|곳)으로\s*(?:이전|옮)|relocat(?:ed|ing|ion)\s+to|(?:have|has)\s+moved\s+to|our\s+new\s+(?:home|location)', re.I)
NEG = re.compile(r'아닙니다|않습니다|않았|않는|no\s+longer|not\s+closed|rumou?r|루머|사실이\s*아', re.I)
HOURS = re.compile(r'영업\s*시간|오픈\s*시간|런치\s*\d|디너\s*\d|\b(?:hours|opening\s+hours|reservations?|book\s+a\s+table|lunch|dinner)\b', re.I)


def _q(text, s, e):
    a, b = max(0, s - 60), min(len(text), e + 60)
    return text[a:b].replace('\n', ' ').strip()[:200]


def _fid(content_hash, field, value):
    return 'pf_' + hashlib.sha256(f'{content_hash}|{field}|{value}|{OFFICIAL_VERSION}'.encode()).hexdigest()[:20]


def _types(d):
    t = d.get('@type')
    return {t} if isinstance(t, str) else set(t or [])


def _ld_restaurants(jsonld):
    out = []
    for d in jsonld or []:
        if not isinstance(d, dict):
            continue
        if _types(d) & {'Restaurant', 'FoodEstablishment', 'LocalBusiness', 'BarOrPub', 'CafeOrCoffeeShop', 'Bakery'}:
            out.append(d)
    return out


def _ld_address(a):
    if isinstance(a, str):
        return a.strip() or None
    if isinstance(a, dict):
        parts = [a.get(k) for k in ('streetAddress', 'addressLocality', 'addressRegion', 'postalCode')]
        s = ', '.join(str(p).strip() for p in parts if p)
        return s or None
    return None


def _person_name(p):
    if isinstance(p, dict):
        return p.get('name')
    return p if isinstance(p, str) else None


def names_match(text: str, target: dict) -> bool:
    low = text.lower()
    for n in (target.get('name_ko'), target.get('name_en')):
        if n and len(n) >= 2 and n.lower() in low:
            return True
    return False


def extract_profile(text: str, jsonld: list | None, *, target: dict, host: str, source_kind: str, site_basis: str,
                    published_at: str | None = None, content_hash: str = '', title: str = '') -> list[dict]:
    """target: {'target_id','name_ko','name_en'}. Returns profile-fact dicts (not yet stored)."""
    epi = 'fact' if source_kind in ('official_site', 'official_jsonld') and site_basis in ('seed', 'article_link') else 'claim'
    out, seen = [], set()

    def add(field, value, quote, extractor, kind=source_kind):
        value = re.sub(r'\s+', ' ', str(value)).strip()
        if not value or (field, value) in seen:
            return
        seen.add((field, value))
        out.append({'fact_id': _fid(content_hash, field, value), 'target_id': target['target_id'], 'field': field, 'value': value, 'epistemic': epi,
                    'source_kind': kind, 'site_basis': site_basis, 'quote': quote[:200], 'extractor': extractor, 'extractor_version': OFFICIAL_VERSION,
                    'published_at': published_at})

    # 1) JSON-LD: the site describing itself in structured form
    for d in _ld_restaurants(jsonld):
        nm = str(d.get('name') or '')
        if nm and not names_match(nm + ' ' + title, target):
            continue                                   # a group site listing many venues: only the block naming OUR restaurant counts
        kind = 'official_jsonld' if source_kind == 'official_site' else source_kind
        addr = _ld_address(d.get('address'))
        if addr:
            add('address', addr, f'JSON-LD address: {addr}', 'ld:address', kind)
        fd = str(d.get('foundingDate') or '')
        m = re.match(r'((?:19|20)\d{2})(?:-(\d{2}))?', fd)
        if m:
            add('opened', m.group(1) + (f'-{m.group(2)}' if m.group(2) else ''), f'JSON-LD foundingDate: {fd}', 'ld:foundingDate', kind)
        for key in ('founder', 'employee', 'chef', 'author'):
            ps = d.get(key)
            for p in (ps if isinstance(ps, list) else [ps]):
                pn = _person_name(p)
                jt = str(p.get('jobTitle', '')) if isinstance(p, dict) else ''
                if pn and (key in ('chef', 'founder') or re.search(r'chef|셰프', jt, re.I)):
                    add('chef', pn.strip(), f'JSON-LD {key}: {pn} {jt}'.strip(), f'ld:{key}', kind)
        if d.get('openingHours') or d.get('openingHoursSpecification'):
            add('status', 'operating', 'JSON-LD openingHours present on the restaurant\'s own page', 'ld:openingHours', kind)

    # 2) page text
    t = text or ''
    for m in OPEN_KO.finditer(t):
        sent = _q(t, m.start(), m.end())
        if NEG.search(sent) or re.search(r'예정|계획|앞두', sent):
            continue
        y, mo, dd = m.group(1), m.group(2), m.group(3)
        v = y + (f'-{int(mo):02d}' if mo else '') + (f'-{int(dd):02d}' if mo and dd else '')
        add('opened', v, sent, 'rule:open_ko')
    for m in OPEN_EN.finditer(t):
        sent = t[max(0, m.start() - 40): m.end() + 60]
        if AWARD.search(sent) and not re.search(r'open|found|establish', m.group(0), re.I):
            continue                                   # 'since 2017, one Michelin star' is not an opening
        mo = MONTHS.get((m.group(1) or '').lower()[:3])
        add('opened', m.group(2) + (f'-{mo:02d}' if mo else ''), _q(t, m.start(), m.end()), 'rule:open_en')
    for rx, tag in ((CHEF_EN_A, 'rule:chef_en'), (CHEF_EN_B, 'rule:chef_en')):
        for m in rx.finditer(t):
            nm = clean_en_name(m.group(1))
            if not nm or nm in NOT_PERSON_EN or names_match(nm, target):
                continue
            add('chef', nm, _q(t, m.start(), m.end()), tag)
    for rx in (CHEF_KO_A, CHEF_KO_B):
        for m in rx.finditer(t):
            raw_nm = m.group(1)
            if len(raw_nm) == 4 and raw_nm[-1] in '가이':
                raw_nm = raw_nm[:-1]                   # '김민수가 이끈다': subject particle
            nm = clean_name(raw_nm, t[m.end(): m.end() + 4])
            if nm and not names_match(nm, target):
                add('chef', nm, _q(t, m.start(), m.end()), 'rule:chef_ko')
    for rx, tag in ((ADDR_KO, 'rule:addr_ko'), (ADDR_EN, 'rule:addr_en')):
        for m in rx.finditer(t):
            add('address', m.group(0), _q(t, m.start(), m.end()), tag)
    for m in CLOSED.finditer(t):
        sent = _q(t, m.start(), m.end())
        if NEG.search(sent) or TEMP.search(sent):
            continue
        add('status', 'closed', sent, 'rule:closed')
    for m in MOVED.finditer(t):
        sent = _q(t, m.start(), m.end())
        if NEG.search(sent):
            continue
        add('relocation', sent, sent, 'rule:moved')
    if not any(f['field'] == 'status' for f in out) and source_kind == 'official_site' and HOURS.search(t) and names_match(t + ' ' + title, target):
        m = HOURS.search(t)
        add('status', 'operating', _q(t, m.start(), m.end()), 'rule:hours_live')
    return out


def store(conn, facts: list[dict], *, fetch_id: str, canonical_url: str, host: str, now: str) -> int:
    n = 0
    for f in facts:
        n += conn.execute("""INSERT OR IGNORE INTO profile_fact(fact_id,fetch_id,canonical_url,host,target_id,field,value,epistemic,source_kind,site_basis,quote,extractor,extractor_version,published_at,created_at)
                             VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                          (f['fact_id'], fetch_id, canonical_url, host, f['target_id'], f['field'], f['value'], f['epistemic'], f['source_kind'], f['site_basis'], f['quote'],
                           f['extractor'], f['extractor_version'], f.get('published_at'), now)).rowcount
    return n


def _host(u: str) -> str:
    import urllib.parse
    h = urllib.parse.urlsplit(u).netloc.lower().split(':')[0]
    return h[4:] if h.startswith('www.') else h


def register_site(conn, target_id: str, url: str, basis: str, verified: bool, evidence: str, now: str) -> int:
    return conn.execute("""INSERT INTO official_site(target_id,url,host,basis,verified,evidence,found_at) VALUES (?,?,?,?,?,?,?)
                           ON CONFLICT(target_id,url) DO UPDATE SET verified=MAX(verified,excluded.verified)""",
                        (target_id, url, _host(url), basis, int(verified), evidence[:300], now)).rowcount


def site_basis_for(conn, target_id: str, host: str, now: str | None = None) -> str | None:
    """Is `host` this restaurant's own site? seed `site` field, or a verified official_site row. Returns the basis or None."""
    import json
    h = host[4:] if host.startswith('www.') else host
    try:
        r = conn.execute("""SELECT basis FROM official_site WHERE target_id=? AND host=? AND verified=1
                            ORDER BY CASE basis WHEN 'seed' THEN 0 WHEN 'article_link' THEN 1 ELSE 2 END LIMIT 1""",
                         (target_id, h)).fetchone()
    except Exception:
        return None
    if r:
        return r['basis']
    t = conn.execute('SELECT attrs_json FROM target WHERE target_id=?', (target_id,)).fetchone()
    site = json.loads(t['attrs_json']).get('site') if t else None
    if site and _host(site) == h and not re.search(r'instagram|facebook|naver|catchtable', site):
        if now:
            register_site(conn, target_id, site, 'seed', True, 'seed data site field', now)
        return 'seed'
    return None


# ───────── official-site discovery: candidate URLs + strict verification ─────────
REST_SIGNAL = re.compile(r'reserv|booking|book\s+a\s+table|menu|tasting|dining|restaurant|chef|예약|메뉴|코스|다이닝|레스토랑|셰프|오마카세|omakase', re.I)
SPAM = re.compile(r'casino|카지노|토토|\bbet(?:ting)?\b|slot|giriş|porn|domain\s+(?:is\s+)?for\s+sale|buy\s+this\s+domain|parked\s+free|hugedomains|sedo|godaddy|under\s+construction', re.I)
CITY = re.compile(r'seoul|서울|korea|한국|new\s*york|nyc|뉴욕|manhattan|brooklyn|busan|부산|ulsan|울산|gangnam|강남|청담|한남|성수|서초|명동|논현|신사|역삼|북촌|정동', re.I)
STOP = {'restaurant', 'the', 'and', 'by', 'seoul', 'nyc', 'kr'}


def slug(name_en: str | None) -> str | None:
    if not name_en:
        return None
    words = [w for w in re.findall(r'[a-z0-9]+', name_en.lower().replace("'", '')) if w not in STOP]
    s = ''.join(words)
    return s if len(s) >= 4 else None


def probe_candidates(t) -> list[str]:
    """Domain guesses for a restaurant's own site. Cheap, polite (one GET each through robots), and ALWAYS verified before use."""
    import json
    s = slug(t['name_en'])
    if not s:
        return []
    area = json.loads(t['attrs_json'] or '{}').get('area') or ''
    nyc = '뉴욕' in area
    c = ([f'https://{s}nyc.com', f'https://www.{s}nyc.com', f'https://{s}.nyc'] if nyc else []) + [f'https://{s}.com', f'https://www.{s}.co.kr', f'https://{s}.kr', f'https://{s}seoul.com']
    return c[:5]


def verify_site(text: str, title: str, url: str, target: dict) -> tuple[bool, str]:
    """A guessed or press-linked page counts as the restaurant's own site only if it names the restaurant, looks like a restaurant site,
    mentions the right city/area, and is not spam/parked."""
    head = f'{title}\n{(text or "")[:4000]}'
    if SPAM.search(head):
        return False, 'spam_or_parked'
    named = names_match(head, target)
    s = slug(target.get('name_en'))
    if not named and s and s in re.sub(r'[^a-z0-9]', '', head.lower()):
        named = True
    if not named:
        return False, 'restaurant_name_absent'
    if not REST_SIGNAL.search(head):
        return False, 'no_restaurant_signal'
    if not CITY.search(head + ' ' + url):
        return False, 'no_city_or_area'
    return True, f'names {target.get("name_en") or target.get("name_ko")}; restaurant signal; city/area present'


def article_link_candidates(links, text, target, banned=()) -> list[str]:
    """Outbound links in a press article that look like THE restaurant's site: host contains the restaurant's slug."""
    s = slug(target.get('name_en'))
    if not names_match(text or '', target):
        return []
    out = []
    for u, anchor, _r in links:
        h = _host(u)
        if not h or any(h == b or h.endswith('.' + b) for b in banned):
            continue
        slug_match = s and s in re.sub(r'[^a-z0-9]', '', h.split('.')[0] if h.count('.') <= 2 else h)
        named_link = names_match(anchor or '', target) and re.search(r'공식\s*(?:홈페이지|사이트)|official (?:site|website)', anchor, re.I)
        if slug_match or named_link:
            root = f'https://{h}/'
            if root not in out:
                out.append(root)
    return out
