"""Kitchen Genome Brand-Specific Fact Extractor and Homonym Disambiguator.
Extracts: chef, restaurant, origin_kitchen, tenure_period, role, independence, relocation, closure, price.
Links every extracted relation to its evidence URL, short quote (<=200 chars), and character/paragraph locator.
Disambiguates homonyms (동명이인 분리) by pairing chef name with verified kitchen/restaurant/era context.
"""
import re
import urllib.parse
from privacy_filter import extract_relevant_dom_region, sanitize_before_store

OFFICIAL_HOSTS = {
    'lyseenyc.com', 'jungsik.com', 'odrenyc.com', 'iter-restaurants.com',
    'the-italian-club.lovable.app', 'sotetsu-hotels.com'
}
PRESS_HOSTS = {
    'mingchu.io', 'bluer.co.kr', 'hotelrestaurant.co.kr', 'esquirekorea.co.kr',
    'foodnews.news', 'bakerynews.co.kr', 'cooknchefnews.com', 'maisonkorea.com',
    'vegannews.co.kr', 'imaeil.com', 'nyculturebeat.com', 'mk.co.kr',
    'heypop.kr', 'joongang.co.kr', 'economist.co.kr', 'dealseoul.com',
    'ny.eater.com', 'dafanew.com', 'metizen.co.kr', 'alive.businessweekly.com.tw',
    'restaurantsforkings.com', 'winein.co.kr', 'ktriptips.com'
}
BLOG_OR_COMMUNITY_HOSTS = {
    'tistory.com', 'brunch.co.kr', 'blog.naver.com', 'm.blog.naver.com',
    'polle.com', 'diningcode.com', 'siksinhot.com', 'catchtable.net'
}

RE_PERIOD = re.compile(r'\b(20\d{2}\s*[~∼\-–]\s*(?:20\d{2}|현재)|\d+\s*년(?:\s*간|\s*근무)?)\b')
RE_YEAR_MONTH = re.compile(r'\b(20[12]\d(?:[-.](?:0?[1-9]|1[0-2]))?)\b')
RE_ROLE = re.compile(
    r'(총괄\s*페이스트리\s*셰프|총괄\s*셰프|헤드\s*셰프|수셰프|파티시에|오너\s*셰프|개업\s*멤버|창립\s*멤버|'
    r'Executive\s+Pastry\s+Chef|Executive\s+Chef|Head\s+Chef|Sous\s+Chef|Owner\s+Chef|Pastry\s+Chef)',
    re.I
)
RE_PRICE = re.compile(r'(\d{1,3}(?:,\d{3})+\s*원|\d+(?:\.\d+)?\s*만\s*원|\$\s*\d{2,4})')


def classify_source_tier(url: str) -> str:
    host = urllib.parse.urlparse(url or '').netloc.lower().removeprefix('www.')
    if not host:
        return 'search_lead'
    if any(h in host for h in OFFICIAL_HOSTS):
        return 'official'
    if any(h in host for h in PRESS_HOSTS):
        return 'press'
    if any(h in host for h in BLOG_OR_COMMUNITY_HOSTS):
        return 'specialist_blog'
    return 'search_lead'


def resolve_chef_entity(
    chef_name: str | None,
    origin_kitchen: str | None,
    restaurant_id: str | None,
    known_registry: dict | None = None
) -> dict:
    """Disambiguate chef identity (동명이인 분리).
    Builds a canonical entity ID combining chef name and origin/restaurant context,
    and checks against known_registry to detect homonym collisions."""
    clean_name = re.sub(r'\s*\(.*?\)', '', (chef_name or '')).strip()
    if not clean_name or any(tok in clean_name for tok in ('미기재', '출신', '호텔 팀')):
        return {
            'chef_entity_id': f'unnamed::{restaurant_id or "unknown"}',
            'chef_name_clean': None,
            'homonym_conflict': False,
            'homonym_note': '실명 미확인 셰프 엔티티'
        }

    scope = origin_kitchen or restaurant_id or 'unscoped'
    entity_id = f'chef:{clean_name}@{scope}'
    homonym_conflict = False
    homonym_note = None

    if known_registry and clean_name in known_registry:
        existing_scopes = known_registry[clean_name]
        # If the same Korean name appears in an unrelated origin kitchen without shared restaurant ID
        if scope not in existing_scopes and len(existing_scopes) > 0:
            homonym_conflict = True
            homonym_note = f'동명이인 후보 분리 필요: 기존 {sorted(existing_scopes)} vs 신규 {scope}'

    return {
        'chef_entity_id': entity_id,
        'chef_name_clean': clean_name,
        'homonym_conflict': homonym_conflict,
        'homonym_note': homonym_note
    }


