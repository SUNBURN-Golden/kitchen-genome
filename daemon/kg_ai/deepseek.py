"""OpenAI-compatible JSON client. Thinking is sent disabled.

The current DeepSeek API enables thinking when `thinking` is omitted. JSON mode
then replaces an unrecognized completion with spaces while finish_reason stays
`stop` and usage is still billed. That blank body is not task JSON. One bounded
retry is allowed for blank content and for schema rejection. A second failure
skips the task; three rejected tasks pause this run. Redirects, proxies, and raw
bodies are not kept. Extraction retains independently validated findings when
other findings fail evidence checks.
"""
import hashlib
import json
import os
import re
import time
from .balance import BalanceGate, http_balance
from .article import messages as article_messages
import urllib.error
import urllib.request
from kg_daemon.runner import NonRetryable
from kg_collect.guard import Guard

URL = 'https://api.deepseek.com/chat/completions'
QWEN_URL = 'https://token-plan.maas.qwencloudapi.com/compatible-mode/v1/chat/completions'
QWEN_MODEL = 'qwen4-max-preview'
QWEN_KEY_PATH = '/home/box/.qwen/token_plan_key'
MAX_BODY = 256 * 1024
# Non-streaming read timeouts. qwen4-max-preview needs ~41 s for a ~3k-token gap-plan
# answer, so the old shared 30 s timeout turned every gap-plan backup call into
# transport_unknown (2026-11-12, 2026-11-13).
DEEPSEEK_TIMEOUT_SEC = 30
QWEN_TIMEOUT_SEC = 180
VERSION = 'json-2'
FALLBACK_CODES = (
    'provider_failed', 'rate_limited', 'auth_failed', 'model_unavailable',
    'balance_low', 'balance_unavailable', 'transport_unknown', 'quota_exhausted',
)
# Definite HTTP rejections the provider does not bill (429 throttle/quota, 401, 404).
# A reservation left 'unknown' with one of these errors is safe to retry in a later
# run; anything else (timeouts, lost responses) stays replay-forbidden.
UNBILLED_REJECTIONS = ('rate_limited', 'quota_exhausted', 'auth_failed', 'model_unavailable')
# Chat-template overhead. Added to the UTF-8 payload length and max_tokens.
# Token count is at most one per payload byte, so this remains an upper bound.
FRAME_TOKENS = 4096
MAX_TASK_ATTEMPTS = 2
MAX_INPUT_BYTES = 64_000
MAX_OUTPUT_TOKENS = 4096
SCHEMA_REJECT_LIMIT = 3
MAX_JSON_WRAPPER = 400
SCHEMA_CODES = (
    'shape_invalid', 'too_many_findings', 'label_invalid', 'quote_missing', 'quote_too_long',
    'quote_not_exact', 'offset_ambiguous', 'offset_invalid', 'name_absent', 'name_invalid',
    'date_not_iso', 'date_not_verbatim', 'mentorship_unsupported', 'field_invalid',
)
RETRYABLE = ('json_blank', 'invalid_task_json') + SCHEMA_CODES
# These leave the client enabled: one document is skipped, other requests may continue.
NONSTOP = {'cached_result_purged', 'task_previously_rejected', 'missing_key', 'input_limit', 'concurrent_attempt', 'attempts_exhausted', 'deadline'}
# Definite primary rejections: the provider refused the request or answered invalidly.
# A stored one of these never routes to the backup on a later attempt.
NON_FALLBACK_CODES = ('missing_key',
                      'model_or_request_rejected', 'response_limit', 'invalid_response_or_usage',
                      'truncated_json', 'incomplete_output', 'schema_reject_limit') + RETRYABLE


class DeepSeekError(NonRetryable):
    pass


class TaskReject(DeepSeekError):
    """Schema or evidence rejection. `code` is safe to log; `detail` is sanitized."""

    def __init__(self, code, detail=''):
        self.code = code
        self.detail = _safe_detail(detail)
        super().__init__(code)


def _safe_detail(text):
    return ''.join(ch for ch in (text or '') if ch in 'abcdefghijklmnopqrstuvwxyz0123456789_=,:- ')[:400]


def failure_code(value):
    return (value or '').split(' ', 1)[0]


def stored_error(exc):
    code = getattr(exc, 'code', str(exc))
    detail = getattr(exc, 'detail', '')
    return code if not detail else code + ' ' + detail


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def load_qwen_key():
    try:
        return open(QWEN_KEY_PATH).read().strip()
    except OSError:
        return ''


