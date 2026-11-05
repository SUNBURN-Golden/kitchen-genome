"""kg-daemon CLI:  python3 -m daemon.kg_daemon ...  (run from repo root with PYTHONPATH=daemon)
   migrate | seed-migrate | run | status | requeue"""
import argparse
import json
import os
import sys

from .db import DEFAULT_DB, ROOT, connect, migrate


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog='kg-daemon')
    ap.add_argument('--db', default=DEFAULT_DB)
    sub = ap.add_subparsers(dest='cmd', required=True)
    sub.add_parser('migrate')
    sm = sub.add_parser('seed-migrate')
    sm.add_argument('--seed', default=os.path.join(ROOT, 'data', 'seed.json'))
    sm.add_argument('--dry-run', action='store_true')
    rn = sub.add_parser('run')
    rn.add_argument('--deepseek', action='store_true')
    rn.add_argument('--no-resume', action='store_true')
    rn.add_argument('--cost-cap', type=float)
    rn.add_argument('--collect-db', help='live collector DB (kg_collect extraction table); default: --db itself')
    rn.add_argument('--today')
    sub.add_parser('status')
    rq = sub.add_parser('requeue')
    rq.add_argument('failure_id')
    a = ap.parse_args(argv)
    conn = connect(':memory:' if getattr(a, 'dry_run', False) else a.db)
    migrate(conn)
    if a.cmd == 'migrate':
        print(json.dumps({'db': a.db, 'ok': True}))
    elif a.cmd == 'seed-migrate':
        from .seed_migration import migrate_seed
        print(json.dumps(migrate_seed(conn, json.load(open(a.seed, encoding='utf-8'))), ensure_ascii=False))
    elif a.cmd == 'run':
        from .runner import Daemon
        from .stages import HANDLERS
        cfg = {'cost_cap_units': a.cost_cap} if a.cost_cap is not None else {}
        raw_dir = os.path.join(os.path.dirname(os.path.abspath(a.db)), 'raw')
        env = {'raw_dir': raw_dir, 'collect_db': a.collect_db or (a.db if a.db != ':memory:' else None)}
        env['deepseek'] = a.deepseek
        if a.today:
            env['today'] = a.today
        rep = Daemon(conn, HANDLERS, cfg, env=env).run(resume=not a.no_resume)
        print(json.dumps(rep, ensure_ascii=False))
        return 0 if rep['status'] in ('succeeded', 'locked', 'paused_cost_cap', 'paused_source') else 1
    elif a.cmd == 'status':
        rows = [dict(r) for r in conn.execute('SELECT run_id,status,failed_stage,cost_used_units,started_at FROM daemon_run ORDER BY started_at DESC LIMIT 5')]
        fq = conn.execute("SELECT COUNT(*) c FROM failure_queue WHERE state='open'").fetchone()['c']
        rv = conn.execute("SELECT COUNT(*) c FROM review_queue WHERE state='open'").fetchone()['c']
        print(json.dumps({'runs': rows, 'open_failures': fq, 'open_reviews': rv}, ensure_ascii=False))
    elif a.cmd == 'requeue':
        conn.execute("UPDATE failure_queue SET state='requeued', resolved_at=datetime('now') WHERE failure_id=? AND state='open'", (a.failure_id,))
        conn.commit()
    return 0
