"""Reuse the collector's extraction contract for eligible local documents only."""
from types import SimpleNamespace
from kg_collect.deepseek import Adapters
from kg_collect.targets import Gazetteer


def extract(ctx, doc, body):
    if body.get('collect') or body.get('instagram'):
        return []
    text = (body.get('title') or '').strip()
    if not text or body.get('extracted_facts'):
        return []
    adapter = ctx.env.get('_deepseek')
    if adapter is None:
        adapter = ctx.env['_deepseek'] = Adapters.for_meter(ctx.cost)
    adapter.client.budget.meter = ctx.cost
    d = SimpleNamespace(text=text, url=doc['url'], published_at=doc['published_at'], content_hash=doc['content_hash'])
    out = []
    for r in adapter.extract(d, [], Gazetteer(ctx.conn)):
        # All rows including unmapped labels have already-valid quotes. They must reach review.
        out.append(('menu' if r['label'] in ('menu', 'collab') else {'career': 'employment', 'employment_claim': 'employment', 'mentorship_claim': 'mentorship'}.get(r['label'], r['label']),
                    {'label': r['label'], 'target_id': body.get('target_id'), 'effective_date': r.get('date_text'),
                     'pending_only': True, 'asserted_by': 'ai_inference', 'model_meta': adapter.client.last_meta,
                     'evidence_locator': {'span_start': r['span_start'], 'span_end': r['span_end'], 'extractor_version': r['extractor_version']}}, r['quote']))
    return out
