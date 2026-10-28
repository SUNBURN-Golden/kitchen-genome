"""python3 -m kg_collect <cmd>  (PYTHONPATH=daemon, from repo root)
   init | cycle [--units N] [--no-resume] | status | scoreboard | gold <file> | reextract"""
import argparse
import json
import os
import sys

from kg_daemon.db import DEFAULT_DB, connect, migrate

from . import cycle


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog='kg-collect')
    ap.add_argument('--db', default=DEFAULT_DB)
    sub = ap.add_subparsers(dest='cmd', required=True)
    sub.add_parser('init')
    c = sub.add_parser('cycle')
    c.add_argument('--units', type=float, default=60)
    c.add_argument('--no-resume', action='store_true')
    c.add_argument('--no-browser', action='store_true')
    sub.add_parser('status')
    sub.add_parser('scoreboard')
    sub.add_parser('review')
    g = sub.add_parser('gold')
    g.add_argument('file')
    g.add_argument('--registry', action='store_true', help='score with the production registry in --db (not the gold file targets)')
    g.add_argument('--doc-context', action='store_true', help='extract each gold sentence inside its stored document (coreference)')
    a = ap.parse_args(argv)
    conn = connect(a.db)
    if a.cmd == 'init':
        print(json.dumps(cycle.init_db(conn), ensure_ascii=False))
        return 0
    migrate(conn)
    data_dir = os.path.dirname(os.path.abspath(a.db))
    if a.cmd == 'cycle':
        from .fetch import playwright_browser
        browser = None
        if not a.no_browser:
            try:
                browser = playwright_browser()
            except Exception:  # noqa: BLE001
                browser = None
        bot = cycle.Bot(conn, data_dir=data_dir, units=a.units, browser=browser)
        rep = cycle.run_cycle(bot, resume=not a.no_resume)
        print(json.dumps(rep, ensure_ascii=False, indent=1))
        return 0 if rep['status'] in ('succeeded', 'locked', 'paused_cost_cap') else 1
    if a.cmd == 'status':
        from .scoreboard import scoreboard
        print(json.dumps({'jobs': {f"{r['pool']}/{r['state']}": r['n'] for r in conn.execute('SELECT pool,state,COUNT(*) n FROM crawl_job GROUP BY pool,state')}, 'scoreboard': scoreboard(conn)}, ensure_ascii=False, indent=1))
    elif a.cmd == 'scoreboard':
        from .scoreboard import scoreboard
        print(json.dumps(scoreboard(conn), ensure_ascii=False, indent=1))
    elif a.cmd == 'review':
        from .review import review_candidates
        print(json.dumps(review_candidates(conn), ensure_ascii=False, indent=1))
    elif a.cmd == 'gold':
        from .gold import run_file
        print(json.dumps(run_file(a.file, gaz_db=a.db if a.registry else None, docs_db=a.db if a.doc_context else None), ensure_ascii=False, indent=1))
    return 0


if __name__ == '__main__':
    sys.exit(main())
