"""Deterministic TV/drama/entertainment pre-filter: skip paid extraction/review of fiction-dominated articles.

A document is skipped only when its URL path carries an entertainment section segment, OR when >=2
distinct drama/competition cue families dominate the body (first-to-last cue span covers at least
MIN_SPAN_RATIO of the boilerplate-cut body) and the body shows no real-restaurant contact signal.
Chef interviews that merely mention a show keep passing: their show vocabulary is clustered (small
span), and 출연/방송/생존/미션/심사/우승 are deliberately NOT cues. Pure re/urllib/unicodedata:
no DB, no kg_ai imports (no cycles).
"""
import re
import unicodedata
from urllib.parse import unquote, urlsplit

REASON = 'skipped_entertainment'

URL_SEGMENTS = {'tv', 'entertainment', 'entertain', 'drama', '연예'}   # whole path segments, never host/query substrings

CORE_CUES = {
    '드라마': r'드라마(?!\s?같은)',   # "드라마 같은 인생" is a metaphor, not a cue
    '극중': r'극\s?중',
    '첫방송': r'첫\s?방송',
    '서바이벌': r'서바이벌',
    '생존팀': r'생존팀',
    '탈락': r'탈락',
    '예능': r'예능',
}
_CUE_PATTERNS = {fam: re.compile(pat) for fam, pat in CORE_CUES.items()}

# Site chrome (related-articles / most-read / pick widgets) must not contaminate the body's cues.
BOILERPLATE = re.compile(r"관련기사|무단\s?전재|저작권자|기사제보|bnt['’]s\s?Pick|많이 본 뉴스", re.I)

MIN_DISTINCT_CUES = 2
MIN_SPAN_RATIO = 0.2     # cues must dominate the body, not sit in one localised mention

# Real-restaurant contact info vetoes the TEXT rule (never the URL rule). Both the raw patterns and
# the sanitize_before_store markers are recognised: gate 1 sees raw text, gates 2/3 see sanitized text.
REAL_WORLD = (
    re.compile(r'0\d{1,2}[-–.]\s?\d{3,4}[-–.]\s?\d{4}'),                       # phone
    re.compile(r'(서울|부산|대구|인천|광주|대전|울산|세종|경기|강원|충북|충남|전북|전남|경북|경남|제주)'
               r'[^\n]{0,60}?(로|길)\s?\d+'),                                  # road address
)
REDACTED_REAL_WORLD = ('[REDACTED_PHONE]', '[REDACTED_ADDRESS]')


def url_cue(url: str | None) -> str | None:
    """The matched entertainment path segment (path only: never host/query/fragment). None-safe."""
    if not url:
        return None
    for seg in unquote(urlsplit(url).path).split('/'):
        seg = unicodedata.normalize('NFC', seg).casefold()
        if seg in URL_SEGMENTS:
            return seg
    return None


def _body(text: str | None) -> str:
    t = unicodedata.normalize('NFC', text or '')
    m = BOILERPLATE.search(t)
    return t[:m.start()] if m else t


def _hits(body: str) -> list[tuple[int, int, str]]:
    return sorted((m.start(), m.end(), fam) for fam, pat in _CUE_PATTERNS.items() for m in pat.finditer(body))


def core_cues(text: str | None) -> list[str]:
    """Distinct cue families in the body (NFC-normalised, cut at the first boilerplate marker)."""
    return sorted({fam for _, _, fam in _hits(_body(text))})


def entertainment_detail(url: str | None, text: str | None) -> dict:
    """Audit payload: which rule fired and the measured dominance of the cues."""
    body = _body(text)
    hits = _hits(body)
    span = hits[-1][1] - hits[0][0] if hits else 0
    return {'url_cue': url_cue(url), 'cues': sorted({fam for _, _, fam in hits}),
            'body_chars': len(body), 'cue_span': span, 'span_ratio': span / len(body) if body else 0.0,
            'real_world': any(p.search(body) for p in REAL_WORLD) or any(marker in body for marker in REDACTED_REAL_WORLD)}


def entertainment_reason(url: str | None, text: str | None) -> str | None:
    """'skipped_entertainment' when the URL section is entertainment, or drama/competition cues dominate
    a body with no real-restaurant contact signal; else None (the paid model remains the backstop)."""
    d = entertainment_detail(url, text)
    if d['url_cue']:
        return REASON
    if len(d['cues']) >= MIN_DISTINCT_CUES and d['cue_span'] >= MIN_SPAN_RATIO * d['body_chars'] and not d['real_world']:
        return REASON
    return None
