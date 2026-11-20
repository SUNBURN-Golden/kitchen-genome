"""Durable circuit breakers for runnable lanes and individual work items."""
import hashlib
import json
import os
import re


def threshold(name, default):
    return max(1, int(os.environ.get('KG_GUARD_' + name, default)))


def signature(value):
    """Keep identifiers and arbitrary exception messages out of guard records."""
    token = str(value).split(':', 1)[0].split(' ', 1)[0].lower()
    return token[:80] if re.fullmatch(r'[a-z0-9_-]+', token) else 'unknown'


def fingerprint(state):
    return hashlib.sha256(json.dumps(state, sort_keys=True).encode()).hexdigest()


class Guard:
    def __init__(self, conn, scope, lane):
        self.conn, self.scope, self.lane = conn, scope, lane
        self.failures = threshold('IDENTICAL_FAILURES', 5)
        self.stalls = threshold('NO_PROGRESS', 10)
        self.attempts = threshold('MAX_ATTEMPTS', 3)

    def _state(self):
        return self.conn.execute('SELECT * FROM guard_lane WHERE scope=? AND lane=?',
                                 (self.scope, self.lane)).fetchone()

    @property
    def reason(self):
        row = self._state()
        return row['reason'] if row else None

    def progress(self, item):
        self.conn.execute("""INSERT INTO guard_lane(scope,lane,last_committed) VALUES (?,?,?)
            ON CONFLICT(scope,lane) DO UPDATE SET signature=NULL,failures=0,fingerprint=NULL,
            stalled=0,last_committed=excluded.last_committed""", (self.scope, self.lane, item))

    def failure(self, code):
        mark = signature(code)
        row = self._state()
        count = row['failures'] + 1 if row and row['signature'] == mark else 1
        reason = 'identical_failures:' + mark if count >= self.failures else None
        self.conn.execute("""INSERT INTO guard_lane(scope,lane,signature,failures,reason) VALUES (?,?,?,?,?)
            ON CONFLICT(scope,lane) DO UPDATE SET signature=excluded.signature,failures=excluded.failures,
            reason=COALESCE(guard_lane.reason,excluded.reason)""", (self.scope, self.lane, mark, count, reason))
        return self.reason

    def runnable(self, state):
        mark = fingerprint(state)
        row = self._state()
        count = row['stalled'] + 1 if row and row['fingerprint'] == mark else 1
        reason = 'no_progress' if count >= self.stalls else None
        self.conn.execute("""INSERT INTO guard_lane(scope,lane,fingerprint,stalled,reason) VALUES (?,?,?,?,?)
            ON CONFLICT(scope,lane) DO UPDATE SET fingerprint=excluded.fingerprint,stalled=excluded.stalled,
            reason=COALESCE(guard_lane.reason,excluded.reason)""", (self.scope, self.lane, mark, count, reason))
        return self.reason

    def release(self):
        self.conn.execute('''UPDATE guard_lane SET reason=NULL,failures=0,fingerprint=NULL,stalled=0
            WHERE scope=? AND lane=?''', (self.scope, self.lane))

    def item(self, key):
        return self.conn.execute('SELECT * FROM guard_item WHERE lane=? AND item_key=?',
                                 (self.lane, key)).fetchone()

    def attempt(self, key, *, previous=0, maximum=None):
        """Charge an attempt before dispatch, surviving a crash or lease takeover."""
        limit = min(self.attempts, maximum) if maximum is not None else self.attempts
        return self.conn.execute("""INSERT INTO guard_item(lane,item_key,attempts,reason)
            VALUES (?,?,?,CASE WHEN ?>=? THEN 'attempts_exhausted' END)
            ON CONFLICT(lane,item_key) DO UPDATE SET attempts=max(attempts,?)+1,
            reason=CASE WHEN max(attempts,?)+1>=? THEN 'attempts_exhausted' END
            WHERE attempts<? AND reason IS NULL RETURNING attempts""",
            (self.lane, key, previous + 1, previous + 1, limit, previous, previous, limit, limit)).fetchone()

    def defer(self, key):
        self.conn.execute('UPDATE guard_item SET attempts=attempts-1,reason=NULL WHERE lane=? AND item_key=? AND attempts>0',
                          (self.lane, key))

    def complete(self, key):
        self.conn.execute('DELETE FROM guard_item WHERE lane=? AND item_key=?', (self.lane, key))
