"""Staged, checkpointed, resumable daemon core.

Guarantees (tested in tests/test_daemon_skeleton.py):
  * Single-flight: a run lock (lease) prevents concurrent runs; an expired lease can be taken over.
  * Each stage has a checkpoint row; 'done' stages are never re-run when a run is resumed.
  * Bounded retries per stage with capped backoff; on exhaustion the stage goes to failure_queue and the run
    stops at that stage ('failed', failed_stage=...). Next run resumes FROM that stage.
  * Cost cap per run (units); hitting it pauses the run (resumable), it never silently continues.
  * Stage handlers are idempotent by construction (INSERT OR IGNORE on deterministic keys); resume may re-enter a
    stage that crashed mid-way."""
import dataclasses
import json
import time
from typing import Callable

from .db import new_id, utcnow

STAGES = ('source_discovery', 'incremental_fetch', 'change_detection', 'extraction', 'entity_resolution',
          'evidence_verification', 'state_history_update', 'service_output_recompute', 'quality_check')

DEFAULT_CONFIG = {
    'lock_name': 'kg-daemon',
    'lock_ttl_sec': 1800,
    'max_attempts': 3,
    'backoff_base_sec': 2.0,
    'backoff_max_sec': 30.0,
    'cost_cap_units': 200.0,
    'unit_costs': {'http_request': 1.0, 'llm_tokens_k': 0.5, 'db_write': 0.0},
    'stale_open_employment_days': 180,
}


class CostCapExceeded(Exception):
    pass


class NonRetryable(Exception):
    """Raise for deterministic failures (e.g. invariant violated): goes straight to the failure queue."""


class NoActiveSources(Exception):
    """All sources paused/disabled: run pauses (paused_source) rather than failing."""


@dataclasses.dataclass
class StageResult:
    output: dict = dataclasses.field(default_factory=dict)
    cursor: dict | None = None      # only used when the handler stops early (cost cap) to resume mid-stage


