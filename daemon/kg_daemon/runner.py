"""Staged, checkpointed, resumable daemon core.

Guarantees (tested in tests/test_daemon.py):
  * Single-flight: a run lock (lease) prevents concurrent runs; an expired lease can be taken over.
  * Each stage has a checkpoint row; 'done' stages are never re-run when a run is resumed.
  * Bounded retries per stage with capped backoff; on exhaustion the stage goes to failure_queue and the run
    stops at that stage ('failed', failed_stage=...). Requeue the failed item after repair.
  * Stage handlers are idempotent by construction (INSERT OR IGNORE on deterministic keys); resume may re-enter a
    stage that crashed mid-way."""
import dataclasses
import json
import re
import time
from typing import Callable

from kg_collect.guard import Guard, fingerprint

from .db import new_id, utcnow

STAGES = ('source_discovery', 'incremental_fetch', 'change_detection', 'extraction', 'entity_resolution',
          'evidence_verification', 'state_history_update', 'service_output_recompute', 'quality_check')

DEFAULT_CONFIG = {
    'lock_name': 'kg-daemon',
    'daemon_name': 'kg-daemon',
    'lock_ttl_sec': 1800,
    'backoff_base_sec': 2.0,
    'backoff_max_sec': 30.0,
    'unit_costs': {'http_request': 1.0, 'llm_tokens_k': 0.5, 'db_write': 0.0},
    'stale_open_employment_days': 180,
}


class NonRetryable(Exception):
    """Raise for deterministic failures (e.g. invariant violated): goes straight to the failure queue."""


class NoActiveSources(Exception):
    """All sources paused/disabled: run pauses (paused_source) rather than failing."""


@dataclasses.dataclass
class StageResult:
    output: dict = dataclasses.field(default_factory=dict)
    cursor: dict | None = None      # optional continuation cursor for a runnable stage


class CostMeter:
    def __init__(self, conn, run_id, unit_costs):
        self.conn, self.run_id, self.unit_costs = conn, run_id, unit_costs
        self.used = conn.execute('SELECT cost_used_units u FROM daemon_run WHERE run_id=?', (run_id,)).fetchone()['u']
        self.stage = None

    def charge(self, kind: str, count: float = 1.0) -> None:
        units = self.unit_costs.get(kind, 1.0) * count
        if units <= 0:
            return
        with self.conn:
            self.conn.execute('INSERT INTO cost_ledger(run_id,stage,kind,units,at) VALUES (?,?,?,?,?)',
                              (self.run_id, self.stage or '?', kind, units, utcnow()))
            self.used = self.conn.execute('UPDATE daemon_run SET cost_used_units=cost_used_units+? WHERE run_id=? RETURNING cost_used_units',
                                          (units, self.run_id)).fetchone()[0]


@dataclasses.dataclass
class StageContext:
    conn: object
    run_id: str
    stage: str
    config: dict
    cost: CostMeter
    prev: dict                 # outputs of earlier stages in this run
    cursor: dict               # this stage's saved cursor (resume mid-stage)
    env: dict                  # injected collaborators: registry, fetchers, root, clock ...

    def save_cursor(self, cursor: dict) -> None:
        self.conn.execute('UPDATE stage_checkpoint SET cursor_json=? WHERE run_id=? AND stage=?', (json.dumps(cursor), self.run_id, self.stage))
        self.conn.commit()


Handler = Callable[[StageContext], StageResult]


