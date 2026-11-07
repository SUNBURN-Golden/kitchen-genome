"""Shared, metered model support. No network or credential reads at import time."""


def pending(row):
    """Fail closed on any known inference/social provenance, even forged claim fields."""
    r = dict(row)
    extractor = (r.get('extractor') or '').lower()
    return bool(r.get('pending_only') or r.get('collector_account') == 'kitchen.genome'
                or r.get('asserted_by') == 'ai_inference' or r.get('epistemic') == 'inference'
                or any(x in extractor for x in ('deepseek', 'model:', 'ai:')))
