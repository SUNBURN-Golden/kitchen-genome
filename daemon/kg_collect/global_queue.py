"""Leased service-DB frontier. Persist a result and all children before acknowledging its lease."""
import json
from contextlib import contextmanager
from uuid import uuid4

from kg_daemon.db import stable_id

from .net import host_of

SWEEP = 'michelin-worldwide-v1'
LEASE_SECONDS = 180


@contextmanager
def write(conn):
    conn.execute('BEGIN IMMEDIATE')
    with conn:
        yield


def enqueue(conn, *, tier, kind, target, now, scope=None, priority=0, cursor=None, identity=None, origins=(), parent=None, fetch_id=None):
    key = stable_id('global', SWEEP, kind, identity or target)
    conn.execute("""INSERT INTO global_work(job_key,sweep_id,tier,kind,scope,target,host,cursor,priority,available_at,fetch_id)
        VALUES (?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(job_key) DO UPDATE SET
        available_at=CASE WHEN excluded.tier>tier AND kind IN ('venue','discover')
            AND state IN ('pending','deferred') THEN MIN(available_at,excluded.available_at) ELSE available_at END,
        tier=MAX(tier,excluded.tier), priority=MAX(priority,excluded.priority)""",
        (key, SWEEP, tier, kind, scope, target, '' if kind == 'extract' else host_of(target),
         json.dumps(cursor or {}, ensure_ascii=False), priority, now, fetch_id))
    if parent and parent != key:
        conn.execute('INSERT OR IGNORE INTO global_work_edge VALUES (?,?)', (parent, key))
    for branch, origin_tier in origins:
        conn.execute("""WITH RECURSIVE descendants(job_key) AS (
            SELECT ? UNION SELECT child_key FROM global_work_edge JOIN descendants ON parent_key=job_key)
            INSERT OR IGNORE INTO global_work_origin
            SELECT job_key,?,? FROM descendants""", (key, branch, origin_tier))
    if origins:
        conn.execute("""WITH RECURSIVE descendants(job_key) AS (
            SELECT ? UNION SELECT child_key FROM global_work_edge JOIN descendants ON parent_key=job_key)
            UPDATE global_work SET tier=MAX(tier,?),priority=CASE WHEN tier<? AND kind='extract'
                THEN MAX(priority,25) ELSE priority END WHERE job_key IN (SELECT job_key FROM descendants)""",
            (key, tier, tier))
    return key


def origins(conn, key):
    return [tuple(row) for row in conn.execute(
        'SELECT branch_id,tier FROM global_work_origin WHERE job_key=? ORDER BY tier DESC,branch_id', (key,))]


def cursor(conn):
    row = conn.execute('SELECT tier FROM global_sweep WHERE sweep_id=?', (SWEEP,)).fetchone()
    return row[0] if row else None


def _advance(conn, now):
    conn.execute("""UPDATE global_tier_pass AS p SET first_pass_at=?,
        disposition=CASE
          WHEN NOT EXISTS (SELECT 1 FROM global_work w WHERE w.scope=p.scope AND w.tier=p.tier
            AND w.sweep_id=p.sweep_id AND w.kind='list') THEN disposition
          WHEN EXISTS (SELECT 1 FROM global_work w WHERE w.scope=p.scope AND w.tier=p.tier
            AND w.sweep_id=p.sweep_id AND w.kind='list'
            AND COALESCE(w.result_state,w.state) IN ('deferred','review'))
            THEN CASE WHEN last_success_at IS NULL AND NOT EXISTS (
              SELECT 1 FROM global_work WHERE scope=p.scope AND tier=p.tier AND kind='list' AND fetch_id IS NOT NULL
            ) THEN 'deferred' ELSE 'partial' END
          WHEN disposition='no_list' THEN disposition
          ELSE 'complete' END,
        last_success_at=CASE WHEN NOT EXISTS (
          SELECT 1 FROM global_work w WHERE w.sweep_id=p.sweep_id AND w.tier=p.tier AND w.scope=p.scope
            AND w.kind='list' AND (w.fetch_id IS NULL OR w.reason IS NOT NULL))
          AND EXISTS (SELECT 1 FROM global_work w WHERE w.sweep_id=p.sweep_id AND w.tier=p.tier
            AND w.scope=p.scope AND w.kind='list') THEN COALESCE(last_success_at,?) ELSE last_success_at END
        WHERE sweep_id=? AND tier>=COALESCE((SELECT tier FROM global_sweep WHERE sweep_id=?),4)
          AND first_pass_at IS NULL AND NOT EXISTS (
            SELECT 1 FROM global_work w WHERE w.sweep_id=p.sweep_id AND w.tier=p.tier AND w.scope=p.scope
            AND w.kind='list' AND w.first_pass_at IS NULL)""", (now, now, SWEEP, SWEEP))
    tier = cursor(conn)
    while tier:
        if conn.execute("""SELECT 1 FROM global_work WHERE sweep_id=? AND tier=?
                            AND first_pass_at IS NULL LIMIT 1""", (SWEEP, tier)).fetchone():
            return
        tier -= 1
        conn.execute('UPDATE global_sweep SET tier=? WHERE sweep_id=?', (tier, SWEEP))