def http_transport(payload, key, url=URL, timeout=DEEPSEEK_TIMEOUT_SEC):
    req = urllib.request.Request(url, data=payload, headers={'Authorization': 'Bearer ' + key, 'Content-Type': 'application/json'})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    try:
        with opener.open(req, timeout=timeout) as response:
            return response.status, response.read(MAX_BODY + 1)
    except urllib.error.HTTPError as exc:
        # Only a short error body is kept, for classification (never stored or logged).
        try:
            body = exc.read(4096)
        except Exception:
            body = b''
        return exc.code, body
    except Exception:
        raise DeepSeekError('transport_unknown') from None


def qwen_http_transport(payload, key):
    return http_transport(payload, key, url=QWEN_URL, timeout=QWEN_TIMEOUT_SEC)


def reservation_tokens(payload, max_tokens):
    """Upper bound on billed tokens: payload bytes + framing + maximum output."""
    return len(payload) + FRAME_TOKENS + max_tokens


def _correction_suffix(code):
    return ' Previous output was rejected (' + code + '). Return one JSON object and nothing else.'


def parse_json_content(content, list_key=None):
    """Task JSON lives in message.content. reasoning_content is never the result.

    Some models answer a {"<list_key>": [...]} task with the bare list. When the caller
    names its envelope key, a top-level JSON array is wrapped as {list_key: array} and
    still goes through the task validator. Without list_key, non-objects are rejected."""
    if not isinstance(content, str) or not content.strip():
        raise DeepSeekError('json_blank')
    text = content.strip()

    def reject(pos, msg):
        # Code points describe the boundaries without retaining response text.
        stripped = content.strip()
        return TaskReject('invalid_task_json',
                          f'len={len(content)} pos={pos} msg={msg.lower()} '
                          f'starts=u{ord(stripped[0]):04x} ends=u{ord(stripped[-1]):04x}')

    try:
        try:
            value = json.loads(text)
        except json.JSONDecodeError:
            fenced = (re.search(r'```json\b\s*(.*?)\s*```', text, re.DOTALL | re.IGNORECASE)
                      or re.search(r'```\s*(.*?)\s*```', text, re.DOTALL))
            if fenced:
                text = fenced.group(1).strip()
                if not text:
                    raise DeepSeekError('json_blank')
                value = json.loads(text)
            else:
                start = text.find('{')
                prefix = text[:start] if start >= 0 else text
                # Never extract a nested object from an array, malformed object,
                # or JSON string. Only short prose may surround the first object.
                if start < 0 or len(prefix) > MAX_JSON_WRAPPER or any(ch in prefix for ch in '{}[]"'):
                    raise
                value, end = json.JSONDecoder().raw_decode(text, start)
                suffix = text[end:]
                if len(suffix) > MAX_JSON_WRAPPER or any(ch in suffix for ch in '{}[]'):
                    raise json.JSONDecodeError('Extra data', text, end)
        if isinstance(value, str):
            value = json.loads(value)
    except json.JSONDecodeError as exc:
        raise reject(exc.pos, exc.msg) from None
    if list_key and isinstance(value, list):
        value = {list_key: value}
    if not isinstance(value, dict):
        raise reject(0, 'expected_object')
    return value


