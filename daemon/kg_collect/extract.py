"""Rule extractor -> LABELLED candidates. label = what the sentence says; epistemic = how far we may believe it.
Labels: career(경력) employment_claim(재직 주장) opening(오픈) relocation(이전) closure(폐업) rename menu(메뉴) collab(협업) mentorship_claim.
Epistemic: fact | claim | inference | unverified.   Rules first. Ambiguous sentences are flagged (extraction.ambiguity) and only then offered to
an optional model hook whose output is always epistemic='inference' and is never applied automatically.
Hard rules: guest events are 'collab', never employment. Co-working is 'collab/co_worker', never mentorship. Mentorship needs 사사/스승/제자 wording.
Nothing is filled in from outside the sentence; a value we cannot read stays NULL."""
import hashlib
import re

from . import EXTRACTOR_VERSION

SURNAMES = set('김이박최정강조윤장임한오서신권황안송류유홍전고문양손배백허남심노하곽성차주우구민나진지엄채원천방공현함변염여추도소석선설마길연위표명기반왕금옥육인맹제모탁국어은편용예경봉사부황보')
ROLE_RE = r'(총괄\s?셰프|총괄\s?쉐프|수\s?셰프|수석\s?셰프|헤드\s?셰프|헤드\s?쉐프|오너\s?셰프|오너\s?쉐프|부주방장|주방장|파티시에|소믈리에|셰프\s?드\s?파티|스태프|인턴|실습생|조리장|셰프|쉐프)'
ORG_SUFFIX = r'(?:레스토랑|식당|다이닝|키친|비스트로|호텔|스시|오마카세|그룹|바|카페|베이커리)'
CHEF_PAT = re.compile(r'(?<![가-힣])([가-힣]{2,5})\s?(?:오너\s?|총괄\s?|수\s?|헤드\s?|수석\s?)?(?:셰프|쉐프)')
CHEF_STOP = {'총괄', '헤드', '오너', '수석', '스타', '미쉐린', '미슐랭', '유명', '이탈리안', '프렌치', '한식', '일식', '중식', '양식', '파인', '신임', '신입', '라면', '요리', '레스토랑', '제과', '스시', '보조', '여성', '남성', '젊은', '현직', '전직', '그의', '그녀', '이들', '두', '세', '한', '이', '그', '저'}
SENT_SPLIT = re.compile(r'(?<=[.!?。])\s+|\n+')