class CostMeter:
    def __init__(self, conn, run_id, cap, unit_costs):
        self.conn, self.run_id, self.cap, self.unit_costs = conn, run_id, cap, unit_costs
        self.used = conn.execute('SELECT cost_used_units u FROM daemon_run WHERE run_id=?', (run_id,)).fetchone()['u']
        self.stage = None

    def charge(self, kind: str, count: float = 1.0) -> None:
        units = self.unit_costs.get(kind, 1.0) * count
        if units <= 0:
            return
        if self.used + units > self.cap:
            raise CostCapExceeded(f'{kind} x{count} would exceed cap {self.cap} (used {self.used})')
        self.used += units
        self.conn.execute('INSERT INTO cost_ledger(run_id,stage,kind,units,at) VALUES (?,?,?,?,?)', (self.run_id, self.stage or '?', kind, units, utcnow()))
        self.conn.execute('UPDATE daemon_run SET cost_used_units=? WHERE run_id=?', (self.used, self.run_id))
        self.conn.commit()


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
        return self.conn.execute("""SELECT r.* FROM daemon_run r WHERE r.status IN ('failed','paused_cost_cap','paused_source','running')
            AND NOT EXISTS (SELECT 1 FROM daemon_run n WHERE n.resumed_from=r.run_id) ORDER BY r.started_at DESC, r.rowid DESC LIMIT 1""").fetchone()

    def _start_run(self, trigger: str, resume: bool) -> str:
        prev = self._resumable() if resume else None
        run_id = new_id('run')
        self.conn.execute('INSERT INTO daemon_run(run_id,daemon,status,trigger,cost_cap_units,resumed_from,started_at) VALUES (?,?,?,?,?,?,?)',
                          (run_id, 'kg-daemon', 'running', trigger, self.config['cost_cap_units'], prev['run_id'] if prev else None, self.clock()))
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
        try:
            run_id = self._start_run(trigger, resume)
            cost = CostMeter(self.conn, run_id, self.config['cost_cap_units'], self.config['unit_costs'])
            prev_out: dict = {}
            for st in self.stages:
                cp = self.conn.execute('SELECT * FROM stage_checkpoint WHERE run_id=? AND stage=?', (run_id, st)).fetchone()
                if cp['status'] == 'done':
                    prev_out[st] = json.loads(cp['output_json'])
                    continue
                outcome = self._run_stage(run_id, st, cost, prev_out)
                if outcome != 'done':
                    return self._report(run_id)
                prev_out[st] = json.loads(self.conn.execute('SELECT output_json FROM stage_checkpoint WHERE run_id=? AND stage=?', (run_id, st)).fetchone()['output_json'])
            self._finish(run_id, 'succeeded')
            return self._report(run_id)
        finally:
            self.release_lock()

    def _run_stage(self, run_id, st, cost, prev_out) -> str:
        cfg = self.config
        cp = self.conn.execute('SELECT * FROM stage_checkpoint WHERE run_id=? AND stage=?', (run_id, st)).fetchone()
        attempts = cp['attempts']
        cost.stage = st
        while True:
            self.conn.execute("UPDATE stage_checkpoint SET status='running', attempts=?, started_at=? WHERE run_id=? AND stage=?", (attempts, self.clock(), run_id, st))
            self.conn.commit()
            cursor = json.loads(self.conn.execute('SELECT cursor_json FROM stage_checkpoint WHERE run_id=? AND stage=?', (run_id, st)).fetchone()['cursor_json'] or '{}')
            ctx = StageContext(self.conn, run_id, st, cfg, cost, dict(prev_out), cursor, self.env)
            try:
                res = self.handlers[st](ctx)
            except CostCapExceeded as e:
                self.conn.execute("UPDATE stage_checkpoint SET status='pending', error=? WHERE run_id=? AND stage=?", (str(e)[:300], run_id, st))
                self._finish(run_id, 'paused_cost_cap', st)
                return 'paused'
            except NoActiveSources as e:
                self.conn.execute("UPDATE stage_checkpoint SET status='pending', error=? WHERE run_id=? AND stage=?", (str(e)[:300], run_id, st))
                self._finish(run_id, 'paused_source', st)
                return 'paused'
            except Exception as e:  # noqa: BLE001 — every handler error is bounded-retry material
                self.conn.rollback()
                attempts += 1
                err = f'{type(e).__name__}: {e}'[:500]
                if isinstance(e, NonRetryable) or attempts >= cfg['max_attempts']:
                    self.conn.execute("UPDATE stage_checkpoint SET status='failed', attempts=?, error=?, finished_at=? WHERE run_id=? AND stage=?", (attempts, err, self.clock(), run_id, st))
                    self.conn.execute('INSERT INTO failure_queue(failure_id,run_id,stage,item_key,error,attempts,created_at) VALUES (?,?,?,?,?,?,?)',
                                      (new_id('fail'), run_id, st, None, err, attempts, self.clock()))
                    self._finish(run_id, 'failed', st)
                    return 'failed'
                self.conn.execute("UPDATE stage_checkpoint SET attempts=?, error=? WHERE run_id=? AND stage=?", (attempts, err, run_id, st))
                self.conn.commit()
                self.sleep(min(cfg['backoff_max_sec'], cfg['backoff_base_sec'] * (2 ** (attempts - 1))))
                continue
            out = res.output if isinstance(res, StageResult) else (res or {})
            self.conn.execute("UPDATE stage_checkpoint SET status='done', attempts=?, output_json=?, error=NULL, finished_at=? WHERE run_id=? AND stage=?",
                              (attempts + 1, json.dumps(out, ensure_ascii=False, sort_keys=True), self.clock(), run_id, st))
            self.conn.commit()
            return 'done'

    def _report(self, run_id) -> dict:
        r = self.conn.execute('SELECT * FROM daemon_run WHERE run_id=?', (run_id,)).fetchone()
        stages = {c['stage']: c['status'] for c in self.conn.execute('SELECT stage,status FROM stage_checkpoint WHERE run_id=? ORDER BY seq', (run_id,))}
        return {'run_id': run_id, 'status': r['status'], 'failed_stage': r['failed_stage'], 'cost_used': r['cost_used_units'], 'resumed_from': r['resumed_from'], 'stages': stages}
