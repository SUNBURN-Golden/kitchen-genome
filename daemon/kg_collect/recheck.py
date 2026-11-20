"""Durable monthly published-target inspections and their pending review work."""
import datetime as dt
import hashlib
import json
from zoneinfo import ZoneInfo
from kg_daemon.db import stable_id, utcnow
from . import discover

ACTIVE = ('queued', 'inspecting', 'review')


def iso(value):
    return value.astimezone(dt.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def ensure_schedule(conn, seed, now=None):
    now = now or dt.datetime.now(dt.timezone.utc)
    published = {str(r['id']) for r in seed.get('restaurants', []) + seed.get('kitchens', [])}
    n = 0
    for t in conn.execute("SELECT * FROM target WHERE kind='restaurant' AND origin='seed' AND status='active'").fetchall():
        sid = str(json.loads(t['attrs_json']).get('seed_id', ''))
        if sid not in published:
            continue
        stagger = int(hashlib.sha256(sid.encode()).hexdigest()[:8], 16) % 30
        n += conn.execute('INSERT OR IGNORE INTO restaurant_recheck(restaurant_id,target_id,next_due_at) VALUES (?,?,?)',
                          (sid, t['target_id'], iso(now + dt.timedelta(days=stagger)))).rowcount
    conn.commit()
    return n


def active_for_job(conn, job_id):
    return conn.execute("""SELECT a.* FROM restaurant_recheck_job l JOIN restaurant_recheck_attempt a USING(attempt_id)
                           WHERE l.job_id=? AND a.state IN ('queued','inspecting','review') ORDER BY a.queued_at LIMIT 1""", (job_id,)).fetchone()


def active_for_fetch(conn, fetch):
    """Only fetches made since admission belong to a coalesced job's attempt.

    Use the same boundary during extraction and reconciliation. Historical
    fetches remain ordinary work, including those awaiting extraction/replay.
    """
    attempt = active_for_job(conn, fetch['job_id']) if fetch['job_id'] else None
    return attempt if attempt and fetch['fetched_at'] >= attempt['queued_at'] else None


def admit(conn, seeds, *, now=None):
    """Queue each due target once across reruns."""
    now = now or dt.datetime.now(dt.timezone.utc)
    stamp, day = iso(now), now.astimezone(ZoneInfo('Asia/Seoul')).date().isoformat()
    conn.commit()
    try:
        conn.execute('BEGIN IMMEDIATE')
        rows = conn.execute("""SELECT r.*,t.name_ko,t.attrs_json FROM restaurant_recheck r JOIN target t USING(target_id)
                               WHERE r.active_attempt_id IS NULL AND r.next_due_at<=? AND t.status='active'
                               ORDER BY r.next_due_at,r.restaurant_id""", (stamp,)).fetchall()
        for r in rows:
            aid = stable_id('rck', r['restaurant_id'], r['next_due_at'])
            # Coalesce an ordinary pending source/status fetch when available.
            shared = conn.execute("""SELECT j.job_id FROM crawl_job j WHERE j.target_id=? AND j.state='queued'
                                     AND j.path IN ('official_chain','entity_followup')
                                     AND NOT EXISTS (SELECT 1 FROM restaurant_recheck_job l JOIN restaurant_recheck_attempt a USING(attempt_id)
                                       WHERE l.job_id=j.job_id AND a.state IN ('queued','inspecting','review'))
                                     ORDER BY CASE j.path WHEN 'official_chain' THEN 0 ELSE 1 END,j.created_at LIMIT 1""", (r['target_id'],)).fetchone()
            dedicated = int(shared is None)
            if shared:
                jid = shared['job_id']
            else:
                # Unique due-period queries are retired after completion, not requeued daily.
                query = r['name_ko'] + ' 폐업 이전 셰프 변경 ' + r['next_due_at'][:10]
                url = discover.search_url(seeds, query)
                discover.enqueue(conn, path='entity_followup', pool='refresh', url=url, query=query,
                                 target_id=r['target_id'], relevance=.7, caps=seeds['caps'])
                job = conn.execute("SELECT job_id FROM crawl_job WHERE path='entity_followup' AND url=?", (url,)).fetchone()
                if not job:
                    continue
                jid = job[0]
            if not conn.execute('SELECT 1 FROM crawl_job WHERE job_id=?', (jid,)).fetchone():
                continue
            conn.execute("INSERT INTO restaurant_recheck_attempt(attempt_id,restaurant_id,due_period,day,queued_at) VALUES (?,?,?,?,?)",
                         (aid,r['restaurant_id'],r['next_due_at'],day,stamp))
            conn.execute('INSERT INTO restaurant_recheck_job(attempt_id,job_id,dedicated_root) VALUES (?,?,?)', (aid,jid,dedicated))
            conn.execute('UPDATE restaurant_recheck SET active_attempt_id=?,last_attempted_at=? WHERE restaurant_id=?', (aid,stamp,r['restaurant_id']))
        conn.commit()
        return len(rows)
    except Exception:
        conn.rollback()
        raise


def link_children(conn, parent):
    attempt = active_for_job(conn, parent)
    if attempt:
        # Coalesced roots retain old child jobs. Only live work or activity since
        # admission belongs to this attempt; finished history cannot invalidate it.
        children = conn.execute("""SELECT j.job_id FROM crawl_job j WHERE j.parent_job_id=:parent
                                   AND (j.state='queued' OR j.created_at>=:queued_at OR j.finished_at>=:queued_at
                                     OR EXISTS (SELECT 1 FROM fetch_record f WHERE f.job_id=j.job_id
                                                AND f.fetched_at>=:queued_at))""",
                                {'parent':parent,'queued_at':attempt['queued_at']}).fetchall()
        for j in children:
            if not conn.execute('SELECT 1 FROM restaurant_recheck_job WHERE attempt_id=? AND job_id=?', (attempt['attempt_id'],j['job_id'])).fetchone():
                conn.execute('INSERT INTO restaurant_recheck_job(attempt_id,job_id) VALUES (?,?)', (attempt['attempt_id'],j['job_id']))


def finish(conn, aid, outcome, *, now=None, completed=False):
    now = now or dt.datetime.now(dt.timezone.utc)
    r = conn.execute('SELECT r.* FROM restaurant_recheck r WHERE active_attempt_id=?', (aid,)).fetchone()
    if not r:
        return
    failures = 0 if completed else r['failures']+1
    days = 30 if completed else (1 if failures == 1 else 3 if failures == 2 else 7)
    stamp = iso(now)
    conn.execute('UPDATE restaurant_recheck SET last_checked_at=CASE WHEN ? THEN ? ELSE last_checked_at END,next_due_at=?,active_attempt_id=NULL,failures=?,last_outcome=? WHERE restaurant_id=?',
                 (int(completed),stamp,iso(now+dt.timedelta(days=days)),failures,outcome,r['restaurant_id']))
    conn.execute('UPDATE restaurant_recheck_attempt SET state=?,reason=?,completed_at=? WHERE attempt_id=?', ('done' if completed else 'failed',outcome,stamp,aid))
    conn.execute("UPDATE ai_review_hold SET state='released' WHERE attempt_id=? AND state='held'", (aid,))
    conn.commit()


def reconcile(conn):
    """A check completes only after all linked inspections and reviews have completed."""
    for a in conn.execute("SELECT * FROM restaurant_recheck_attempt WHERE state IN ('queued','inspecting','review')").fetchall():
        aid = a['attempt_id']
        jobs = conn.execute('SELECT DISTINCT j.* FROM crawl_job j JOIN restaurant_recheck_job l USING(job_id) WHERE l.attempt_id=?', (aid,)).fetchall()
        if any(j['state'] in ('failed','blocked','dropped','skipped') for j in jobs):
            finish(conn,aid,'source_unavailable')
            continue
        if any(j['state'] == 'queued' for j in jobs):
            continue
        pending = conn.execute("""SELECT 1 FROM restaurant_recheck_job l JOIN review_queue q ON q.subject_id=l.extraction_id
                                  WHERE l.attempt_id=? AND q.reason='ai_inference' AND q.state='open' LIMIT 1""", (aid,)).fetchone()
        if pending:
            conn.execute("UPDATE restaurant_recheck_attempt SET state='review' WHERE attempt_id=?", (aid,))
            continue
        prose = conn.execute("""SELECT DISTINCT f.* FROM fetch_record f JOIN restaurant_recheck_job l USING(job_id)
                                WHERE l.attempt_id=? AND f.fetched_at>=? AND f.outcome='ok'
                                AND f.method NOT IN ('feed','sitemap')""", (aid,a['queued_at'])).fetchall()
        uninspected = [f for f in prose if not conn.execute(
            'SELECT 1 FROM restaurant_recheck_job WHERE attempt_id=? AND fetch_id=? AND extraction_id IS NULL',
            (aid,f['fetch_id'])).fetchone()]
        if uninspected:
            if any(f['extracted_at'] is None for f in uninspected):
                continue
            finish(conn,aid,'partial_inspection')
        elif prose:
            finish(conn,aid,'reviewed_or_no_change',completed=True)
        elif jobs and all(j['state']=='done' for j in jobs):
            finish(conn,aid,'no_inspectable_source')
    conn.commit()