class Client:
    def __init__(self, budget, *, key=None, model=None, transport=http_transport, balance_transport=None, clock=time.monotonic,
                 fallback_key=None, fallback_model=None, fallback_transport=None, deadline=None):
        self.deadline = deadline
        self.key = os.environ.get('DEEPSEEK_API_KEY', '') if key is None else key
        self.model = model or os.environ.get('DEEPSEEK_MODEL', 'deepseek-chat')
        self.budget, self.transport, self.stopped = budget, transport, None
        self._schema_rejections = 0
        self.primary_paused = None
        self._previous_fallback_worthy = False
        self.last_meta = {}
        self.balance = BalanceGate(balance_transport if balance_transport is not None else (http_balance if transport is http_transport else None), clock)
        self._task = None
        self._fatal_stops = {}
        # Custom transports (tests) stay primary-only unless a fallback is injected.
        auto = fallback_transport is None and transport is http_transport
        self.fallback_model = fallback_model or os.environ.get('KG_QWEN_MODEL') or QWEN_MODEL
        self.fallback_key = load_qwen_key() if fallback_key is None and auto else (fallback_key or '')
        self.fallback_transport = (qwen_http_transport if auto and self.fallback_key else fallback_transport if self.fallback_key else None)

    @property
    def status(self):
        paused = self.stopped and not (self.fallback_available and self._fallback_stop)
        return 'disabled' if not self.configured else ('paused' if paused else 'enabled')

    @property
    def configured(self):
        return bool(self.key or self.fallback_available)

    @property
    def fallback_available(self):
        return bool(self.fallback_key and self.fallback_transport)

    @property
    def _fallback_stop(self):
        return self.stopped in FALLBACK_CODES or (
            self.stopped == 'previous_attempt_unknown_or_failed' and self._previous_fallback_worthy)

    def check_balance(self):
        """Return a run-blocking balance reason, allowing a configured backup."""
        if (self.primary_paused or not self.key) and self.fallback_available:
            return None
        reason = self.balance.check(self.key)
        if reason in FALLBACK_CODES and self.fallback_available:
            self.primary_paused = self.primary_paused or reason
            return None
        if reason:
            self.stopped = self.stopped or reason
        return reason

    def _messages(self, instruction, data, correction):
        return article_messages(instruction, data, _correction_suffix(correction) if correction else '')

    def _payload(self, instruction, data, max_tokens, correction, *, provider='deepseek'):
        messages = self._messages(instruction, data, correction)
        model = self.fallback_model if provider == 'qwen' else self.model
        # thinking disabled: omission enables thinking, which bills non-JSON completions.
        body = {'model': model, 'messages': messages, 'response_format': {'type': 'json_object'},
                'temperature': 0, 'stream': False, 'max_tokens': max_tokens}
        if provider == 'deepseek':
            body['thinking'] = {'type': 'disabled'}
        else:
            # qwen4-max-preview is a hybrid model; thinking is on by default and
            # consumes max_tokens, which showed up as truncated_json on even a 1-finding review.
            body['enable_thinking'] = False
        return messages, json.dumps(body, ensure_ascii=False).encode()

    def _request_key(self, task, payload):
        return hashlib.sha256(task.encode() + VERSION.encode() + payload).hexdigest()

    def _meta(self, task, payload, user_content, *, provider='deepseek'):
        model = self.fallback_model if provider == 'qwen' else self.model
        self.last_meta = {'request_id': self._request_key(task, payload), 'model': model, 'prompt_version': task,
                          'schema_version': VERSION, 'source_hash': hashlib.sha256(user_content.encode()).hexdigest(),
                          'provider': provider}

    def _cached_result(self, task, instruction, data, max_tokens, correction, *, provider='deepseek'):
        messages, payload = self._payload(instruction, data, max_tokens, correction, provider=provider)
        row = self.budget.lookup(self._request_key(task, payload))
        if row and row['state'] == 'done' and row['result_json'] is not None:
            self._meta(task, payload, json.dumps(messages, ensure_ascii=False, sort_keys=True), provider=provider)
            self.budget.cache_hit(row['request_key'])
            return json.loads(row['result_json'])
        return None

    def _should_fallback(self, code, task, instruction, data, max_tokens):
        if not (self.fallback_key and self.fallback_transport):
            return False
        if code in FALLBACK_CODES:
            return True
        # A prior primary attempt may have reserved and failed. Replay is forbidden, but
        # a configured backup may still answer — unless the stored error is a definite
        # non-fallback rejection. A missing or unclassifiable error still tries the backup.
        if code == 'previous_attempt_unknown_or_failed':
            row = self.budget.lookup(self._request_key(task, self._payload(instruction, data, max_tokens, None)[1]))
            return failure_code(row['error'] if row else '') not in NON_FALLBACK_CODES
        return False

    def use_task(self, task):
        if self._task != task:
            if self._task is not None:
                self._fatal_stops[self._task] = self.stopped
                self.stopped = self._fatal_stops.get(task)
                self._schema_rejections = 0
            self._task = task

    def call(self, task, instruction, data, validate, *, max_tokens=2048, list_key=None):
        if type(max_tokens) is not int or not 0 < max_tokens <= MAX_OUTPUT_TOKENS:
            raise DeepSeekError('input_limit')
        self.use_task(task)
        provider = 'qwen' if (self.primary_paused or not self.key) and self.fallback_available else 'deepseek'
        if self.stopped:
            if not (self.fallback_available and self._fallback_stop):
                return self._call(task, instruction, data, validate, max_tokens=max_tokens, provider=provider, list_key=list_key)
            self.primary_paused = self.primary_paused or self.stopped
            self.stopped = None
            provider = 'qwen'
        if provider == 'qwen':
            return self._call(task, instruction, data, validate, max_tokens=max_tokens, provider=provider, list_key=list_key)
        try:
            return self._call(task, instruction, data, validate, max_tokens=max_tokens, provider='deepseek', list_key=list_key)
        except DeepSeekError as exc:
            code = failure_code(stored_error(exc))
            if self._should_fallback(code, task, instruction, data, max_tokens):
                self.primary_paused = self.primary_paused or code
                self.stopped = None
                return self._call(task, instruction, data, validate, max_tokens=max_tokens, provider='qwen', list_key=list_key)
            raise

    def _call(self, task, instruction, data, validate, *, max_tokens, provider, list_key=None):
        if provider == 'deepseek' and not self.key:
            raise DeepSeekError('missing_key')
        if provider == 'qwen' and not (self.fallback_key and self.fallback_transport):
            raise DeepSeekError('missing_key')
        cached = self._cached_result(task, instruction, data, max_tokens, None, provider=provider)
        if cached is None:
            for code in RETRYABLE:
                cached = self._cached_result(task, instruction, data, max_tokens, code, provider=provider)
                if cached is not None:
                    break
        if cached is not None:
            return cached
        if self.stopped:
            raise DeepSeekError(self.stopped)
        guard = Guard(self.budget.conn, self.budget.meter.run_id, provider + ':' + task)
        if guard.reason:
            self.stopped = guard.reason
            raise DeepSeekError(guard.reason)
        canonical = self.budget.lookup(self._request_key(task, self._payload(instruction, data, max_tokens, None, provider=provider)[1]))
        if canonical and canonical['state'] == 'failed':
            # A billed failure stays charged and is not replayed. Other documents may continue.
            # truncated_json / incomplete_output used to take the unknown-attempt path and halt the run.
            raise DeepSeekError('task_previously_rejected')
        correction = None
        for attempt in range(MAX_TASK_ATTEMPTS):
            try:
                return self._once(task, instruction, data, validate, max_tokens=max_tokens, correction=correction, provider=provider, list_key=list_key)
            except DeepSeekError as exc:
                code = failure_code(stored_error(exc))
                if code in RETRYABLE and attempt + 1 < MAX_TASK_ATTEMPTS:
                    self.stopped = None
                    correction = code
                    continue
                if code not in NONSTOP and code != 'task_previously_rejected':
                    guard.failure(code)
                    self.budget.conn.commit()
                if code in RETRYABLE:
                    # _once already settled the billed failure. Skip this task,
                    # but stop a broken prompt from rejecting the whole queue.
                    self._reject_task()
                elif code not in NONSTOP:
                    self.stopped = self.stopped or code
                raise

    def _reject_task(self):
        self._schema_rejections += 1
        self.stopped = 'schema_reject_limit' if self._schema_rejections >= SCHEMA_REJECT_LIMIT else None

    def _once(self, task, instruction, data, validate, *, max_tokens, correction, provider='deepseek', list_key=None):
        messages, payload = self._payload(instruction, data, max_tokens, correction, provider=provider)
        if len(payload) > MAX_INPUT_BYTES:
            raise DeepSeekError('input_limit')
        key = self._request_key(task, payload)
        self._meta(task, payload, json.dumps(messages, ensure_ascii=False, sort_keys=True), provider=provider)
        old = self.budget.lookup(key)
        if old:
            if old['state'] == 'done' and old['result_json'] is not None:
                self.budget.cache_hit(key)
                return json.loads(old['result_json'])
            if old['state'] == 'expired' or (old['state'] == 'done' and old['result_json'] is None):
                # Retention may purge cached IG results after 30 days. That must not disable the
                # model for every other request in this run; this one request simply has no cache.
                raise DeepSeekError('cached_result_purged')
            if old['state'] == 'failed':
                raise DeepSeekError('task_previously_rejected')
            if (old['state'] == 'unknown' and failure_code(old['error']) in UNBILLED_REJECTIONS
                    and old['run_id'] != self.budget.meter.run_id):
                # An earlier run got a definite, unbilled HTTP rejection; release it and retry.
                # Within the same run the rejection still stands (no hammering a throttled API).
                self.budget.release(key)
                old = None
        if old:
            self._previous_fallback_worthy = failure_code(old['error']) not in NON_FALLBACK_CODES
            self.stopped = 'previous_attempt_unknown_or_failed'
            raise DeepSeekError(self.stopped)
        if self.deadline is not None and time.monotonic() >= self.deadline:
            raise DeepSeekError('deadline')
        if provider == 'deepseek':
            reason = self.balance.check(self.key)
            if reason:
                self.stopped = reason
                raise DeepSeekError(reason)
        remaining = self.deadline - time.monotonic() if self.deadline is not None else None
        if remaining is not None and remaining <= 0:
            raise DeepSeekError('deadline')
        api_key = self.fallback_key if provider == 'qwen' else self.key
        transport = self.fallback_transport if provider == 'qwen' else self.transport
        model = self.fallback_model if provider == 'qwen' else self.model
        bound = reservation_tokens(payload, max_tokens)
        article = data.get('_article') if isinstance(data, dict) else None
        if article and article.get('article_key'):
            item = self._request_key(task, f"{article['article_key']}:{data.get('source_start', 0)}".encode())
        else:
            item = self._request_key(task, self._payload(instruction, data, max_tokens, None)[1])
        registered = self.budget.reserve(key, task, model, bound, provider=provider, item=item)
        if registered is None:
            from kg_daemon.claims import queue_review
            queue_review(self.budget.conn, 'guard_attempts_exhausted', 'ai_request', item,
                         {'task': task}, dedupe_key='guard-ai:' + item)
            self.budget.conn.commit()
            raise DeepSeekError('attempts_exhausted')
        if not registered:
            raise DeepSeekError('concurrent_attempt')
        guard = Guard(self.budget.conn, self.budget.meter.run_id, provider + ':' + task)
        usage = None
        try:
            if remaining is not None and transport is http_transport:
                status, raw = transport(payload, api_key, timeout=min(DEEPSEEK_TIMEOUT_SEC, remaining))
            elif remaining is not None and transport is qwen_http_transport:
                status, raw = http_transport(payload, api_key, url=QWEN_URL, timeout=min(QWEN_TIMEOUT_SEC, remaining))
            else:
                status, raw = transport(payload, api_key)
            if status != 200:
                if status == 429 and b'insufficient_quota' in (raw or b''):
                    raise DeepSeekError('quota_exhausted')
                raise DeepSeekError({400: 'model_or_request_rejected', 401: 'auth_failed', 404: 'model_unavailable', 429: 'rate_limited'}.get(status, 'provider_failed'))
            if len(raw) > MAX_BODY:
                raise DeepSeekError('response_limit')
            response = json.loads(raw)
            usage = response['usage']
            for field in ('prompt_tokens', 'completion_tokens', 'total_tokens'):
                if type(usage[field]) is not int or usage[field] < 0:
                    raise ValueError()
            if usage['total_tokens'] != usage['prompt_tokens'] + usage['completion_tokens']:
                raise ValueError()
            cache = usage.get('prompt_cache_hit_tokens', (usage.get('prompt_tokens_details') or {}).get('cached_tokens', 0))
            if type(cache) is not int or not 0 <= cache <= usage['prompt_tokens']:
                raise ValueError()
            usage['prompt_cache_hit_tokens'] = cache
        except Exception as exc:
            code = str(exc) if isinstance(exc, DeepSeekError) else 'invalid_response_or_usage'
            self.stopped = code
            self.budget.fail(key, code)
            raise DeepSeekError(code) from None
        try:
            choice = response['choices'][0]
            reason = choice.get('finish_reason')
            if reason == 'length':
                raise DeepSeekError('truncated_json')
            if reason != 'stop':
                raise DeepSeekError('incomplete_output')
            # reasoning_content is chain-of-thought, not evidence and not the task object.
            result = validate(parse_json_content((choice.get('message') or {}).get('content'), list_key=list_key))
        except DeepSeekError as exc:
            self.stopped = failure_code(stored_error(exc))
            self.budget.settle(key, usage, error=stored_error(exc))
            raise
        except Exception:
            self.stopped = 'invalid_task_json'
            self.budget.settle(key, usage, error=self.stopped)
            raise DeepSeekError(self.stopped) from None
        guard.progress(key)
        Guard(self.budget.conn, self.budget.meter.run_id, 'model:' + task).complete(item)
        self.budget.settle(key, usage, result=result)
        return result
