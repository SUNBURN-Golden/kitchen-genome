"""Bounded gap planning and difficult extraction. The model controls no persistence fields."""
import json
import os
import re
import sys
import unicodedata

from kg_ai.budget import Budget
from kg_ai.deepseek import Client, DeepSeekError, TaskReject, reservation_tokens
from kg_daemon.db import ROOT, stable_id
from kg_daemon.runner import CostCapExceeded

_CRAWLER = os.path.join(ROOT, 'crawler')
if _CRAWLER not in sys.path:
    sys.path.insert(0, _CRAWLER)
from privacy_filter import sanitize_before_store  # noqa: E402

from . import gaps

MIN_MODEL_CHARS = 400
LABELS = {'career', 'employment_claim', 'opening', 'closure', 'relocation', 'rename', 'menu', 'collab', 'mentorship_claim'}
FIELDS = {'label', 'chef_text', 'restaurant_text', 'org_text', 'role_text', 'date_text', 'quote', 'span_start', 'span_end', 'ambiguity'}
NULLABLE = {'chef_text', 'restaurant_text', 'org_text', 'role_text', 'date_text', 'ambiguity'}
EXTRACT_INSTRUCTION = (
    'Extract explicitly supported culinary facts. Return one JSON object {"findings":[...]} with at most 8 findings. '
    'Return {"findings":[]} when nothing is explicitly supported. '
    'Each finding may contain only label, chef_text, restaurant_text, org_text, role_text, date_text, quote, span_start, span_end, ambiguity. '
    'label is exactly one of career, employment_claim, opening, closure, relocation, rename, menu, collab, mentorship_claim. '
    'quote is copied exactly from the supplied text, 1 to 200 characters, including the same spaces. Do not paraphrase, translate, or repair the quote. '
    'span_start and span_end are optional half-open Unicode offsets where the supplied text slice equals quote. Omit both if unsure. '
    'A quote that occurs more than once must include those exact offsets. '
    'chef_text, restaurant_text, org_text, and role_text must themselves occur in the supplied text. Omit a name that does not. Do not copy a name from known_targets unless that exact string is in the text. '
    'date_text must already appear in the text as ISO YYYY, YYYY-MM, or YYYY-MM-DD and must be copied exactly. Do not convert Korean or other dates. Omit date_text otherwise. '
    'Omit unknown fields. Extra keys are ignored. Shared employment is not mentorship. '
    'A mentorship_claim quote must contain 사사, 스승, mentor, or 제자. No inferred employer or fetch date.'
)


def _as_int(value):
    if type(value) is bool or value is None:
        raise ValueError()
    if type(value) is int:
        return value
    if type(value) is float and value.is_integer():
        return int(value)
    if isinstance(value, str) and re.fullmatch(r'-?\d+', value.strip()):
        return int(value.strip())
    raise ValueError()


def _label_token(value):
    if isinstance(value, str) and re.fullmatch(r'[a-z0-9_]{1,40}', value):
        return 'label=' + value
    return 'label_len=' + str(len(value) if isinstance(value, str) else 0)


def normalize_finding(raw, text):
    """Keep a finding only when its quote is an exact source substring.

    A unique exact quote may have its offsets filled in. A quote is never
    rewritten, trimmed, or searched with folded whitespace. Omitted nulls and
    extra keys are structural. Any evidence miss rejects the finding.
    """
    if not isinstance(raw, dict):
        raise TaskReject('shape_invalid', 'finding=not_object')
    if raw.get('label') not in LABELS:
        raise TaskReject('label_invalid', _label_token(raw.get('label')))
    quote = raw.get('quote')
    if not isinstance(quote, str) or not quote:
        raise TaskReject('quote_missing')
    if len(quote) > 200:
        raise TaskReject('quote_too_long', 'quote_len=' + str(len(quote)))
    if quote not in text:
        raise TaskReject('quote_not_exact', 'quote_len=' + str(len(quote)) + ',in_text=0')
    starts = []
    pos = 0
    while len(starts) <= 8:
        found = text.find(quote, pos)
        if found < 0:
            break
        starts.append(found)
        pos = found + 1
    try:
        start, end = _as_int(raw.get('span_start')), _as_int(raw.get('span_end'))
    except ValueError:
        start = end = None
    if start is not None and 0 <= start < end <= len(text) and text[start:end] == quote:
        span = (start, end)
    elif len(starts) == 1:
        span = (starts[0], starts[0] + len(quote))
    elif len(starts) > 1:
        # The identical quote repeats (titles, captions, syndicated leads). Keep the first occurrence instead of
        # aborting the document; the review stage sees every occurrence and rejects if they support different claims.
        span = (starts[0], starts[0] + len(quote))
    else:
        raise TaskReject('offset_invalid')
    out = {key: None for key in NULLABLE}
    out.update({'label': raw['label'], 'quote': quote, 'span_start': span[0], 'span_end': span[1]})
    for key in NULLABLE:
        if key not in raw or raw[key] is None:
            continue
        if not isinstance(raw[key], str) or len(raw[key]) > 120:
            kind = 'name_invalid' if key in ('chef_text', 'restaurant_text', 'org_text', 'role_text') else 'field_invalid'
            raise TaskReject(kind, 'field=' + key)
        out[key] = raw[key]
    if len(starts) > 1 and out['ambiguity'] is None:
        out['ambiguity'] = 'quote_repeats=' + str(min(len(starts), 9))
    for key in ('chef_text', 'restaurant_text', 'org_text', 'role_text'):
        if out[key] is not None and out[key].casefold() not in text.casefold():
            raise TaskReject('name_absent', 'field=' + key)
    if out['date_text'] is not None:
        if not re.fullmatch(r'\d{4}(?:-\d{2}(?:-\d{2})?)?', out['date_text']):
            raise TaskReject('date_not_iso')
        if out['date_text'] not in text:
            raise TaskReject('date_not_verbatim')
    if out['label'] == 'mentorship_claim' and not re.search(r'사사|스승|mentor|제자', quote, re.I):
        raise TaskReject('mentorship_unsupported')
    if set(out) != FIELDS:
        raise TaskReject('field_invalid', 'fields=mismatch')
    return out