class Daemon:
    def __init__(self, conn, handlers: dict[str, Handler], config: dict | None = None, env: dict | None = None,
                 sleep=time.sleep, clock=utcnow, owner: str | None = None, stages=STAGES):
        self.conn, self.handlers, self.env, self.sleep, self.clock = conn, handlers, env or {}, sleep, clock
        self.config = {**DEFAULT_CONFIG, **(config or {})}
        self.owner = owner or new_id('own')
        self.stages = stages
        missing = [s for s in stages if s not in handlers]
        if missing:
            raise ValueError(f'no handler for stages: {missing}')

    # ── lock ──
    def acquire_lock(self) -> bool:
        import datetime as dt
        now = self.clock()
        ttl = dt.timedelta(seconds=self.config['lock_ttl_sec'])
        exp = (dt.datetime.strptime(now, '%Y-%m-%dT%H:%M:%SZ') + ttl).strftime('%Y-%m-%dT%H:%M:%SZ')
        cur = self.conn.execute("""INSERT INTO run_lock(lock_name,owner,acquired_at,expires_at) VALUES (?,?,?,?)
            ON CONFLICT(lock_name) DO UPDATE SET owner=excluded.owner, acquired_at=excluded.acquired_at, expires_at=excluded.expires_at
            WHERE run_lock.expires_at <= ?""", (self.config['lock_name'], self.owner, now, exp, now))
        self.conn.commit()
        return cur.rowcount == 1

    def release_lock(self) -> None:
        self.conn.execute('UPDATE run_lock SET expires_at=? WHERE lock_name=? AND owner=?', ('1970-01-01T00:00:00Z', self.config['lock_name'], self.owner))
        self.conn.commit()

    # ── run lifecycle ──
    def _resumable(self):
        return self.conn.execute("""SELECT r.* FROM daemon_run r WHERE r.daemon=? AND r.status IN ('failed','paused_cost_cap','paused_source','running')
            AND NOT EXISTS (SELECT 1 FROM daemon_run n WHERE n.resumed_from=r.run_id) ORDER BY r.started_at DESC, r.rowid DESC LIMIT 1""", (self.config['daemon_name'],)).fetchone()

    def close_superseded_runs(self) -> int:
        """A run left 'running' (its process died: no lock, no finished_at) that a later run resumed is over: mark it 'abandoned' with
        finished_at = the moment its successor took over. Idempotent; also repairs rows written before resume closed them."""
        cur = self.conn.execute("""UPDATE daemon_run SET status='abandoned',
                                     finished_at=(SELECT MIN(n.started_at) FROM daemon_run n WHERE n.resumed_from=daemon_run.run_id)
                                   WHERE daemon=? AND status='running'
                                     AND EXISTS (SELECT 1 FROM daemon_run n WHERE n.resumed_from=daemon_run.run_id)""", (self.config['daemon_name'],))
        return cur.rowcount

    def close_orphaned_runs(self, keep_run_id: str | None = None) -> int:
        """We hold this daemon's lock, so any other 'running' row is a dead process (SIGTERM/timeout
        without a closer, or a crash). Mark those 'abandoned' — the same status close_superseded_runs
        uses for a leftover running predecessor. The run we are about to resume is kept so
        _start_run can attach it via resumed_from; close_superseded_runs then finishes it.
        Idempotent; never touches another daemon's rows or already-final statuses."""
        now = self.clock()
        if keep_run_id:
            cur = self.conn.execute("""UPDATE daemon_run SET status='abandoned', finished_at=?
                                       WHERE daemon=? AND status='running' AND run_id!=?
                                         AND NOT EXISTS (SELECT 1 FROM daemon_run n WHERE n.resumed_from=daemon_run.run_id)""",
                                    (now, self.config['daemon_name'], keep_run_id))
        else:
            cur = self.conn.execute("""UPDATE daemon_run SET status='abandoned', finished_at=?
                                       WHERE daemon=? AND status='running'
                                         AND NOT EXISTS (SELECT 1 FROM daemon_run n WHERE n.resumed_from=daemon_run.run_id)""",
                                    (now, self.config['daemon_name']))
        self.conn.commit()
        return cur.rowcount

    def _finish_if_running(self, run_id, status, failed_stage=None) -> int:
        """Close a still-open run (SIGTERM or a timeout closer). No-op if already final."""
        cur = self.conn.execute(
            "UPDATE daemon_run SET status=?, failed_stage=?, finished_at=? WHERE run_id=? AND status='running'",
            (status, failed_stage, self.clock(), run_id))
        self.conn.commit()
        return cur.rowcount

    def _start_run(self, trigger: str, resume: bool) -> str:
        prev = self._resumable() if resume else None
        run_id = new_id('run')
        self.conn.execute('INSERT INTO daemon_run(run_id,daemon,status,trigger,cost_cap_units,resumed_from,started_at) VALUES (?,?,?,?,?,?,?)',
                          (run_id, self.config['daemon_name'], 'running', trigger, 0, prev['run_id'] if prev else None, self.clock()))
        # we hold the lock, so a resumed 'running' predecessor is dead: close it (failed/paused predecessors keep their own final status)
        self.close_superseded_runs()
        for i, st in enumerate(self.stages):
            carried = None
            if prev:
                carried = self.conn.execute("SELECT * FROM stage_checkpoint WHERE run_id=? AND stage=? AND status='done'", (prev['run_id'], st)).fetchone()
            if carried:
                self.conn.execute('INSERT INTO stage_checkpoint(run_id,stage,seq,status,attempts,idempotency_key,cursor_json,output_json,started_at,finished_at) VALUES (?,?,?,?,?,?,?,?,?,?)',
                                  (run_id, st, i, 'done', carried['attempts'], carried['idempotency_key'], carried['cursor_json'], carried['output_json'], carried['started_at'], carried['finished_at']))
            else:
                cursor = '{}'
                if prev:
                    pc = self.conn.execute('SELECT cursor_json FROM stage_checkpoint WHERE run_id=? AND stage=?', (prev['run_id'], st)).fetchone()
                    cursor = pc['cursor_json'] if pc else '{}'
                self.conn.execute('INSERT INTO stage_checkpoint(run_id,stage,seq,status,cursor_json) VALUES (?,?,?,?,?)', (run_id, st, i, 'pending', cursor))
        self.conn.commit()
        return run_id

    def _finish(self, run_id, status, failed_stage=None):
        self.conn.execute('UPDATE daemon_run SET status=?, failed_stage=?, finished_at=? WHERE run_id=?', (status, failed_stage, self.clock(), run_id))
        self.conn.commit()

    def run(self, trigger: str = 'manual', resume: bool = True) -> dict:
        if not self.acquire_lock():
            return {'status': 'locked', 'run_id': None}
        import signal
        run_id = None
        self._active_stage = None
        self._killed = False
        prev_term = signal.getsignal(signal.SIGTERM)

        def _on_sigterm(signum, frame):
            self._killed = True
            if run_id:
                try:
                    self.conn.rollback()
                except Exception:
                    pass
                try:
                    self._finish_if_running(run_id, 'failed', self._active_stage)
                except Exception:
                    pass
            if callable(prev_term) and prev_term not in (signal.SIG_DFL, signal.SIG_IGN):
                prev_term(signum, frame)
            raise KeyboardInterrupt

        try:
            signal.signal(signal.SIGTERM, _on_sigterm)
            keep = self._resumable() if resume else None
            self.close_orphaned_runs(keep_run_id=keep['run_id'] if keep else None)
            run_id = self._start_run(trigger, resume)
            cost = CostMeter(self.conn, run_id, self.config['unit_costs'])
            prev_out: dict = {}
            for st in self.stages:
                if self._killed:
                    break
                self._active_stage = st
                cp = self.conn.execute('SELECT * FROM stage_checkpoint WHERE run_id=? AND stage=?', (run_id, st)).fetchone()
                if cp['status'] == 'done':
                    prev_out[st] = json.loads(cp['output_json'])
                    continue
                outcome = self._run_stage(run_id, st, cost, prev_out)
                if outcome != 'done':
                    return self._report(run_id)
                prev_out[st] = json.loads(self.conn.execute('SELECT output_json FROM stage_checkpoint WHERE run_id=? AND stage=?', (run_id, st)).fetchone()['output_json'])
            if self._killed:
                try:
                    self.conn.rollback()
                except Exception:
                    pass
                self._finish_if_running(run_id, 'failed', self._active_stage)
                return self._report(run_id)
            self._finish(run_id, 'succeeded')
            return self._report(run_id)
        except KeyboardInterrupt:
            if run_id:
                try:
                    self.conn.rollback()
                except Exception:
                    pass
                self._finish_if_running(run_id, 'failed', self._active_stage)
                return self._report(run_id)
            raise
        finally:
            signal.signal(signal.SIGTERM, prev_term)
            self.release_lock()

    def _run_stage(self, run_id, st, cost, prev_out) -> str:
        cfg = self.config
        cp = self.conn.execute('SELECT * FROM stage_checkpoint WHERE run_id=? AND stage=?', (run_id, st)).fetchone()
        attempts = cp['attempts']
        cost.stage = st
        root = self.conn.execute('''WITH RECURSIVE chain(id,parent) AS (
            SELECT run_id,resumed_from FROM daemon_run WHERE run_id=?
            UNION ALL SELECT r.run_id,r.resumed_from FROM daemon_run r JOIN chain c ON r.run_id=c.parent)
            SELECT id FROM chain WHERE parent IS NULL''', (run_id,)).fetchone()[0]
        guard = Guard(self.conn, root, cfg['daemon_name'] + ':' + st)
        def failed(reason, key, count, *, new_failure=True):
            self.conn.execute("""UPDATE stage_checkpoint SET status='failed',attempts=?,error=?,finished_at=?
                WHERE run_id=? AND stage=?""", (count, reason, self.clock(), run_id, st))
            if new_failure:
                self.conn.execute("""INSERT INTO failure_queue(failure_id,run_id,stage,item_key,error,attempts,created_at)
                    VALUES (?,?,?,?,?,?,?)""", (new_id('fail'), run_id, st, key, reason, count, self.clock()))
            self._finish(run_id, 'failed', st)
            return 'failed'

        while True:
            cursor = json.loads(self.conn.execute('SELECT cursor_json FROM stage_checkpoint WHERE run_id=? AND stage=?',
                                                  (run_id, st)).fetchone()['cursor_json'] or '{}')
            key = fingerprint([root, cursor])
            item = guard.item(key)
            blocked = self.conn.execute("""SELECT failure_id,state,error,attempts FROM failure_queue
                WHERE stage=? AND item_key=? AND state IN ('open','requeued')
                ORDER BY rowid DESC LIMIT 1""", (st, key)).fetchone()
            if blocked and blocked['state'] == 'requeued':
                guard.complete(key)
                guard.release()
                self.conn.execute("UPDATE failure_queue SET state='dismissed',resolved_at=? WHERE failure_id=?",
                                  (self.clock(), blocked['failure_id']))
                self.conn.commit()
            elif guard.reason or (item and item['reason']) or blocked:
                reason = guard.reason or (item and item['reason']) or blocked['error']
                return failed(reason, key, item['attempts'] if item else (blocked['attempts'] if blocked else attempts),
                              new_failure=not blocked)
            self.conn.execute("""UPDATE stage_checkpoint SET status='running',attempts=?,started_at=?
                WHERE run_id=? AND stage=?""", (attempts, self.clock(), run_id, st))
            self.conn.commit()
            ctx = StageContext(self.conn, run_id, st, cfg, cost, dict(prev_out), cursor, self.env)
            try:
                res = self.handlers[st](ctx)
            except NoActiveSources as e:
                self.conn.execute("UPDATE stage_checkpoint SET status='pending', error=? WHERE run_id=? AND stage=?", (str(e)[:300], run_id, st))
                self._finish(run_id, 'paused_source', st)
                return 'paused'
            except Exception as e:  # noqa: BLE001 — every handler error is bounded-retry material
                self.conn.rollback()
                attempts += 1
                err = type(e).__name__
                if isinstance(e, NonRetryable) and re.fullmatch(r'[a-z0-9_]{1,80}', str(e)):
                    err = str(e)
                reason = guard.failure(err)
                guard.attempt(key, maximum=cfg.get('max_attempts'))
                item = guard.item(key)
                # Save the failure even if the next invocation takes over the lease.
                if isinstance(e, NonRetryable) or reason or item['reason']:
                    return failed(reason or item['reason'] or err, key, item['attempts'])
                self.conn.execute("UPDATE stage_checkpoint SET attempts=?, error=? WHERE run_id=? AND stage=?", (attempts, err, run_id, st))
                self.conn.commit()
                self.sleep(min(cfg['backoff_max_sec'], cfg['backoff_base_sec'] * (2 ** (attempts - 1))))
                continue
            if isinstance(res, StageResult) and res.cursor is not None:
                if res.cursor != cursor:
                    guard.progress(fingerprint(res.cursor))
                ctx.save_cursor(res.cursor)
                if guard.runnable(res.cursor):
                    return failed(guard.reason, fingerprint([root, res.cursor]), attempts)
                self.conn.commit()
                continue
            guard.progress(fingerprint(cursor))
            guard.complete(fingerprint([root, cursor]))
            out = res.output if isinstance(res, StageResult) else (res or {})
            self.conn.execute("UPDATE stage_checkpoint SET status='done', attempts=?, output_json=?, error=NULL, finished_at=? WHERE run_id=? AND stage=?",
                              (attempts + 1, json.dumps(out, ensure_ascii=False, sort_keys=True), self.clock(), run_id, st))
            # the stage now succeeds on a resumed run: earlier failures of this stage in the resume chain are closed as 'dismissed' with resolved_at (kept, not deleted)
            self.conn.execute("""UPDATE failure_queue SET state='dismissed', resolved_at=? WHERE stage=? AND state IN ('open','requeued')
                                 AND run_id IN (SELECT run_id FROM daemon_run WHERE daemon=?)""", (self.clock(), st, self.config['daemon_name']))
            self.conn.commit()
            return 'done'

    def _report(self, run_id) -> dict:
        r = self.conn.execute('SELECT * FROM daemon_run WHERE run_id=?', (run_id,)).fetchone()
        stages = {c['stage']: c['status'] for c in self.conn.execute('SELECT stage,status FROM stage_checkpoint WHERE run_id=? ORDER BY seq', (run_id,))}
        return {'run_id': run_id, 'status': r['status'], 'failed_stage': r['failed_stage'], 'cost_used': r['cost_used_units'], 'resumed_from': r['resumed_from'], 'stages': stages}
