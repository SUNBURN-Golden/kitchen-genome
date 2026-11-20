"""Public booking-page profile reader (CatchTable / Naver Place / Resy / Tock / OpenTable shop pages).

Policy first: a booking page is fetched ONLY through the polite net layer, so login walls, challenges and HTTP access refusals stop the host for the run.
As of 2026-10-28 every Korean platform forbids generic bots: app.catchtable.co.kr (`User-agent: *  Disallow: /`, only named search engines allowed),
m.place.naver.com / pcmap.place.naver.com (`Disallow: /`, and Naver Place is excluded by collection role), www.opentable.com (`Disallow: /`),
www.exploretock.com (Cloudflare challenge). We never present as a search-engine crawler to get in. Those pages therefore end as `fail_robots` /
`fail_blocked` and the gap is classified `inaccessible` — which is the honest result.

When a platform does allow us, its listing is a third party asserting facts about the restaurant -> epistemic `claim` (never `fact`).
The parser reads JSON-LD first, then embedded app state (`__NEXT_DATA__`, `__APOLLO_STATE__`) for a small set of well-known keys.
"""
import json
import re

from .official import extract_profile

BOOKING_HOSTS = ('catchtable.co.kr', 'catchtable.net', 'place.naver.com', 'tabling.co.kr', 'resy.com', 'opentable.com', 'exploretock.com', 'tock.com', 'toasttab.com')
STATE_RX = [re.compile(r'<script[^>]+id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S), re.compile(r'window\.__APOLLO_STATE__\s*=\s*(\{.*?\});\s*</script>', re.S)]
KEYS = {
    'address': ('roadAddress', 'roadAddr', 'shopAddress', 'fullAddress', 'address1'),
    'opened': ('openDate', 'openedAt', 'openingDate', 'foundingDate'),
    'chef': ('chefName', 'headChef', 'chef'),
    'status': ('businessStatus', 'shopStatus', 'isClosed', 'closed'),
    'name': ('shopName', 'name', 'venueName'),
}


def is_booking_host(host: str) -> bool:
    return any(host == h or host.endswith('.' + h) for h in BOOKING_HOSTS)


def _walk(o, found, depth=0):
    if depth > 12:
        return
    if isinstance(o, dict):
        for k, v in o.items():
            for field, keys in KEYS.items():
                if k in keys and isinstance(v, (str, int, bool)) and field not in found:
                    found[field] = v
            _walk(v, found, depth + 1)
    elif isinstance(o, list):
        for v in o[:200]:
            _walk(v, found, depth + 1)


def app_state(html: str) -> dict:
    found: dict = {}
    for rx in STATE_RX:
        m = rx.search(html)
        if m:
            try:
                _walk(json.loads(m.group(1)), found)
            except ValueError:
                pass
    return found


def extract_booking(html: str, text: str, jsonld: list, *, target: dict, host: str, published_at=None, content_hash='', title='') -> list[dict]:
    facts = extract_profile(text, jsonld, target=target, host=host, source_kind='booking_page', site_basis='booking', published_at=published_at,
                            content_hash=content_hash, title=title)
    st = app_state(html or '')
    nm = str(st.get('name') or '')
    if nm and not any(n and n.lower() in nm.lower() for n in (target.get('name_ko'), target.get('name_en'))):
        return facts                                   # app state belongs to some other shop: ignore it
    from .official import _fid, OFFICIAL_VERSION
    have = {(f['field'], f['value']) for f in facts}
    for field in ('address', 'opened', 'chef', 'status'):
        v = st.get(field)
        if v in (None, '', False) and field != 'status':
            continue
        if field == 'status':
            if v in (True, 'CLOSED', 'closed', 'PERMANENTLY_CLOSED', '폐업'):
                v = 'closed'
            else:
                continue
        if field == 'opened':
            m = re.match(r'((?:19|20)\d{2})(?:[-.](\d{2}))?', str(v))
            if not m:
                continue
            v = m.group(1) + (f'-{m.group(2)}' if m.group(2) else '')
        v = str(v).strip()
        if (field, v) in have:
            continue
        facts.append({'fact_id': _fid(content_hash, field, v), 'target_id': target['target_id'], 'field': field, 'value': v, 'epistemic': 'claim',
                      'source_kind': 'booking_page', 'site_basis': 'booking', 'quote': f'{host} listing data: {field}={v}'[:200], 'extractor': f'state:{field}',
                      'extractor_version': OFFICIAL_VERSION, 'published_at': published_at})
    return facts
