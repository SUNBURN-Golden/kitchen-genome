"""python3 -m kg_collect <cmd>  (PYTHONPATH=daemon, from repo root)
   init | cycle [--no-resume] | status | scoreboard | gold <file> | reextract"""
import argparse
import json
import os
import sys
import time

from kg_daemon.db import DEFAULT_DB, connect, migrate, utcnow

from . import cycle, michelin

# Shell `timeout` (scripts/collect_daily.sh) is the hard wall. Leave FINALIZE_RESERVE_SEC
# after the work window so wrap-up can finish before exit 124. EVALUATE_RESERVE_SEC is
# reserved after extract for scoring/gaps/apply/quality (2026-11-26: extract ran to the
# deadline and a 120s leftover was not enough; 240s was then capped by work/6 at 230s
# under the old 1500s wall). The default wall is 1680s so a full 360s reserve still
# leaves 1200s for extract: 120 finalize + 360 evaluate + 1200 extract.
DEFAULT_COLLECT_TIMEOUT = 1680
FINALIZE_RESERVE_SEC = 120
EVALUATE_RESERVE_SEC = 360


def collect_windows(wall: float, evaluate_reserve: float = EVALUATE_RESERVE_SEC,
                    finalize_reserve: float = FINALIZE_RESERVE_SEC) -> tuple[float, float]:
    """Return (work_until, extract_until) offsets from cycle start, in seconds.

    On a short wall (tests) the evaluate reserve shrinks so it never takes more than
    a quarter of the work window. On the default 1680s wall it is the full 360s.
    """
    work = max(0.0, float(wall) - min(finalize_reserve, float(wall) / 5))
    reserved = min(evaluate_reserve, work / 4)
    return work, max(0.0, work - reserved)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog='kg-collect')
    ap.add_argument('--db', default=DEFAULT_DB)
    sub = ap.add_subparsers(dest='cmd', required=True)
    sub.add_parser('init')
    c = sub.add_parser('cycle')
    c.add_argument('--deepseek', action='store_true')
    c.add_argument('--no-resume', action='store_true')
    c.add_argument('--no-browser', action='store_true')
    sub.add_parser('status')
    sub.add_parser('scoreboard')
    daily = sub.add_parser('daily-summary')
    daily.add_argument('--heldout')
    daily.add_argument('--seed')
    sub.add_parser('review')
    merge = sub.add_parser('lineage-merge', help='record a reviewed entity redirect')
    merge.add_argument('kind', choices=('chef', 'branch'))
    merge.add_argument('from_id')
    merge.add_argument('to_id')
    merge.add_argument('--evidence-id', required=True)
    merge.add_argument('--reason', required=True)
    undo = sub.add_parser('lineage-unmerge', help='reverse a recorded entity redirect')
    undo.add_argument('redirect_id')
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
        now = time.monotonic()
        wall = float(os.environ.get('KG_COLLECT_TIMEOUT', str(DEFAULT_COLLECT_TIMEOUT)))
        work_until, extract_until = collect_windows(wall)
        bot = cycle.Bot(conn, data_dir=data_dir, deepseek=a.deepseek,
                        deadline=now + work_until, fetch_deadline=now + work_until / 2, extract_deadline=now + extract_until,
                        roster_config=michelin.load_config())
        from . import proxynet
        daily_scope = os.environ.get('KG_WEB_RUN_ID')
        bot.net.deadline = time.time() + max(0, bot.fetch_deadline - time.monotonic())
        bot.net.start_run(run_id=daily_scope, join=bool(daily_scope))
        try:
            proxynet.egress_ok(probe=lambda url, timeout: bot.net.get(url, diagnostic=True, timeout=timeout))
            rep = cycle.run_cycle(bot, resume=not a.no_resume)
            rep.update(egress=proxynet.DEFAULT_ROUTE.name, egress_reason=proxynet.DEFAULT_ROUTE.reason)
        finally:
            bot.net.finish_run()
        print(json.dumps(rep, ensure_ascii=False, indent=1))
        return 0 if rep['status'] in ('succeeded', 'locked') else 1
    if a.cmd == 'status':
        from .scoreboard import scoreboard
        print(json.dumps({'jobs': {f"{r['pool']}/{r['state']}": r['n'] for r in conn.execute('SELECT pool,state,COUNT(*) n FROM crawl_job GROUP BY pool,state')}, 'scoreboard': scoreboard(conn)}, ensure_ascii=False, indent=1))
    elif a.cmd == 'scoreboard':
        from .scoreboard import scoreboard
        print(json.dumps(scoreboard(conn), ensure_ascii=False, indent=1))
    elif a.cmd == 'daily-summary':
        from .scoreboard import daily_summary
        print('COLLECT_METRICS ' + json.dumps(daily_summary(conn, heldout_path=a.heldout, seed_path=a.seed),
                                              ensure_ascii=False, sort_keys=True))
    elif a.cmd == 'review':
        from .review import review_candidates
        print(json.dumps(review_candidates(conn), ensure_ascii=False, indent=1))
    elif a.cmd in ('lineage-merge', 'lineage-unmerge'):
        from . import global_queue, lineage_identity

        try:
            with global_queue.write(conn):
                if a.cmd == 'lineage-merge':
                    if not a.reason.strip():
                        raise ValueError('a review reason is required')
                    result = lineage_identity.merge(conn, a.kind, a.from_id, a.to_id, a.evidence_id, a.reason, utcnow())
                    print(json.dumps({'redirect_id': result}))
                else:
                    result = lineage_identity.reverse(conn, a.redirect_id, utcnow())
                    print(json.dumps({'reversed': bool(result)}))
        except ValueError as exc:
            ap.error(str(exc))
    elif a.cmd == 'gold':
        from .gold import run_file
        print(json.dumps(run_file(a.file, gaz_db=a.db if a.registry else None, docs_db=a.db if a.doc_context else None), ensure_ascii=False, indent=1))
    return 0


if __name__ == '__main__':
    sys.exit(main())
