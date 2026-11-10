"""Conservative model reservations against CostMeter AND a shared KST-day allowance."""
import datetime as dt
import json
from zoneinfo import ZoneInfo
from kg_daemon.db import utcnow
from kg_daemon.runner import CostCapExceeded


def kst_day():
    return dt.datetime.now(ZoneInfo('Asia/Seoul')).date().isoformat()


class Budget:
    def __init__(self, meter, daily_cap=20, day=kst_day):
        self.meter, self.conn, self.day = meter, meter.conn, day
        self.workload = 'fresh'
        self.hold_attempt = None
        self.hold_attempts = set()
        self.daily_cap = max(0, min(20, float(daily_cap)))

    def lookup(self, key):
        return self.conn.execute('SELECT * FROM ai_request WHERE request_key=?', (key,)).fetchone()

    def _held_units(self, day):
        owned = set(self.hold_attempts)
        if self.hold_attempt:
            owned.add(self.hold_attempt)
        return sum(r['units'] for r in self.conn.execute(
            "SELECT attempt_id,units FROM ai_review_hold WHERE day=? AND state='held'", (day,))
            if r['attempt_id'] not in owned)

    def room_tokens(self):
        """Tokens still reservable under both the run cap and the shared day cap."""
        m = self.meter
        run_left = m.cap - m.used
        daily = self.conn.execute('SELECT COALESCE(SUM(units),0) FROM ai_request WHERE day=?', (self.day(),)).fetchone()[0]
        holds = self._held_units(self.day())
        left = min(run_left, self.daily_cap - float(daily) - holds)
        if self.workload == 'recheck':
            used = self.conn.execute("SELECT COALESCE(SUM(units),0) FROM ai_request WHERE day=? AND workload='recheck'", (self.day(),)).fetchone()[0]
            left = min(left, min(8, .4 * self.daily_cap) - used - holds)
        rate = m.unit_costs.get('llm_tokens_k') or 0
        if left <= 0 or rate <= 0:
            return 0
        return int(left / rate * 1000)

    def reserve(self, key, task, model, tokens):
        c, m = self.conn, self.meter
        units = tokens / 1000 * m.unit_costs['llm_tokens_k']
        if units <= 0:
            raise CostCapExceeded('model allowance disabled')
        c.commit()
        try:
            c.execute('BEGIN IMMEDIATE')
            if self.lookup(key):
                c.rollback()
                return False
            used = c.execute('SELECT cost_used_units FROM daemon_run WHERE run_id=?', (m.run_id,)).fetchone()[0]
            day = self.day()
            daily = c.execute('SELECT COALESCE(SUM(units),0) FROM ai_request WHERE day=?', (day,)).fetchone()[0]
            holds = self._held_units(day)
            recheck_used = c.execute("SELECT COALESCE(SUM(units),0) FROM ai_request WHERE day=? AND workload='recheck'", (day,)).fetchone()[0]
            if (used + units > m.cap or daily + units + holds > self.daily_cap
                or (self.workload == 'recheck' and recheck_used + units + holds > min(8, .4 * self.daily_cap))):
                raise CostCapExceeded('model reservation exceeds run/day cap')
            c.execute('INSERT INTO ai_request(request_key,run_id,day,task,model,state,units,reserved_tokens,created_at,workload) VALUES (?,?,?,?,?,?,?,?,?,?)',
                      (key, m.run_id, day, task, model, 'unknown', units, tokens, utcnow(), self.workload))
            owned = set(self.hold_attempts) | ({self.hold_attempt} if self.hold_attempt else set())
            for attempt in owned:
                c.execute("UPDATE ai_review_hold SET state='consumed',request_key=? WHERE attempt_id=? AND state='held'", (key, attempt))
            c.execute('INSERT INTO cost_ledger(run_id,stage,kind,units,at) VALUES (?,?,?,?,?)',
                      (m.run_id, m.stage or '?', 'llm_tokens_k', units, utcnow()))
            c.execute('UPDATE daemon_run SET cost_used_units=cost_used_units+? WHERE run_id=?', (units, m.run_id))
            c.commit()
            m.used = used + units
            return True
        except Exception:
            c.rollback()
            raise

    def settle(self, key, usage, result=None, error=None):
        c, m = self.conn, self.meter
        c.commit()
        try:
            c.execute('BEGIN IMMEDIATE')
            row = self.lookup(key)
            if row['state'] != 'unknown':
                c.rollback()
                return
            units = usage['total_tokens'] / 1000 * m.unit_costs['llm_tokens_k']
            delta = units - row['units']
            c.execute('UPDATE ai_request SET state=?,units=?,prompt_tokens=?,completion_tokens=?,cache_tokens=?,result_json=?,error=? WHERE request_key=?',
                      ('failed' if error else 'done', units, usage['prompt_tokens'], usage['completion_tokens'], usage.get('prompt_cache_hit_tokens', 0),
                       json.dumps(result, ensure_ascii=False) if result is not None else None, error, key))
            c.execute('INSERT INTO cost_ledger(run_id,stage,kind,units,at) VALUES (?,?,?,?,?)',
                      (m.run_id, m.stage or '?', 'llm_settlement', delta, utcnow()))
            c.execute('UPDATE daemon_run SET cost_used_units=cost_used_units+? WHERE run_id=?', (delta, row['run_id']))
            # Attribution only; ai_request/cost_ledger are the authoritative charged amounts.
            c.execute('INSERT INTO budget_ledger(cycle_id,pool,kind,units,at) VALUES (?,?,?,?,?)',
                      (row['run_id'], 'refresh' if row['workload'] == 'recheck' else 'discovery', 'llm_tokens_k', units, utcnow()))
            c.commit()
            m.used = c.execute('SELECT cost_used_units FROM daemon_run WHERE run_id=?', (m.run_id,)).fetchone()[0]
        except Exception:
            c.rollback()
            raise
        if usage['total_tokens'] > row['reserved_tokens']:
            raise CostCapExceeded('provider usage exceeded reservation; model phase stopped')

    def fail(self, key, code):
        self.conn.execute('UPDATE ai_request SET error=? WHERE request_key=? AND state=?', (code, key, 'unknown'))
        self.conn.commit()
