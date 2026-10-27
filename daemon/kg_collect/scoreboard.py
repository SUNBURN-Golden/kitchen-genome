"""Scoreboard: the numbers the owner asked for. Every figure is computed from the DB (no hand-entered values)."""
import json
import os
import statistics

from kg_daemon.db import ROOT

from . import gaps
from .discover import initial_hosts

GOLD_SYNTH = os.path.join(ROOT, 'tests', 'fixtures', 'gold_synthetic.json')


def scoreboard(conn, bot=None, gold_paths=None) -> dict:
    init = bot.initial if bot else initial_hosts()
    promoted = [r['host'] for r in conn.execute("SELECT host FROM domain_profile WHERE state='promoted' AND in_initial_registry=0 ORDER BY host")]
    seen_new = conn.execute("SELECT COUNT(*) FROM domain_profile WHERE in_initial_registry=0").fetchone()[0]
    tgt = {r['status']: r['n'] for r in conn.execute("SELECT status, COUNT(*) n FROM target WHERE origin='discovered' GROUP BY status")}
    cov = gaps.coverage(conn)
    g_tot = conn.execute('SELECT COUNT(*) FROM gap').fetchone()[0]
    g_res = conn.execute("SELECT COUNT(*) FROM gap WHERE state='resolved'").fetchone()[0]
    g_cls = {r['klass']: r['n'] for r in conn.execute("SELECT klass, COUNT(*) n FROM gap WHERE state='open' GROUP BY klass")}
    lat = [r['latency_hours'] for r in conn.execute("SELECT latency_hours FROM field_history WHERE latency_hours IS NOT NULL")]
    changes = conn.execute('SELECT COUNT(*) FROM field_history').fetchone()[0]
    valid_items = conn.execute("""SELECT COUNT(*) FROM extraction WHERE epistemic IN ('claim','fact') AND ambiguity IS NULL
                                  AND (resolved_chef_target IS NOT NULL OR resolved_restaurant_target IS NOT NULL)""").fetchone()[0]
    spent = conn.execute('SELECT COALESCE(SUM(units),0) FROM budget_ledger').fetchone()[0]
    by_pool = {r['pool']: round(r['u'], 1) for r in conn.execute('SELECT pool, SUM(units) u FROM budget_ledger GROUP BY pool')}
    fetch = {r['outcome']: r['n'] for r in conn.execute('SELECT outcome, COUNT(*) n FROM fetch_record GROUP BY outcome')}
    sb = {
        'new_valid_sources': {'count': len(promoted), 'hosts': promoted, 'new_hosts_seen': seen_new},
        'new_targets': {'candidate': tgt.get('candidate', 0), 'promoted_active': tgt.get('active', 0), 'total_discovered': sum(tgt.values())},
        'required_field_coverage': {k: cov[k] for k in ('required_fields', 'filled_initial', 'coverage_initial', 'gaps_resolved_with_evidence', 'coverage_now')},
        'gap_resolution': {'gaps_total': g_tot, 'resolved_with_evidence': g_res, 'rate': round(g_res / g_tot, 4) if g_tot else None, 'open_by_class': g_cls},
        'change_latency_hours': {'changes_detected': changes, 'with_latency': len(lat), 'median': statistics.median(lat) if lat else None, 'max': max(lat) if lat else None},
        'cost': {'units_spent': round(spent, 1), 'by_pool': by_pool, 'valid_items': valid_items, 'units_per_valid_item': round(spent / valid_items, 3) if valid_items else None,
                 'units_per_new_valid_source': round(spent / len(promoted), 1) if promoted else None},
        'fetch_outcomes': fetch,
        'extractions': {r['epistemic']: r['n'] for r in conn.execute('SELECT epistemic, COUNT(*) n FROM extraction GROUP BY epistemic')},
    }
    gp = gold_paths if gold_paths is not None else [p for p in (GOLD_SYNTH, os.path.join(ROOT, 'data_store', 'gold', 'gold_real.json')) if os.path.exists(p)]
    if gp:
        from .gold import run_file
        sb['accuracy'] = {os.path.basename(p): {k: v for k, v in run_file(p).items() if k not in ('errors', 'by_label')} for p in gp}
    return sb
