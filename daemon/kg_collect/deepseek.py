"""Bounded gap planning and difficult extraction. The model controls no persistence fields."""
import json
import os
import re
import sys
import unicodedata

from kg_ai.budget import Budget
from kg_ai.deepseek import Client, DeepSeekError
from kg_daemon.db import ROOT, stable_id

_CRAWLER = os.path.join(ROOT, 'crawler')
if _CRAWLER not in sys.path:
    sys.path.insert(0, _CRAWLER)
from privacy_filter import sanitize_before_store  # noqa: E402

from . import gaps

LABELS = {'career', 'employment_claim', 'opening', 'closure', 'relocation', 'rename', 'menu', 'collab', 'mentorship_claim'}
FIELDS = {'label', 'chef_text', 'restaurant_text', 'org_text', 'role_text', 'date_text', 'quote', 'span_start', 'span_end', 'ambiguity'}
NULLABLE = {'chef_text', 'restaurant_text', 'org_text', 'role_text', 'date_text', 'ambiguity'}
EXTRACT_INSTRUCTION = ('Extract explicitly supported culinary facts; return {"findings":[]} if unsupported. Each finding uses: '
                       'label (career|employment_claim|opening|closure|relocation|rename|menu|collab|mentorship_claim), '
                       'chef_text, restaurant_text, org_text, role_text, date_text (nullable ISO date verbatim in text), '
                       'quote (exact source substring <=200 characters), span_start, span_end (Unicode offsets into the supplied text), ambiguity (nullable). '
                       'Unknown fields/dates stay null and may be omitted. Extra keys are ignored. '
                       'Shared employment never implies mentorship. No inferred employer or fetch date.')


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


def normalize_finding(raw, text):
    """Keep a finding only when its quote is an exact source substring.

    Omitted nulls, extra keys, and a wrong offset are structural. A quote that
    is not in the source, an ambiguous quote with no exact offset, or a field
    that is not in the text is rejected. The model cannot set persistence fields.
    """
    if not isinstance(raw, dict) or raw.get('label') not in LABELS:
        raise ValueError()
    quote = raw.get('quote')
    if not isinstance(quote, str) or not quote or len(quote) > 200 or quote not in text:
        raise ValueError()
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
    else:
        raise ValueError()
    out = {key: None for key in NULLABLE}
    out.update({'label': raw['label'], 'quote': quote, 'span_start': span[0], 'span_end': span[1]})
    for key in NULLABLE:
        if key not in raw or raw[key] is None:
            continue
        if not isinstance(raw[key], str) or len(raw[key]) > 120:
            raise ValueError()
        out[key] = raw[key]
    for key in ('chef_text', 'restaurant_text', 'org_text', 'role_text'):
        if out[key] is not None and out[key].casefold() not in text.casefold():
            raise ValueError()
    if out['date_text'] is not None and (not re.fullmatch(r'\d{4}(?:-\d{2}(?:-\d{2})?)?', out['date_text']) or out['date_text'] not in text):
        raise ValueError()
    if out['label'] == 'mentorship_claim' and not re.search(r'사사|스승|mentor|제자', quote, re.I):
        raise ValueError()
    if set(out) != FIELDS:
        raise ValueError()
    return out


class Adapters:
    def __init__(self, client):
        self.client = client
        # Observable extraction funnel: a production no-op must not hide behind ai=enabled.
        self.stats = {'considered': 0, 'skipped_filter': 0, 'skipped_no_targets': 0,
                      'skipped_not_needed': 0, 'called': 0, 'failed': 0, 'findings': 0}

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

    def extract(self, doc, recs, gaz, context=None):
        if self.client.status != 'enabled':
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

        def validate(obj):
            if not isinstance(obj, dict) or not isinstance(obj.get('findings'), list) or len(obj['findings']) > 8:
                raise ValueError()
            return [normalize_finding(row, text) for row in obj['findings']]
        self.stats['called'] += 1
        try:
            rows = self.client.call('web-extract-1', EXTRACT_INSTRUCTION,
                                    {'url': doc.url, 'published_at': doc.published_at, 'text': text, 'known_targets': targets,
                                     'rule_ambiguities': [r.get('ambiguity') for r in recs]}, validate, max_tokens=4096)
        except DeepSeekError:
            self.stats['failed'] += 1
            return []
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
