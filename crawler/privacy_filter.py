"""Pre-storage Privacy (PII) and Prompt-Injection Filter for Kitchen Genome.
Applied BEFORE saving any extracted text/fact to local storage or passing to AI/extractors.
Rules:
1. Extract only relevant DOM body/article regions (drop scripts, forms, inputs, nav, cookies, DMs).
2. Redact personal phone numbers, personal emails, residential addresses, resident registration numbers (주민번호),
   children/health/financial info, login cookies/tokens/passwords.
3. Preserve public professional culinary facts (chef name, kitchen, role, tenure period, opening/closure year) with source URL.
4. Detect and neutralize prompt-injection strings so page text is never treated as bot instructions.
"""
import re

# PII regex patterns
RE_RRN = re.compile(r'\b\d{6}\s*[-~]\s*[1-4]\d{6}\b')
RE_PHONE = re.compile(r'\b(?:01[016789]|02|0[3-6][1-5]|070)[-.\s]?\d{3,4}[-.\s]?\d{4}\b')
RE_EMAIL = re.compile(r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b')
RE_CARD_OR_ACCT = re.compile(r'\b(?:\d{4}[-\s]){3}\d{4}\b|\b\d{3,6}-\d{2,6}-\d{4,6}\b')
RE_SECRET_TOKEN = re.compile(
    r'(?i)(?:bearer\s+[a-z0-9_\-\.=]{12,}|(?:session|token|password|passwd|secret|cookie|api[_-]?key)\s*[=:]\s*[^\s,;\"\']{6,})'
)
RE_RESIDENTIAL_ADDR = re.compile(
    r'(?:[가-힣]+(?:시|도)\s+)?[가-힣]+(?:구|군|시)\s+[가-힣0-9]+(?:동|읍|면|로|길)\s+\d+(?:-\d+)?\s*(?:아파트|빌라|오피스텔|호|동\s*\d+호)'
)
RE_PRIVATE_CONTEXT = re.compile(
    r'(?i)(?:비밀번호|주민등록번호|계좌번호|카드번호|진료기록|건강검진|자녀\s*이름|아이\s*학교|어린이집|1:1\s*문의|다이렉트\s*메시지|DM\s*내역)'
)

# Prompt injection patterns
RE_PROMPT_INJECTION = re.compile(
    r'(?i)(?:이\s*명령을\s*실행|이전\s*지시를\s*무시|시스템\s*프롬프트|ignore\s+(?:all\s+)?previous\s+instructions|'
    r'disregard\s+all\s+prior|system\s*prompt\s*:|you\s+are\s+now\s+a|execute\s+this\s+command|'
    r'<\s*/?\s*cursor_untrusted_data|\[GROK_BOT_HIDDEN_PROMPT\])'
)

# HTML tag filters for extracting only relevant content region
RE_DROP_BLOCKS = re.compile(
    r'(?is)<(script|style|noscript|form|input|textarea|select|button|nav|footer|header|aside|iframe)\b[^>]*>.*?</\1>|'
    r'<(input|meta|link)\b[^>]*>'
)
RE_MAIN_REGION = re.compile(r'(?is)<(article|main)\b[^>]*>(.*?)</\1>')
RE_TAGS = re.compile(r'<[^>]+>')


def extract_relevant_dom_region(html_or_text: str, max_chars: int = 8000) -> str:
    """Extract only the article/main content region from HTML and strip all form/input/script blocks."""
    if not html_or_text:
        return ''
    cleaned = RE_DROP_BLOCKS.sub(' ', html_or_text)
    m = RE_MAIN_REGION.search(cleaned)
    region = m.group(2) if m else cleaned
    text = RE_TAGS.sub(' ', region)
    text = re.sub(r'\s+', ' ', text).strip()
    return text[:max_chars]


def sanitize_before_store(text: str, meta: dict | None = None) -> tuple[str, dict]:
    """Sanitize extracted text and metadata before saving to disk or passing to an extractor.
    Returns (sanitized_text, audit_flags)."""
    if not text:
        return '', {'pii_redacted': False, 'prompt_injection_detected': False, 'redaction_types': []}

    redaction_types = []
    out = text

    if RE_RRN.search(out):
        out = RE_RRN.sub('[REDACTED_RRN]', out)
        redaction_types.append('rrn')

    if RE_SECRET_TOKEN.search(out):
        out = RE_SECRET_TOKEN.sub('[REDACTED_CREDENTIAL]', out)
        redaction_types.append('credential_or_cookie')

    if RE_PHONE.search(out):
        out = RE_PHONE.sub('[REDACTED_PHONE]', out)
        redaction_types.append('phone')

    if RE_CARD_OR_ACCT.search(out):
        out = RE_CARD_OR_ACCT.sub('[REDACTED_FINANCIAL]', out)
        redaction_types.append('financial')

    if RE_EMAIL.search(out):
        out = RE_EMAIL.sub('[REDACTED_EMAIL]', out)
        redaction_types.append('email')

    if RE_RESIDENTIAL_ADDR.search(out):
        out = RE_RESIDENTIAL_ADDR.sub('[REDACTED_ADDRESS]', out)
        redaction_types.append('residential_address')

    if RE_PRIVATE_CONTEXT.search(out):
        redaction_types.append('private_context_marker')
        # Remove sentences containing sensitive private context markers
        sentences = re.split(r'(?<=[.!?。\n])\s+', out)
        out = ' '.join(s for s in sentences if not RE_PRIVATE_CONTEXT.search(s))

    injection_detected = False
    if RE_PROMPT_INJECTION.search(out):
        injection_detected = True
        out = RE_PROMPT_INJECTION.sub('[DATA_LITERAL_NOT_INSTRUCTION]', out)

    out = re.sub(r'\s+', ' ', out).strip()
    flags = {
        'pii_redacted': bool(redaction_types),
        'prompt_injection_detected': injection_detected,
        'redaction_types': sorted(set(redaction_types)),
    }
    return out, flags