def locate_evidence(text: str, keywords: list[str], max_quote: int = 200) -> dict:
    """Find the paragraph and character offset containing the strongest match and return a short quote."""
    if not text:
        return {'paragraph_idx': 0, 'char_start': 0, 'char_end': 0, 'quote': ''}
    paragraphs = [p.strip() for p in re.split(r'\n+', text) if p.strip()] or [text]
    best_p_idx = 0
    best_pos = 0
    best_score = -1

    for idx, p in enumerate(paragraphs):
        score = sum(1 for k in keywords if k and k.lower() in p.lower())
        if score > best_score:
            best_score = score
            best_p_idx = idx
            for k in keywords:
                if k and k.lower() in p.lower():
                    best_pos = p.lower().find(k.lower())
                    break

    target_p = paragraphs[best_p_idx]
    start = max(0, best_pos - 40)
    end = min(len(target_p), start + max_quote)
    quote = target_p[start:end].strip()
    return {
        'paragraph_idx': best_p_idx,
        'char_start': start,
        'char_end': end,
        'quote': quote[:max_quote]
    }


def extract_kitchen_genome_facts(
    raw_html_or_text: str,
    url: str,
    restaurant_record: dict,
    known_chef_registry: dict | None = None
) -> dict:
    """Extract Kitchen Genome facts after running DOM region extraction and PII/injection filtering."""
    region_text = extract_relevant_dom_region(raw_html_or_text)
    clean_text, privacy_flags = sanitize_before_store(region_text)

    r_id = restaurant_record.get('id')
    r_name = restaurant_record.get('name')
    chef = restaurant_record.get('chef')
    origin_kitchen = restaurant_record.get('kitchen')

    role_m = RE_ROLE.search(clean_text)
    period_m = RE_PERIOD.search(clean_text)
    ym_m = RE_YEAR_MONTH.search(clean_text)
    price_m = RE_PRICE.search(clean_text)

    has_closure = any(w in clean_text for w in ('폐업', '폐점', '영업 종료', '문을 닫', 'closed'))
    has_relocation = any(w in clean_text for w in ('이전', '확장 이전', '자리로 옮', 'relocated'))
    has_independence = any(w in clean_text for w in ('독립', '오픈', '개업', '문을 연', 'opened'))

    entity_info = resolve_chef_entity(chef, origin_kitchen, r_id, known_chef_registry)
    search_terms = [x for x in [r_name, entity_info.get('chef_name_clean'), origin_kitchen] if x]
    locator = locate_evidence(clean_text, search_terms)
    tier = classify_source_tier(url)

    facts = {
        'chef': chef,
        'chef_entity_id': entity_info['chef_entity_id'],
        'homonym_conflict': entity_info['homonym_conflict'],
        'homonym_note': entity_info['homonym_note'],
        'restaurant': r_name,
        'restaurant_id': r_id,
        'origin_kitchen': origin_kitchen,
        'tenure_period': period_m.group(1) if period_m else restaurant_record.get('period'),
        'role': role_m.group(1) if role_m else restaurant_record.get('role'),
        'independence': ym_m.group(1) if (has_independence and ym_m) else restaurant_record.get('opened'),
        'relocation': True if has_relocation else False,
        'closure': ym_m.group(1) if (has_closure and ym_m) else restaurant_record.get('closed_at'),
        'price_clue': price_m.group(1) if price_m else restaurant_record.get('price'),
        'price_is_official': (tier == 'official') and bool(price_m),
    }

    return {
        'brand': 'kitchengenome',
        'target_id': r_id,
        'source_tier': tier,
        'extracted_facts': facts,
        'evidence_locator': {
            'url': url,
            'paragraph_idx': locator['paragraph_idx'],
            'char_start': locator['char_start'],
            'char_end': locator['char_end'],
            'quote': locator['quote'],
        },
        'privacy_audit': privacy_flags,
    }