def validate_extraction(obj, text):
    """Accept every finding or reject the whole object. The detail lists every miss."""
    if not isinstance(obj, dict) or not isinstance(obj.get('findings'), list):
        raise TaskReject('shape_invalid', 'findings=missing')
    findings = obj['findings']
    if len(findings) > 8:
        raise TaskReject('too_many_findings', 'n=' + str(len(findings)))
    rows, problems = [], []
    for index, raw in enumerate(findings):
        try:
            rows.append(normalize_finding(raw, text))
        except TaskReject as exc:
            item = str(index) + ':' + exc.code
            if exc.detail:
                item += ',' + exc.detail
            problems.append(item)
    if problems:
        primary = problems[0].split(':', 1)[1].split(',', 1)[0]
        raise TaskReject(primary, ' '.join(problems))
    return rows


def text_that_fits(client, instruction, data, max_tokens, text):
    """Longest prefix whose byte reservation fits the remaining run and day caps.

    The full text is returned unchanged when it already fits, so a cached request
    key stays stable. None means even a short excerpt cannot be reserved.
    """
    def bound(sample):
        payload = client._payload(instruction, {**data, 'text': sample}, max_tokens, None)[1]
        return reservation_tokens(payload, max_tokens)

    limit = client.budget.room_tokens()
    if bound(text) <= limit:
        return text
    if len(text) < MIN_MODEL_CHARS or bound(text[:MIN_MODEL_CHARS]) > limit:
        return None
    lo, hi, best = MIN_MODEL_CHARS, len(text), MIN_MODEL_CHARS
    while lo <= hi:
        mid = (lo + hi) // 2
        if bound(text[:mid]) <= limit:
            best = mid
            lo = mid + 1
        else:
            hi = mid - 1
    return text[:best]