PATTERNS = {
    'collab': re.compile(r'게스트\s?(?:셰프|쉐프|다이닝|디너)|팝업|콜라보|협업|초청|내한|원\s?나이트|투\s?나이트|스페셜\s?디너|합작|함께\s?선보|공동\s?개발|손잡고|손을\s?잡고'),
    'co_worker': re.compile(r'함께\s?(?:일했|근무|일한)|같이\s?(?:일했|근무)|동료|동기로|호흡을\s?맞춰|한솥밥'),
    'mentor': re.compile(r'사사|스승|제자|밑에서\s?(?:배웠|수련|배운)|가르침|멘토'),
    'career': re.compile(r'출신|에서\s?(?:(?:20\d{2}\s?년?\s?(?:부터|[~\-–])\s?(?:20\d{2}\s?년?|현재|지금)(?:까지)?\s?)?)(?:' + ROLE_RE + r'로?\s?)?(?:\d{1,2}\s?년(?:간|동안)?\s?)?(?:근무|일했|일한|경력|수련|근속|지냈|재직|배웠)|를\s?거쳐|을\s?거쳐|에서\s?' + ROLE_RE + r'(?:로|를)?\s?(?:맡|지냈|역임|활약|일)|경력을\s?(?:쌓|가진)|출신으로'),
    'employment': re.compile(r'(?:총괄|헤드|오너|수|수석)\s?(?:셰프|쉐프)(?:로|를)?\s?(?:부임|합류|영입|선임|취임|맡|이끌|활약|있|일하)|총괄(?:한다|하고\s?있|했)|(?:으)?로\s?(?:옮겼|이직했|이적했)|(?:이끌고\s?있|이끄는|맡고\s?있|합류했|합류한|부임했|영입했|영입된|선임됐)|(?<=[가-힣A-Za-z’\'”])의\s(?:총괄|헤드|오너|수|수석)?\s?(?:셰프|쉐프)'),
    'opening': re.compile(r'(?:그랜드\s?|가\s?|재\s?|신규\s?)?오픈(?:했|한다|할|하며|해|예정|을\s?앞|[\s,.]|$)|개업|문을\s?(?:연|열|열었)|[을를]\s?열었|개점|론칭'),
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
TENURE_RE = re.compile(r'(20\d{2})\s?(?:년)?\s?(?:[~\-–]|부터)\s?(20\d{2}|현재|지금)')
DURATION_RE = re.compile(r'(\d{1,2})\s?년(?:\s?간|\s?동안|\s?가량|\s?여)')


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
    if nm in NOT_A_NAME or after.lstrip().startswith(('들', '컬렉션', '클래스', '출신들')):
        return None
    if len(nm) >= 3 and nm[-1] in '은는의도를을와과':
        nm = nm[:-1]
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


# '<hotel/restaurant>은 신임 총괄 셰프로 ‘기욤 콤파라(Guillaume Comparat)’를 선임했다': appointment of a (possibly foreign, never-seen) chef.
APPOINT = re.compile(r'(?:총괄|헤드|수석|오너|수)?\s?(?:셰프|쉐프|주방장)(?:로|에)\s?[‘\'"“]?([가-힣A-Za-z][가-힣A-Za-z\s·.\-]{1,24}?)\s?(?:\([^)]{2,40}\))?[’\'"”]?(?:를|을)\s?(?:신임\s?|새\s?)?(?:선임|영입|임명|발탁|맞이|모셨|모시|영입했|내정)')
# '한식당 ‘세옥(SEOK)’이 문을 열었다' / '레스토랑, ‘SEOK’이 ...': venue-type noun + quoted name
QUOTED_VENUE = re.compile(r'(?:한식당|양식당|일식당|중식당|레스토랑|식당|다이닝|비스트로|오마카세|바|카페|베이커리|펍)[,\s]*[‘\'“"]([^’\'”"]{2,30})[’\'”"]')


def clean_venue(nm: str) -> str | None:
    nm = re.sub(r'\s*\([^)]*\)\s*$', '', nm).strip()
    if not 2 <= len(nm) <= 24 or nm.isdigit():
        return None
    hangul = re.sub(r'\([^)]*\)', '', nm)
    return hangul.strip() or None


def norm_key(x: str) -> str:
    return re.sub(r'[\s\-·.]', '', x).lower()


DESCRIPTORS = {'프렌치', '이탈리안', '럭셔리', '특급', '5성급', '글로벌', '국내', '유명', '호텔', '신규'}
SUBJ_BEFORE = re.compile(r'^(?:[“‘\'"\[(].{0,40}?[”’\'"\])]\s*)?([가-힣A-Za-z0-9·&\s]{2,25}?)(?:은|는|이|가)\s')


def nearest_before(items: list, pos: int):
    """Chef mention closest before `pos` (else the first one after). Mentions are dicts with start/end/surface."""
    before = [i for i in items if i['end'] <= pos]
    return before[-1] if before else (items[0] if items else None)


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


def extract_text(text: str, *, gaz, host: str, published_at: str | None = None, tier: str = 'press', ctx: dict | None = None,
                 ocr_conf: float | None = None, official_hosts: set | None = None, content_hash: str = '') -> list[dict]:
    """ctx: {'chef': target-row?, 'restaurant': target-row?} the job's target (used ONLY when the sentence has no subject; marks epistemic=inference)."""
    out = []
    pub_year = (published_at or '')[:4] or None
    official_hosts = official_hosts or set()
    for off, sent in sentences(text):
        if len(sent) < 8:
            continue
        mentions = gaz.find(sent)
        known = [(m['start'], m['end']) for m in mentions]
        chefs = [m for m in mentions if m['kind'] == 'chef']
        rests = [m for m in mentions if m['kind'] == 'restaurant']
        new_chefs = new_chef_names(sent, known)
        hits = {k: p.search(sent) for k, p in PATTERNS.items()}
        ar = AT_RISK.search(sent) or COND_CLOSE.search(sent)
        if ar and hits['closure'] and ar.start() <= hits['closure'].start() <= ar.end():
            hits['closure'] = None                    # 'closure crisis' / 'closes if sales fail' is a risk or a rule, not a closure
        if not any(hits.values()) and not APPOINT.search(sent):
            continue
        base_unverified = bool(HEARSAY.search(sent)) or tier in ('social_lead', 'search_lead') or (ocr_conf is not None and ocr_conf < 60)
        negated = bool(NEGATION.search(sent))
        future = bool(FUTURE.search(sent))
        dm = DATE_RE.search(sent)
        date_iso, date_note = iso_from_dateish(dm, pub_year)
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
                   'date_text': dm.group(0) if dm and label != 'career' else None, 'date_iso': date_iso if label != 'career' else None, 'date_note': date_note if label != 'career' else None,
                   'tenure_from': tm.group(1) if tm and label in ('career', 'employment_claim') else None,
                   'tenure_to': (tm.group(2) if tm and tm.group(2) not in ('현재', '지금') else None) if tm and label in ('career', 'employment_claim') else None,
                   'span_start': off, 'span_end': off + len(sent), 'quote': _quote(sent, anchor), 'extractor': f'rule:{label}',
                   'extractor_version': EXTRACTOR_VERSION, 'ambiguity': amb, 'published_at': published_at, **(extra or {})}
            rec['extraction_id'] = 'ext_' + hashlib.sha1('|'.join([content_hash, str(rec['span_start']), label, str(chef), str(rest), str(org), EXTRACTOR_VERSION]).encode()).hexdigest()[:14]
            out.append(rec)

        chef_names = [c['surface'] for c in chefs] + [c['surface'] for c in new_chefs]
        # ── collab / co-working first: they veto employment readings ──
        is_collab = bool(hits['collab'])
        if is_collab and (chef_names or rests):
            mk('collab', chef=chef_names[0] if chef_names else None, rest=rests[0]['surface'] if rests else None, anchor=hits['collab'].start(),
               extra={'collab_kind': 'guest_event'})
        if hits['co_worker'] and not hits['mentor'] and (chef_names or rests):
            mk('collab', chef=chef_names[0] if chef_names else None, rest=rests[0]['surface'] if rests else None, anchor=hits['co_worker'].start(),
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
            if org and chef and bare:
                mk('career', chef=chef, org=org, anchor=m.start(), ambiguity=amb or 'org_name_unverified', epi='unverified')
            elif org and chef:
                mk('career', chef=chef, org=org, anchor=m.start(), ambiguity=amb, inferred=inferred,
                   rest=next((r['surface'] for r in rests if r['surface'] != org), None))
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
            if m.group(0).startswith('의') and not any(r['end'] == m.start() for r in cur):
                cur = []                  # 'X의 셰프' only means employment when X is a known restaurant right before 의
            if cur and (pick or amb):
                near = nearest_before(cur, m.start())
                led_rests = [near] + [r for r in cur if r is not near and r['start'] > near['start'] and re.search(r'(?:과|와|,|·|및)\s?[\'’"”]?$', sent[near['end']:r['start']].strip() + ' ')] if hits['employment'].group(0).startswith(('이끄', '맡')) else [near]
                for r in led_rests[:3]:
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
        qv = QUOTED_VENUE.search(sent) if hits['opening'] else None
        new_venue = clean_venue(qv.group(1)) if qv else None
        if new_venue and any(norm_key(new_venue) == norm_key(r['surface']) for r in rests):
            new_venue = None
        if new_venue:
            subject_rest = new_venue                  # a quoted, never-seen venue opened: it becomes a candidate target downstream
        elif status_rests:
            subject_rest = status_rests[0]['surface']
        else:
            subject_rest = ctx['restaurant']['name_ko'] if ctx and ctx.get('restaurant') and DEMONSTRATIVE.search(sent) else None
        inferred_subject = not status_rests and not new_venue and subject_rest is not None
        for label in ('opening', 'relocation', 'closure', 'rename'):
            if hits[label] and (subject_rest or chef_names):
                if label == 'opening' and is_collab:
                    continue
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
                   ambiguity=None if (status_rests or inferred_subject) else 'restaurant_not_identified')
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
