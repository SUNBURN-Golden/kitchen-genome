"""Durable model request registration and token usage accounting."""
import datetime as dt
import json
from zoneinfo import ZoneInfo
from kg_daemon.db import utcnow


def kst_day():
    return dt.datetime.now(ZoneInfo('Asia/Seoul')).date().isoformat()


class Budget:
    def __init__(self, meter, day=kst_day):
        self.meter, self.conn, self.day = meter, meter.conn, day
        self.workload = 'fresh'

    def lookup(self, key):
        return self.conn.execute('SELECT * FROM ai_request WHERE request_key=?', (key,)).fetchone()

    def cache_hit(self, key):
        self.conn.execute('UPDATE ai_request SET cache_hit_reads=cache_hit_reads+1 WHERE request_key=?', (key,))
        self.conn.execute("""INSERT INTO ai_cache_day(day,request_key,hits) VALUES (?,?,1)
            ON CONFLICT(day,request_key) DO UPDATE SET hits=hits+1""", (self.day(), key))
        self.conn.commit()

    def reserve(self, key, task, model, tokens, *, provider=None, item=None):
        if type(tokens) is not int or tokens <= 0:
            raise ValueError('invalid request size')
        with self.conn:
            cur = self.conn.execute("""INSERT INTO ai_request(request_key,run_id,day,task,model,state,units,
                  reserved_tokens,created_at,workload,provider) VALUES (?,?,?,?,?,'unknown',0,?,?,?,?)
                  ON CONFLICT(request_key) DO NOTHING""",
                  (key, self.meter.run_id, self.day(), task, model, tokens, utcnow(), self.workload, provider))
            if not cur.rowcount:
                return False
            if item:
                from kg_collect.guard import Guard
                guard = Guard(self.conn, self.meter.run_id, 'model:' + task)
                if not guard.attempt(item):
                    self.conn.execute('DELETE FROM ai_request WHERE request_key=?', (key,))
                    return None
        return True

    def settle(self, key, usage, result=None, error=None):
        c, m = self.conn, self.meter
        units = usage['total_tokens'] / 1000 * m.unit_costs['llm_tokens_k']
        with c:
            row = c.execute("""UPDATE ai_request SET state=?,units=?,prompt_tokens=?,completion_tokens=?,
                            cache_tokens=?,result_json=?,error=? WHERE request_key=? AND state='unknown'
                            RETURNING run_id,workload""",
                            ('failed' if error else 'done', units, usage['prompt_tokens'], usage['completion_tokens'],
                             usage.get('prompt_cache_hit_tokens', 0), json.dumps(result, ensure_ascii=False)
                             if result is not None else None, error, key)).fetchone()
            if row:
                c.execute('INSERT INTO cost_ledger(run_id,stage,kind,units,at) VALUES (?,?,?,?,?)',
                          (row['run_id'], m.stage or '?', 'llm_tokens_k', units, utcnow()))
                c.execute('UPDATE daemon_run SET cost_used_units=cost_used_units+? WHERE run_id=?', (units, row['run_id']))
                c.execute('INSERT INTO budget_ledger(cycle_id,pool,kind,units,at) VALUES (?,?,?,?,?)',
                          (row['run_id'], 'refresh' if row['workload'] == 'recheck' else 'discovery', 'llm_tokens_k', units, utcnow()))
        m.used = c.execute('SELECT cost_used_units FROM daemon_run WHERE run_id=?', (m.run_id,)).fetchone()[0]

    def fail(self, key, code):
        self.conn.execute('UPDATE ai_request SET error=? WHERE request_key=? AND state=?', (code, key, 'unknown'))
        self.conn.commit()