def _settle(conn, where, params):
    return conn.execute(f"""UPDATE global_work SET state=result_state,result_state=NULL,
        lease_token=NULL,lease_expires_at=NULL WHERE state='checkpointed' AND {where}""", params).rowcount


def claim(conn, now, *, busy=(), daily=False):
    """Claim in tier order. Busy hosts are skipped across the whole due frontier."""
    with write(conn):
        _settle(conn, 'lease_expires_at<=?', (now,))
        _advance(conn, now)
        row = conn.execute("""SELECT w.* FROM global_work w JOIN global_sweep s USING(sweep_id)
            LEFT JOIN global_scope g ON g.scope=w.scope
            WHERE w.sweep_id=? AND w.tier>=s.tier AND w.available_at<=?
              AND (w.state IN ('pending','deferred') OR (w.state='leased' AND w.lease_expires_at<=?))
              AND NOT EXISTS (SELECT 1 FROM global_work h WHERE h.sweep_id=w.sweep_id
                AND h.tier>w.tier AND h.first_pass_at IS NULL)
              AND (?=0 OR w.first_pass_at IS NOT NULL OR EXISTS (
                SELECT 1 FROM json_each(g.countries_json) WHERE value='KR'))
              AND (w.host='' OR (
                w.host NOT IN (SELECT value FROM json_each(?)) AND NOT EXISTS (
                  SELECT 1 FROM global_work l WHERE l.host=w.host AND l.state IN ('leased','checkpointed')
                    AND l.lease_expires_at>?)))
            ORDER BY w.tier DESC,
                CASE WHEN ?=1 AND EXISTS (SELECT 1 FROM json_each(g.countries_json) WHERE value='KR') THEN 0 ELSE 1 END,
                w.priority DESC, w.available_at, w.job_key LIMIT 1""",
            (SWEEP, now, now, daily, json.dumps(sorted(busy)), now, daily)).fetchone()
        if row is None:
            return
        token = uuid4().hex
        conn.execute("""UPDATE global_work SET state='leased',lease_token=?,lease_expires_at=?,
            attempts=attempts+1 WHERE job_key=?""", (token, now + LEASE_SECONDS, row['job_key']))
        return dict(row, state='leased', lease_token=token, lease_expires_at=now + LEASE_SECONDS, attempts=row['attempts'] + 1)


def owns(conn, job, now):
    return conn.execute("""SELECT 1 FROM global_work WHERE job_key=? AND state='leased'
        AND lease_token=? AND lease_expires_at>?""", (job['job_key'], job['lease_token'], now)).fetchone() is not None


def checkpoint(conn, job, now, *, state='done', available_at=None, reason=None, fetch_id=None, attempted=True):
    """Call in the transaction that writes the result. Its owner must validate the lease first."""
    conn.execute("""UPDATE global_work SET state='checkpointed',result_state=?,available_at=?,reason=?,
        fetch_id=COALESCE(?,fetch_id), first_pass_at=CASE WHEN ? THEN COALESCE(first_pass_at,?) ELSE first_pass_at END
        WHERE job_key=? AND lease_token=? AND state='leased'""",
        (state, now if available_at is None else available_at, reason, fetch_id, attempted, now, job['job_key'], job['lease_token']))


def acknowledge(conn, job, now):
    with write(conn):
        changed = _settle(conn, 'job_key=? AND lease_token=? AND lease_expires_at>?',
                          (job['job_key'], job['lease_token'], now))
        if changed:
            _advance(conn, now)
        return bool(changed)


def due(conn, now):
    return conn.execute("""SELECT COUNT(*) FROM global_work w JOIN global_sweep s USING(sweep_id)
        WHERE w.sweep_id=? AND w.tier>=s.tier AND
          ((w.state IN ('pending','deferred') AND w.available_at<=?) OR
           (w.state IN ('leased','checkpointed') AND w.lease_expires_at<=?))""", (SWEEP, now, now)).fetchone()[0]
