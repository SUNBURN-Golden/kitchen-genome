"""Bounded gap planning and difficult extraction. The model controls no persistence fields."""
import json
import os
import re
import sys
import unicodedata
from types import SimpleNamespace

from kg_ai.budget import Budget
from kg_ai.deepseek import Client, DeepSeekError, TaskReject, reservation_tokens
from kg_daemon.db import ROOT, stable_id
from kg_daemon.runner import CostCapExceeded

_CRAWLER = os.path.join(ROOT, 'crawler')
if _CRAWLER not in sys.path:
    sys.path.insert(0, _CRAWLER)
from privacy_filter import sanitize_before_store  # noqa: E402

from . import gaps
from .targets import eligibility
from kg_ai.article import source, article_key
from kg_ai.balance import PAUSES

PLAN_INSTRUCTION = 'Return JSON {"queries":[{"gap_id":"known ID","target_id":"known ID","query":"search terms","reason":"short reason"}]}. At most 3 per gap. Seek independent permitted sources; never bypass blocked access.'
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


def article_data(data, text):
    article = source(data['url'], data.get('published_at'), text)
    if data.get('snapshot_key'):
        article['article_key'] = data['snapshot_key']
    return {'_article': article,
            **{k: v for k, v in data.items() if k not in ('url', 'published_at')}}


