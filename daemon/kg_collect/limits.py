"""Durable ceilings shared by legacy crawl and kg_collect. KST days never depend on run IDs."""
import contextlib
import fcntl
from pathlib import Path
from kg_ai.budget import kst_day


class DailyLimit(Exception):
    pass


class Limits:
    def __init__(self, conn, day=kst_day, candidates=500, fetches=200, robots=200):
        self.conn, self.day = conn, day
        self.candidates, self.fetches = min(500, candidates), min(200, fetches)
        self.robots = min(200, robots)

    def admit(self, url):
        c, day = self.conn, self.day()
        c.commit()
        try:
            c.execute('BEGIN IMMEDIATE')
            if c.execute('SELECT 1 FROM web_daily_url WHERE day=? AND url=?', (day, url)).fetchone():
                c.commit()
                return True
            if c.execute('SELECT COUNT(*) FROM web_daily_url WHERE day=?', (day,)).fetchone()[0] >= self.candidates:
                c.commit()
                return False
            c.execute('INSERT INTO web_daily_url(day,url) VALUES (?,?)', (day, url))
            c.commit()
            return True
        except Exception:
            c.rollback()
            raise

    def dispatch(self, *, robots=False):
        c, day = self.conn, self.day()
        c.commit()
        try:
            c.execute('BEGIN IMMEDIATE')
            c.execute('INSERT OR IGNORE INTO web_daily_fetch(day) VALUES (?)', (day,))
            if robots:
                cur = c.execute('UPDATE web_daily_fetch SET robots=robots+1 WHERE day=? AND robots<?', (day, self.robots))
                cap = 'web_daily_robots_cap'
            else:
                cur = c.execute('UPDATE web_daily_fetch SET attempts=attempts+1 WHERE day=? AND attempts<?', (day, self.fetches))
                cap = 'web_daily_fetch_cap'
            if not cur.rowcount:
                raise DailyLimit(cap)
            c.commit()
        except Exception:
            c.rollback()
            raise

    @contextlib.contextmanager
    def network_lock(self):
        path = self.conn.execute('PRAGMA database_list').fetchone()[2]
        if not path:
            yield
            return
        with open(str(Path(path).parent / 'web_dispatch.lock'), 'a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            yield