class Adapters:
    def __init__(self, client):
        self.client = client
        self.defer_extract = False
        # Observable extraction funnel: a production no-op must not hide behind ai=enabled.
        self.stats = {'considered': 0, 'skipped_filter': 0, 'skipped_no_targets': 0,
                      'skipped_not_needed': 0, 'skipped_budget': 0, 'called': 0, 'failed': 0, 'findings': 0}

    def _budget_stop(self, exc=None):
        """A reservation that does not fit stops later model calls and lets deterministic stages finish.

        Usage past the byte ceiling still aborts the run: that call was already billed.
        """
        if exc is not None and self.client.stopped == 'usage_over_reservation':
            raise exc
        self.client.stopped = 'cost_cap'
        return []

    @classmethod
    def for_meter(cls, meter):
        return cls(Client(Budget(meter, daily_cap=os.environ.get('KG_AI_DAILY_COST_CAP', '20'))))

    def plan(self, conn):
        jobs = list({j['gap_id']: j for j in gaps.gap_jobs(conn, limit=20)}.values())
        allowed = {j['gap_id']: j for j in jobs}
        if not jobs or self.client.status != 'enabled':
            return []
        data = []
        for j in jobs:
            target = conn.execute('SELECT name_ko,name_en FROM target WHERE target_id=?', (j['target_id'],)).fetchone()
            # Only non-yielding attempts are historical failures; successful gap queries are not failed input.
            history = [r[0] for r in conn.execute(
                "SELECT query FROM crawl_job WHERE gap_id=? AND query IS NOT NULL AND NOT (state='done' AND last_error IS NULL) ORDER BY rowid DESC LIMIT 3",
                (j['gap_id'],))]
            data.append({'gap_id': j['gap_id'], 'target_id': j['target_id'], 'name': dict(target), 'field': j['field'],
                         'attempts': j['attempts'], 'failed_queries': history, 'allowed_sources': ['official', 'press', 'specialist_blog']})

        def validate(obj):
            if not isinstance(obj, dict) or not isinstance(obj.get('queries'), list) or len(obj['queries']) > 60:
                raise ValueError()
            out, counts, seen = [], {}, set()
            for row in obj['queries']:
                if not isinstance(row, dict) or any(k not in row for k in ('gap_id', 'target_id', 'query', 'reason')):
                    raise ValueError()
                g = allowed[row['gap_id']]
                q = row['query']
                if row['target_id'] != g['target_id'] or not isinstance(q, str) or not 2 <= len(q) <= 200 or re.search(r'https?://|[\x00-\x1f]', q):
                    raise ValueError()
                if not isinstance(row['reason'], str) or len(row['reason']) > 200:
                    raise ValueError()
                counts[row['gap_id']] = counts.get(row['gap_id'], 0) + 1
                if counts[row['gap_id']] > 3:
                    raise ValueError()
                if (row['gap_id'], q) not in seen:
                    out.append({'path': 'reverse_gap', 'pool': g['pool'], 'target_id': g['target_id'], 'gap_id': g['gap_id'], 'query': q, 'relevance': .7})
                    seen.add((row['gap_id'], q))
            return out
        try:
            return self.client.call('gap-plan-1', 'Return JSON {"queries":[{"gap_id":"known ID","target_id":"known ID","query":"search terms","reason":"short reason"}]}. At most 3 per gap. Seek independent permitted sources; never bypass blocked access.', data, validate, max_tokens=4096)
        except DeepSeekError:
            return []
        except CostCapExceeded as exc:
            return self._budget_stop(exc)

    def extract(self, doc, recs, gaz, context=None):
        self.defer_extract = False
        if self.client.stopped not in (None, 'cost_cap') or not self.client.key:
            return []
        self.stats['considered'] += 1
        # Send the same filtered text we validate offsets against. Every real page contains
        # newlines/tabs that sanitize_before_store collapses, so comparing the sanitized text to
        # the raw text dropped nearly all production documents before the model was called.
        text, _ = sanitize_before_store(doc.text[:12000])
        text = unicodedata.normalize('NFC', text or '')
        if not text:
            self.stats['skipped_filter'] += 1
            return []
        mentions = gaz.find(text)
        options = {m['target_id'] for m in mentions if m.get('target_id')}
        if context:
            options.update(v['target_id'] for v in context.values() if isinstance(v, dict) and v.get('target_id'))
        if not options:
            self.stats['skipped_no_targets'] += 1
            return []
        if not recs and not gaz.conn.execute("SELECT 1 FROM gap WHERE state='open' AND target_id IN (%s) LIMIT 1" % ','.join('?' * len(options)), tuple(options)).fetchone():
            self.stats['skipped_not_needed'] += 1
            return []
        from .extract import needs_model
        if recs and not any(needs_model(r) for r in recs):
            self.stats['skipped_not_needed'] += 1
            return []
        targets = list({m['target_id']: {k: m.get(k) for k in ('target_id', 'kind', 'name_ko', 'name_en')} for m in mentions if m.get('target_id')}.values())
        body = {'url': doc.url, 'published_at': doc.published_at, 'known_targets': targets,
                'rule_ambiguities': [r.get('ambiguity') for r in recs]}
        fitted = text_that_fits(self.client, EXTRACT_INSTRUCTION, body, 4096, text)
        if fitted is None or self.client.stopped == 'cost_cap':
            self.defer_extract = True
            self.stats['skipped_budget'] += 1
            self.client.stopped = 'cost_cap'
            return []
        text = fitted

        def validate(obj):
            return validate_extraction(obj, text)
        self.stats['called'] += 1
        try:
            rows = self.client.call('web-extract-1', EXTRACT_INSTRUCTION, {**body, 'text': text}, validate, max_tokens=4096)
        except DeepSeekError:
            self.stats['failed'] += 1
            return []
        except CostCapExceeded as exc:
            self.defer_extract = True
            self.stats['skipped_budget'] += 1
            return self._budget_stop(exc)
        result = []
        for row in rows:
            r = {**row, 'epistemic': 'inference', 'asserted_by': 'ai_inference', 'pending_only': True, 'extractor': 'deepseek:web',
                 'extractor_version': 'web-extract-1', 'published_at': doc.published_at,
                 'model_meta_json': json.dumps(self.client.last_meta, sort_keys=True),
                 'extraction_id': stable_id('ext', doc.content_hash or doc.url, self.client.last_meta['request_id'], json.dumps(row, sort_keys=True))}
            # Exact known matches only. Never create targets from inference.
            for field, kind in (('chef', 'chef'), ('restaurant', 'restaurant'), ('org', 'restaurant')):
                matches = [m for m in gaz.lookup(r.get(field + '_text') or '') if m.get('kind') == kind]
                r['resolved_' + field + '_target'] = matches[0]['target_id'] if len(matches) == 1 else None
            result.append(r)
        self.stats['findings'] += len(result)
        return result
