"""OpenAI-compatible JSON client. Thinking is sent disabled.

The current DeepSeek API enables thinking when `thinking` is omitted. JSON mode
then replaces an unrecognized completion with spaces while finish_reason stays
`stop` and usage is still billed. That blank body is not task JSON. One bounded
retry is allowed for blank content and for schema rejection. A second failure
pauses this run. Redirects, proxies, raw bodies, and partial results are not kept.
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
from kg_daemon.runner import NonRetryable, CostCapExceeded

URL = 'https://api.deepseek.com/chat/completions'
QWEN_URL = 'https://token-plan.maas.qwencloudapi.com/compatible-mode/v1/chat/completions'
QWEN_MODEL = 'qwen4-max-preview'
QWEN_KEY_PATH = '/home/box/.qwen/token_plan_key'
MAX_BODY = 256 * 1024
VERSION = 'json-2'
FALLBACK_CODES = (
    'provider_failed', 'rate_limited', 'auth_failed', 'model_unavailable',
    'balance_low', 'balance_unavailable', 'transport_unknown',
)
# Chat-template overhead. Added to the UTF-8 payload length and max_tokens.
# Token count is at most one per payload byte, so this remains an upper bound.
FRAME_TOKENS = 4096
MAX_TASK_ATTEMPTS = 2
SCHEMA_CODES = (
    'shape_invalid', 'too_many_findings', 'label_invalid', 'quote_missing', 'quote_too_long',
    'quote_not_exact', 'offset_ambiguous', 'offset_invalid', 'name_absent', 'name_invalid',
    'date_not_iso', 'date_not_verbatim', 'mentorship_unsupported', 'field_invalid',
)
RETRYABLE = ('json_blank', 'invalid_task_json') + SCHEMA_CODES
# These leave the client enabled: one document is skipped, other requests may continue.
NONSTOP = {'cached_result_purged', 'task_previously_rejected', 'missing_key', 'input_limit', 'concurrent_attempt'}
# Definite primary rejections: the provider refused the request or answered invalidly.
# A stored one of these never routes to the backup on a later attempt.
NON_FALLBACK_CODES = ('model_or_request_rejected', 'response_limit', 'invalid_response_or_usage',
                      'truncated_json', 'incomplete_output') + RETRYABLE


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


def http_transport(payload, key, url=URL):
    req = urllib.request.Request(url, data=payload, headers={'Authorization': 'Bearer ' + key, 'Content-Type': 'application/json'})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    try:
        with opener.open(req, timeout=30) as response:
            return response.status, response.read(MAX_BODY + 1)
    except urllib.error.HTTPError as exc:
        return exc.code, b''
    except Exception:
        raise DeepSeekError('transport_unknown') from None


def qwen_http_transport(payload, key):
    return http_transport(payload, key, url=QWEN_URL)


def reservation_tokens(payload, max_tokens):
    """Upper bound on billed tokens: payload bytes + framing + maximum output."""
    return len(payload) + FRAME_TOKENS + max_tokens


def _correction_suffix(code):
    return ' Previous output was rejected (' + code + '). Return one JSON object and nothing else.'


def parse_json_content(content):
    """Task JSON lives in message.content. reasoning_content is never the result."""
    if not isinstance(content, str) or not content.strip():
        raise DeepSeekError('json_blank')
    text = content.strip()
    fenced = re.fullmatch(r'```(?:json)?\s*(.*?)\s*```', text, re.DOTALL | re.IGNORECASE)
    if fenced:
        text = fenced.group(1).strip()
    if not text:
        raise DeepSeekError('json_blank')
    try:
        value = json.loads(text)
        if isinstance(value, str):
            value = json.loads(value)
    except (json.JSONDecodeError, TypeError):
        raise DeepSeekError('invalid_task_json') from None
    if not isinstance(value, dict):
        raise DeepSeekError('invalid_task_json')
    return value


class Client:
    def __init__(self, budget, *, key=None, model=None, transport=http_transport, balance_transport=None, clock=time.monotonic,
                 fallback_key=None, fallback_model=None, fallback_transport=None):
        self.key = os.environ.get('DEEPSEEK_API_KEY', '') if key is None else key
        self.model = model or os.environ.get('DEEPSEEK_MODEL', 'deepseek-chat')
        self.budget, self.transport, self.stopped = budget, transport, None
        self.last_meta = {}
        self.balance = BalanceGate(balance_transport if balance_transport is not None else (http_balance if transport is http_transport else None), clock)
        # Custom transports (tests) stay primary-only unless a fallback is injected.
        auto = fallback_transport is None and transport is http_transport
        self.fallback_model = fallback_model or QWEN_MODEL
        self.fallback_key = load_qwen_key() if fallback_key is None and auto else (fallback_key or '')
        self.fallback_transport = (qwen_http_transport if auto and self.fallback_key else fallback_transport if self.fallback_key else None)

    @property
    def status(self):
        return 'disabled' if not self.key else ('paused' if self.stopped else 'enabled')

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

    def call(self, task, instruction, data, validate, *, max_tokens=2048):
        try:
            return self._call(task, instruction, data, validate, max_tokens=max_tokens, provider='deepseek')
        except DeepSeekError as exc:
            if self._should_fallback(failure_code(stored_error(exc)), task, instruction, data, max_tokens):
                self.stopped = None
                return self._call(task, instruction, data, validate, max_tokens=max_tokens, provider='qwen')
            raise

    def _call(self, task, instruction, data, validate, *, max_tokens, provider):
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
        canonical = self.budget.lookup(self._request_key(task, self._payload(instruction, data, max_tokens, None, provider=provider)[1]))
        if canonical and canonical['state'] == 'failed':
            # A billed failure stays charged and is not replayed. Other documents may continue.
            # truncated_json / incomplete_output used to take the unknown-attempt path and halt the run.
            raise DeepSeekError('task_previously_rejected')
        correction = None
        for attempt in range(MAX_TASK_ATTEMPTS):
            try:
                return self._once(task, instruction, data, validate, max_tokens=max_tokens, correction=correction, provider=provider)
            except CostCapExceeded:
                # A retry that cannot be reserved pauses on the original task error.
                # Usage past the reservation still stops the phase; do not hide that.
                if correction and self.stopped != 'usage_over_reservation':
                    self.stopped = correction
                    raise DeepSeekError(correction) from None
                raise
            except DeepSeekError as exc:
                code = failure_code(stored_error(exc))
                if code in RETRYABLE and attempt + 1 < MAX_TASK_ATTEMPTS:
                    self.stopped = None
                    correction = code
                    continue
                if code not in NONSTOP:
                    self.stopped = self.stopped or code
                raise

    def _once(self, task, instruction, data, validate, *, max_tokens, correction, provider='deepseek'):
        messages, payload = self._payload(instruction, data, max_tokens, correction, provider=provider)
        if len(payload) > 64_000:
            raise DeepSeekError('input_limit')
        key = self._request_key(task, payload)
        self._meta(task, payload, json.dumps(messages, ensure_ascii=False, sort_keys=True), provider=provider)
        old = self.budget.lookup(key)
        if old:
            if old['state'] == 'done' and old['result_json'] is not None:
                return json.loads(old['result_json'])
            if old['state'] == 'expired' or (old['state'] == 'done' and old['result_json'] is None):
                # Retention may purge cached IG results after 30 days. That must not disable the
                # model for every other request in this run; this one request simply has no cache.
                raise DeepSeekError('cached_result_purged')
            if old['state'] == 'failed':
                raise DeepSeekError('task_previously_rejected')
            self.stopped = 'previous_attempt_unknown_or_failed'
            raise DeepSeekError(self.stopped)
        if provider == 'deepseek':
            reason = self.balance.check(self.key)
            if reason:
                self.stopped = reason
                raise DeepSeekError(reason)
        api_key = self.fallback_key if provider == 'qwen' else self.key
        transport = self.fallback_transport if provider == 'qwen' else self.transport
        model = self.fallback_model if provider == 'qwen' else self.model
        # Byte ceiling, not a chars/4 guess: under-reserving pauses after the provider has billed.
        bound = reservation_tokens(payload, max_tokens)
        if not self.budget.reserve(key, task, model, bound):
            raise DeepSeekError('concurrent_attempt')
        usage = None
        try:
            status, raw = transport(payload, api_key)
            if status != 200:
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
            cache = usage.get('prompt_cache_hit_tokens', 0)
            if type(cache) is not int or not 0 <= cache <= usage['prompt_tokens']:
                raise ValueError()
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
            result = validate(parse_json_content((choice.get('message') or {}).get('content')))
        except DeepSeekError as exc:
            self.stopped = failure_code(stored_error(exc))
            self.budget.settle(key, usage, error=stored_error(exc))
            raise
        except Exception:
            self.stopped = 'invalid_task_json'
            self.budget.settle(key, usage, error=self.stopped)
            raise DeepSeekError(self.stopped) from None
        try:
            self.budget.settle(key, usage, result=result)
        except CostCapExceeded:
            self.stopped = 'usage_over_reservation'
            raise
        return result