def text_that_fits(client, instruction, data, max_tokens, text, *, limit=None):
    """Longest prefix whose byte reservation fits the remaining run and day caps.

    The full text is returned unchanged when it already fits, so a cached request
    key stays stable. None means even a short excerpt cannot be reserved.
    """
    def bound(sample):
        payload = client._payload(instruction, article_data(data, sample), max_tokens, None)[1]
        return reservation_tokens(payload, max_tokens)

    limit = client.budget.room_tokens() if limit is None else limit
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
        self.article_results = {}
        # Observable extraction funnel: a production no-op must not hide behind ai=enabled.
        self.stats = {'considered': 0, 'skipped_filter': 0, 'skipped_no_targets': 0,
                      'eligible': 0, 'plan_skipped_targets': 0, 'skipped_balance': 0, 'skipped_not_needed': 0, 'skipped_budget': 0, 'called': 0, 'failed': 0, 'findings': 0}

    def _budget_stop(self, exc=None, workload='fresh'):
        """Defer rechecks independently; a shared cap pause lets rules finish.

        Usage past the byte ceiling still aborts the run: that call was already billed.
        """
        if exc is not None and self.client.stopped == 'usage_over_reservation':
            raise exc
        if workload != 'recheck':
            self.client.stopped = 'cost_cap'
        return []

    @classmethod
    def for_meter(cls, meter):
        return cls(Client(Budget(meter, daily_cap=os.environ.get('KG_AI_DAILY_COST_CAP', '20'))))

    def plan(self, conn):
        jobs = list({j['gap_id']: j for j in gaps.gap_jobs(conn, limit=20)}.values())
        valid = []
        for j in jobs:
            if conn.execute("SELECT 1 FROM target t JOIN gap g ON g.target_id=t.target_id WHERE g.gap_id=? AND g.state='open' AND t.status='active' AND t.kind IN ('chef','restaurant') AND t.origin IN ('seed','discovered')", (j['gap_id'],)).fetchone():
                valid.append(j)
            else:
                self.stats['plan_skipped_targets'] += 1
        jobs = valid
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
        while data and reservation_tokens(self.client._payload(PLAN_INSTRUCTION, data, 4096, None)[1], 4096) > self.client.budget.room_tokens():
            data = data[:len(data)//2]
        if not data:
            self.stats['skipped_budget'] += 1
            return []
        allowed = {row['gap_id']: allowed[row['gap_id']] for row in data}
        try:
            return self.client.call('gap-plan-2', PLAN_INSTRUCTION, data, validate, max_tokens=4096)
        except DeepSeekError:
            if self.client.stopped in PAUSES:
                self.stats['skipped_balance'] += 1
            return []
        except CostCapExceeded as exc:
            return self._budget_stop(exc)

    def extract(self, doc, recs, gaz, context=None, workload='fresh', attempt_id=None):
        full = unicodedata.normalize('NFC', sanitize_before_store(doc.text)[0] or '')
        if len(full) <= 12000:
            return self._extract_window(doc, recs, gaz, context, workload, attempt_id)
        # Overlap preserves boundary quotes (the evidence schema caps quotes at 200).
        # An incomplete window defers the remaining article, never claims full coverage.
        before = dict(self.stats)
        results, seen, start, deferred = [], set(), 0, False
        snapshot = article_key(getattr(doc, 'canonical_url', None) or doc.url, full)
        while start < len(full):
            end = min(start + 12000, len(full))
            fragment = full[start:end]
            offset = start + len(fragment) - len(fragment.lstrip())
            window = SimpleNamespace(**vars(doc))
            window.text = fragment.strip()
            window.snapshot_key, window.source_start = snapshot, offset
            rows = self._extract_window(window, recs, gaz, context, workload, attempt_id)
            for r in rows:
                r['span_start'] += offset
                r['span_end'] += offset
                key = tuple(r.get(k) for k in ('label','quote','chef_text','restaurant_text','org_text','role_text','date_text'))
                if key not in seen:
                    results.append(r)
                    seen.add(key)
            if self.defer_extract or self.client.stopped:
                deferred = True
                break
            if getattr(self, 'window_partial', False):
                deferred = True
                break
            if end == len(full):
                break
            start = end - 200
        self.defer_extract = deferred
        # The funnel counts documents; request/finding counts remain separate.
        for name in ('considered','eligible','skipped_filter','skipped_no_targets','skipped_not_needed','skipped_budget','skipped_balance'):
            self.stats[name] = before[name] + int(self.stats[name] > before[name])
        if self.stats['eligible'] > before['eligible']:
            self.stats['skipped_no_targets'] = before['skipped_no_targets']
        self.stats['findings'] = before['findings'] + len(results)
        return results

    def _extract_window(self, doc, recs, gaz, context=None, workload='fresh', attempt_id=None):
        self.window_partial = False
        self.defer_extract = False
        if self.client.status == 'paused' and self.client.stopped in PAUSES:
            self.defer_extract = True
            self.stats['skipped_balance'] += 1
            return []
        if (self.client.status != 'enabled' and self.client.stopped != 'cost_cap') or not self.client.key:
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
        gate = eligibility(gaz, text)
        mentions, options = gate['spans'], gate['target_ids']
        if not options:
            self.stats['skipped_no_targets'] += 1
            return []
        self.stats['eligible'] += 1
        if workload != 'recheck' and not recs and not gaz.conn.execute("SELECT 1 FROM gap WHERE state='open' AND target_id IN (%s) LIMIT 1" % ','.join('?' * len(options)), tuple(options)).fetchone():
            self.stats['skipped_not_needed'] += 1
            return []
        from .extract import needs_model
        if workload != 'recheck' and recs and not any(needs_model(r) for r in recs):
            self.stats['skipped_not_needed'] += 1
            return []
        targets = list({m['target_id']: {k: m.get(k) for k in ('target_id', 'kind', 'name_ko', 'name_en')} for m in mentions if m.get('target_id')}.values())
        body = {'url': getattr(doc, 'canonical_url', None) or doc.url, 'published_at': doc.published_at, 'known_targets': targets,
                'snapshot_key': getattr(doc, 'snapshot_key', None), 'source_start': getattr(doc, 'source_start', 0),
                'workload': workload, 'rule_ambiguities': sorted({r.get('ambiguity') or '' for r in recs})}
        max_output = 1024 if workload == 'recheck' else 4096
        if workload == 'recheck':
            body['finding_limit'] = 2
        self.client.budget.workload = workload
        cache_key = (article_key(body['url'], text), workload, body['snapshot_key'], body['source_start'])
        cached = self.article_results.get(cache_key)
        review_tokens, previous_hold = 0, None
        fit_limit = self.client.budget.room_tokens()
        if workload == 'recheck' and cached is None:
            reason = self.client.check_balance()
            if reason:
                self.defer_extract = True
                self.stats['skipped_balance'] += 1
                return []
            # Reserve for at most two recheck findings; the extraction validator enforces that ceiling.
            # Fit BOTH costs before creating a hold. Keep full review context even
            # when extraction fits only a prefix, and preserve prior window holds.
            review_tokens = len(text.encode()) + 4096 + 440 + 2400
            old = gaz.conn.execute("SELECT units FROM ai_review_hold WHERE attempt_id=? AND day=? AND state='held'",
                                  (attempt_id, self.client.budget.day())).fetchone()
            previous_hold = old['units'] if old else None
            old_tokens = (previous_hold or 0) / self.client.budget.meter.unit_costs['llm_tokens_k'] * 1000
            fit_limit -= max(0, review_tokens - old_tokens)
        fitted = cached[2] if cached else text_that_fits(self.client, EXTRACT_INSTRUCTION, body, max_output, text, limit=fit_limit)
        if fitted is None or self.client.stopped == 'cost_cap':
            self.defer_extract = True
            self.stats['skipped_budget'] += 1
            if workload != 'recheck':
                self.client.stopped = 'cost_cap'
            return []
        self.window_partial = len(fitted) < len(text)
        # Keep incomplete rechecks retryable even if fitting removed every known
        # name and the final eligibility gate returns before the model call.
        if workload == 'recheck' and self.window_partial:
            self.defer_extract = True
        text = fitted
        if not eligibility(gaz, text)['target_ids']:
            self.stats['skipped_no_targets'] += 1
            return []
        if workload == 'recheck' and cached is None:
            from .recheck import hold_review
            extract_tokens = reservation_tokens(self.client._payload(EXTRACT_INSTRUCTION, article_data(body, text), max_output, None)[1], max_output)
            if not hold_review(gaz.conn, attempt_id, self.client.budget, review_tokens, extraction_tokens=extract_tokens):
                self.defer_extract = True
                self.stats['skipped_budget'] += 1
                return []

        def validate(obj):
            rows = validate_extraction(obj, text)
            if workload == 'recheck' and len(rows) > 2:
                raise TaskReject('too_many_findings')
            return rows
        self.stats['called'] += int(cached is None)
        rows = []
        try:
            if cached is None:
                rows = self.client.call('web-extract-2', EXTRACT_INSTRUCTION, article_data(body, text), validate, max_tokens=max_output)
                self.article_results[cache_key] = (rows, dict(self.client.last_meta), text)
            else:
                rows, meta, text = cached
                self.client.last_meta = meta
        except DeepSeekError:
            if self.client.status == 'paused' and self.client.stopped in PAUSES:
                self.defer_extract = True
                self.stats['skipped_balance'] += 1
                self.stats['called'] -= 1
                return []
            # A failed extraction defers this document even when the client can
            # continue with later documents (including schema rejections).
            self.defer_extract = True
            self.stats['failed'] += 1
            return []
        except CostCapExceeded as exc:
            self.defer_extract = True
            self.stats['skipped_budget'] += 1
            return self._budget_stop(exc, workload)
        finally:
            if workload == 'recheck' and cached is None and not rows:
                from .recheck import restore_review_hold
                restore_review_hold(gaz.conn, attempt_id, previous_hold)
        result = []
        for row in rows:
            r = {**row, 'epistemic': 'inference', 'asserted_by': 'ai_inference', 'pending_only': True, 'extractor': 'deepseek:web',
                 'extractor_version': 'web-extract-2', 'published_at': doc.published_at,
                 'model_meta_json': json.dumps({**self.client.last_meta, 'workload': workload, 'partial': self.window_partial, 'source_start': body['source_start'], 'snapshot_key': body['snapshot_key']}, sort_keys=True),
                 'extraction_id': stable_id('ext', body['url'], self.client.last_meta['request_id'], json.dumps(row, sort_keys=True))}
            # Exact known matches only. Never create targets from inference.
            for field, kind in (('chef', 'chef'), ('restaurant', 'restaurant'), ('org', 'restaurant')):
                matches = [m for m in gaz.lookup(r.get(field + '_text') or '') if m.get('kind') == kind]
                r['resolved_' + field + '_target'] = matches[0]['target_id'] if len(matches) == 1 else None
            result.append(r)
        self.stats['findings'] += len(result)
        return result
