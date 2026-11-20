"""KST-day dispatch and URL counters for reporting."""
from kg_ai.budget import kst_day


def record_dispatch(conn, url, *, robots=False, day=None):
    day = day or kst_day()
    if not robots:
        conn.execute('INSERT OR IGNORE INTO web_daily_url(day,url) VALUES (?,?)', (day, url))
    conn.execute("""INSERT INTO web_daily_fetch(day,attempts,robots) VALUES (?,?,?)
        ON CONFLICT(day) DO UPDATE SET attempts=attempts+excluded.attempts,robots=robots+excluded.robots""",
        (day, int(not robots), int(robots)))
    conn.commit()
