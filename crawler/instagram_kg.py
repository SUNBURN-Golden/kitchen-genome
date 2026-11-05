"""Dedicated @kitchen.genome pending-only collector. No alternate identity or block recovery."""
import argparse
import contextlib
import datetime as dt
import fcntl
import io
import json
import os
from pathlib import Path
import re
import signal
import sys
import time
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'daemon'))
from kg_ai.budget import Budget
from kg_ai.deepseek import Client, DeepSeekError
from kg_daemon.claims import queue_review
from kg_daemon.db import DEFAULT_DB, connect, new_id, stable_id, utcnow
from kg_daemon.runner import CostCapExceeded, CostMeter, DEFAULT_CONFIG
from privacy_filter import sanitize_before_store
from instagram import KW

LOGIN = 'kitchen.genome'
SESSION = Path('/home/box/.secrets/kg_ig_kitchen_genome.session')
FLAG = ROOT / 'crawler/out/ig_kg_blocked.flag'
OUT = ROOT / 'crawler/out/ig_kg'
KST = ZoneInfo('Asia/Seoul')
DELAY, CAP, POSTS, FOLLOWS = 8, 25, 6, 10


class Stop(Exception):
    pass


class Skip(Exception):
    pass


def now_kst():
    return dt.datetime.now(KST)


def in_window(now):
    t = now.astimezone(KST).time()
    return dt.time(6, 45) <= t < dt.time(8)


def reason(exc):
    text = (type(exc).__name__ + ' ' + str(exc)).lower()
    for word, code in [('checkpoint', 'checkpoint'), ('challenge', 'challenge'), ('429', 'http_429'),
                       ('twofactor', 'two_factor'), ('two-factor', 'two_factor'), ('login', 'login_failed'),
                       ('403', 'access_denied'), ('401', 'access_denied'), ('timeout', 'transport_unknown')]:
        if word in text:
            return code
    return 'transport_unknown'


def atomic_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + '.tmp')
    with open(tmp, 'w', encoding='utf-8') as f:
        os.chmod(tmp, 0o600)
        json.dump(data, f, ensure_ascii=False)
    os.replace(tmp, path)


class Guard:
    """One dispatch gate for EVERY HTTP request, including login, pagination and follow."""
    def __init__(self, conn, flag=FLAG, now=now_kst, sleep=time.sleep):
        self.conn, self.flag, self.now, self.sleep = conn, Path(flag), now, sleep
        self.day = now().astimezone(KST).date().isoformat()
        self.requests = 0
        self.active = False

    def check(self):
        if self.flag.exists():
            raise Stop('blocked_flag')
        if not in_window(self.now()) or self.now().astimezone(KST).date().isoformat() != self.day:
            raise Skip('outside_window')
        row = self.conn.execute('SELECT * FROM ig_account_state WHERE account=?', (LOGIN,)).fetchone()
        if row and row['stopped_day'] == self.day and not self.active:
            raise Stop('same_day_stop')

    def block(self, code):
        self.conn.rollback()
        atomic_json(self.flag, {'at': self.now().isoformat(), 'reason': code, 'account': LOGIN})
        self.conn.execute('INSERT INTO ig_account_state(account,stopped_day) VALUES (?,?) ON CONFLICT(account) DO UPDATE SET stopped_day=excluded.stopped_day', (LOGIN, self.day))
        self.conn.commit()

    def dispatch(self, send):
        self.check()
        row = self.conn.execute('SELECT last_dispatch FROM ig_account_state WHERE account=?', (LOGIN,)).fetchone()
        if row and row[0] is not None:
            wait = DELAY - (self.now().timestamp() - row[0])
            if wait > 0:
                self.sleep(wait)
        self.check()
        # A killed process leaves stopped_day behind, preventing automatic same-day relogin.
        self.conn.execute('INSERT INTO ig_account_state(account,last_dispatch,stopped_day) VALUES (?,?,?) ON CONFLICT(account) DO UPDATE SET last_dispatch=excluded.last_dispatch,stopped_day=excluded.stopped_day',
                          (LOGIN, self.now().timestamp(), self.day))
        self.conn.commit()
        self.active = True
        self.requests += 1
        try:
            response = send()
            if hasattr(response, 'status_code'):
                code = response.status_code
                # Inspect only for access-control signals; never log or persist response bodies.
                body = response.text[:20000].lower()
                if code in (401, 403, 429) or code >= 500 or any(x in body for x in ('checkpoint_required', 'challenge_required', 'login_required', 'feedback_required', 'two_factor_required')):
                    raise Stop('http_429' if code == 429 else ('checkpoint' if 'checkpoint' in body else 'challenge'))
            return response
        except Skip:
            raise
        except Exception as exc:
            code = str(exc) if isinstance(exc, Stop) else reason(exc)
            self.block(code)
            raise Stop(code) from None

    def reserve_handle(self, handle):
        self.check()
        n = self.conn.execute('SELECT COUNT(*) FROM ig_handle_day WHERE day=?', (self.day,)).fetchone()[0]
        if n >= CAP:
            return False
        cur = self.conn.execute('INSERT OR IGNORE INTO ig_handle_day(day,handle) VALUES (?,?)', (self.day, handle))
        self.conn.commit()
        return bool(cur.rowcount)

    def caption_slot(self, handle):
        cur = self.conn.execute('UPDATE ig_handle_day SET captions=captions+1 WHERE day=? AND handle=? AND captions<?', (self.day, handle, POSTS))
        self.conn.commit()
        return bool(cur.rowcount)

    def follow_slot(self, handle):
        if self.conn.execute('SELECT COUNT(*) FROM ig_follow_day WHERE day=?', (self.day,)).fetchone()[0] >= FOLLOWS:
            return False
        cur = self.conn.execute('INSERT OR IGNORE INTO ig_follow_day(day,handle) VALUES (?,?)', (self.day, handle))
        self.conn.commit()
        return bool(cur.rowcount)

    def finish(self):
        self.conn.execute('UPDATE ig_account_state SET stopped_day=NULL WHERE account=?', (LOGIN,))
        self.conn.commit()


class LoaderBackend:
    """Instaloader read transport. requests.Session.send covers temporary login sessions too.

    Scoped process-wide patch is held under the account's exclusive file lock. No retries,
    redirects or proxies. Following requires a separate injected backend and stays off here.
    """
    follow_available = False

    def __init__(self, guard):
        import instaloader as il
        self.il, self.guard = il, guard

    def __enter__(self):
        import requests
        self.original = requests.sessions.Session.send
        original, guard = self.original, self.guard

        def send(session, request, **kwargs):
            if kwargs.get('proxies') or not re.fullmatch(r'https://(?:[A-Za-z0-9-]+\.)*instagram\.com(?::443)?/.*', request.url):
                raise Stop('unsupported_transport')
            kwargs['allow_redirects'] = False
            response = guard.dispatch(lambda: original(session, request, **kwargs))
            if 300 <= response.status_code < 400:
                raise Stop('redirect_denied')
            return response
        self.requests = requests
        requests.sessions.Session.send = send
        try:
            il = self.il
            class NoRetry(il.RateController):
                def handle_429(self, query_type):
                    raise Stop('http_429')
            self.loader = il.Instaloader(download_pictures=False, download_videos=False, download_video_thumbnails=False,
                                        download_geotags=False, download_comments=False, save_metadata=False,
                                        max_connection_attempts=1, quiet=True, rate_controller=lambda ctx: NoRetry(ctx))
        except Exception:
            requests.sessions.Session.send = self.original
            raise
        return self

    def __exit__(self, *args):
        self.requests.sessions.Session.send = self.original

    def login(self, session, password):
        if Path(session).exists():
            self.loader.load_session_from_file(LOGIN, str(session))
        else:
            self.loader.login(LOGIN, password)
        if self.loader.test_login() != LOGIN:
            raise Stop('invalid_session_identity')
        if not Path(session).exists():
            Path(session).parent.mkdir(parents=True, mode=0o700, exist_ok=True)
            old = os.umask(0o077)
            try:
                self.loader.save_session_to_file(str(session))
            finally:
                os.umask(old)
        os.chmod(session, 0o600)

    def profile(self, handle):
        return self.il.Profile.from_username(self.loader.context, handle)

    def posts(self, profile):
        return iter(profile.get_posts())


def model_call(client, task, instruction, data, validate, default):
    if client is None or client.status != 'enabled':
        return default
    try:
        return client.call(task, instruction, data, validate)
    except (DeepSeekError, CostCapExceeded):
        if client:
            client.stopped = client.stopped or 'cost_cap'
        return default


def rank_handles(client, handles):
    known = {r['handle']: r for r in handles}
    def validate(obj):
        if set(obj) != {'handles'} or not isinstance(obj['handles'], list) or len(obj['handles']) > CAP or len(set(obj['handles'])) != len(obj['handles']) or any(h not in known for h in obj['handles']):
            raise ValueError()
        return obj['handles']
    ranked = model_call(client, 'ig-plan-1', 'Return JSON {"handles":["known handle"]}. Prioritize public restaurant/brand handles for culinary gaps. Max 25. No new handles.',
                        {'handles': handles[:100]}, validate, [])
    order = ranked + [h for h in known if h not in ranked]
    return [known[h] for h in order]


def scan_text(conn, client, handle, target, url, date, text, day=None):
    text, _ = sanitize_before_store((text or '')[:6000])
    if not text:
        return []
    findings = []
    for kind, words in KW.items():
        word = next((w for w in words if w in text), None)
        if word:
            start = max(0, text.index(word) - 12)
            findings.append({'kind': kind, 'excerpt': text[start:start+40], 'asserted_by': 'source', 'extractor': 'rule:ig_kg'})
    def validate(obj):
        if set(obj) != {'findings', 'follow'} or obj['follow'] not in ('follow', 'skip', 'review') or not isinstance(obj['findings'], list) or len(obj['findings']) > 4:
            raise ValueError()
        out = []
        for f in obj['findings']:
            if set(f) != {'kind', 'excerpt'} or f['kind'] not in KW or not isinstance(f['excerpt'], str) or not 1 <= len(f['excerpt']) <= 40 or f['excerpt'] not in text:
                raise ValueError()
            out.append(f)
        return {'findings': out, 'follow': obj['follow']}
    result = model_call(client, 'ig-extract-1', 'Return JSON {"findings":[{"kind":"closure|rename|chef_move|opening","excerpt":"exact substring <=40 Unicode characters"}],"follow":"follow|skip|review"}. Culinary facts only. Never infer unsupported events. Follow public restaurant/brand only.',
                        {'handle': handle, 'target_id': target, 'text': text, 'post_date': date}, validate, {'findings': [], 'follow': 'skip'})
    if result['follow'] != 'skip':
        recommendation_id = stable_id('rev', LOGIN, handle, url, 'follow', result['follow'])
        queue_review(conn, 'instagram_kg_follow_pending', 'ig_kg_follow', recommendation_id,
                     {'pending_only': True, 'collector_account': LOGIN, 'handle': handle, 'url': url,
                      'follow_recommendation': result['follow'], 'asserted_by': 'ai_inference',
                      'model_meta': client.last_meta}, dedupe_key='ig_kg:follow:' + recommendation_id)
    for f in result['findings']:
        findings.append({**f, 'asserted_by': 'ai_inference', 'extractor': 'deepseek:ig_kg', 'model_meta': client.last_meta})
    out = []
    for f in findings:
        excerpt, _ = sanitize_before_store(f['excerpt'])
        if not excerpt:
            continue
        rid = stable_id('rev', LOGIN, target, url, f['kind'], f['extractor'], excerpt[:40])
        rec = {**f, 'excerpt': excerpt[:40], 'instagram': True, 'collector_account': LOGIN, 'pending_only': True,
               'target_id': target, 'handle': handle, 'url': url, 'date': date, 'review_id': stable_id('rev', 'ig_kg:' + rid),
               'collected_at': utcnow(), 'collection_day': day or now_kst().date().isoformat(), 'follow_recommendation': result['follow']}
        queue_review(conn, 'instagram_kg_pending', 'ig_kg', rid, rec, dedupe_key='ig_kg:' + rid)
        out.append(rec)
    conn.commit()
    return out, result['follow']


def purge(conn, out, now):
    cutoff = (now.astimezone(KST).date() - dt.timedelta(days=30)).isoformat()
    for row in conn.execute("SELECT review_id,payload_json FROM review_queue WHERE subject_type='ig_kg'").fetchall():
        payload = json.loads(row['payload_json'])
        if payload.get('collection_day', payload.get('collected_at', '')[:10]) <= cutoff:
            payload.pop('excerpt', None)
            conn.execute('UPDATE review_queue SET payload_json=? WHERE review_id=?', (json.dumps(payload), row['review_id']))
    conn.execute("UPDATE ai_request SET result_json=NULL WHERE task='ig-extract-1' AND day<=?", (cutoff,))
    for path in Path(out).glob('*.json'):
        if path.stem <= cutoff.replace('-', ''):
            path.unlink()
    conn.commit()


def collect(conn, handles, backend_factory=LoaderBackend, *, client=None, flag=FLAG, out=OUT, session=SESSION,
            now=now_kst, sleep=time.sleep, follow=False, password=None):
    guard = Guard(conn, flag, now, sleep)
    findings, checked, followed = [], 0, 0
    ai = lambda: client.status if client else 'disabled'
    purge(conn, out, now())
    try:
        guard.check()
        if not Path(session).exists() and not password:
            raise Skip('missing_password')
        # Only restaurant/brand seed records enter this allowlist. Never harvest profiles.
        handles = [r for r in handles if r.get('handle') != LOGIN and re.fullmatch(r'[A-Za-z0-9_.]{1,30}', r.get('handle', ''))]
        attempted = {r[0] for r in conn.execute('SELECT handle FROM ig_handle_day WHERE day=?', (guard.day,))}
        handles = [r for r in handles if r['handle'] not in attempted]
        recent = {r['handle']: r['last_day'] for r in conn.execute('SELECT handle,MAX(day) last_day FROM ig_handle_day GROUP BY handle')}
        handles.sort(key=lambda r: (recent.get(r['handle'], ''), r['handle']))
        if not handles or len(attempted) >= CAP:
            raise Skip('daily_cap_or_no_handles')
        for r in handles[:100]:
            row = conn.execute("SELECT target_id FROM target WHERE json_extract(attrs_json,'$.seed_id')=? AND kind='restaurant' LIMIT 1", (r['target_id'],)).fetchone()
            r['gaps'] = [x[0] for x in conn.execute("SELECT field FROM gap WHERE target_id=? AND state='open' LIMIT 10", (row[0],))] if row else []
            r['last_checked'] = recent.get(r['handle'])
        handles = rank_handles(client, handles)
        with backend_factory(guard) as backend, contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            backend.login(session, password)
            for r in handles:
                h = r['handle']
                if not guard.reserve_handle(h):
                    continue
                profile = backend.profile(h)
                checked += 1
                if profile.is_private:
                    continue
                recs = scan_text(conn, client, h, r['target_id'], f'https://www.instagram.com/{h}/', None, profile.biography, guard.day)
                decision = 'skip'
                if recs:
                    fs, decision = recs
                    findings.extend(fs)
                posts = backend.posts(profile)
                for _ in range(POSTS):
                    if not guard.caption_slot(h):
                        break
                    try:
                        post = next(posts)
                    except StopIteration:
                        break
                    recs = scan_text(conn, client, h, r['target_id'], f'https://www.instagram.com/p/{post.shortcode}/', post.date_utc.date().isoformat(), post.caption, guard.day)
                    if recs:
                        fs, post_decision = recs
                        findings.extend(fs)
                        if post_decision == 'follow':
                            decision = 'follow'
                if decision == 'follow' and follow and backend.follow_available and not getattr(profile, 'followed_by_viewer', True) and guard.follow_slot(h):
                    backend.follow(h)
                    followed += 1
                conn.execute('UPDATE ig_handle_day SET complete=1 WHERE day=? AND handle=?', (guard.day, h))
                conn.commit()
        guard.finish()
        line, rc = f'IG_OK account={LOGIN} handles={checked} findings={len(findings)} follows={followed} ai={ai()} pending_only=1', 0
        if follow and not backend.follow_available:
            line += ' reason=follows_disabled_backend_unavailable'
    except ImportError:
        if guard.requests:
            guard.block('transport_unknown')
            line, rc = f'IG_BLOCKED account={LOGIN} reason=transport_unknown ai={ai()}', 2
        else:
            line, rc = f'IG_SKIPPED account={LOGIN} reason=dependency_unavailable ai={ai()}', 0
    except Skip as exc:
        if guard.active:
            guard.finish()
        line, rc = f'IG_SKIPPED account={LOGIN} reason={exc} ai={ai()}', 0
    except Exception as exc:
        code = str(exc) if isinstance(exc, Stop) else reason(exc)
        guard.block(code)
        line, rc = f'IG_BLOCKED account={LOGIN} reason={code} flag=crawler/out/ig_kg_blocked.flag ai={ai()}', 2
    finally:
        # Rebuild from the private review inbox so reruns never overwrite earlier partial findings.
        findings = [json.loads(r[0]) for r in conn.execute("SELECT payload_json FROM review_queue WHERE subject_type='ig_kg'")]
        findings = [f for f in findings if f.get('collection_day') == guard.day]
        atomic_json(Path(out) / (guard.day.replace('-', '') + '.json'), {'day': guard.day, 'pending_only': True, 'findings': findings})
    if client is not None and hasattr(client, 'key') and not client.key:
        line += ' ai_reason=missing_key'
    elif client is not None and client.stopped:
        line += ' ai_reason=' + client.stopped
    return rc, line


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument('--db', default=DEFAULT_DB)
    ap.add_argument('--cost-cap', type=float, default=20)
    ap.add_argument('--deepseek', action='store_true')
    ap.add_argument('--mark-timeout', action='store_true')
    args = ap.parse_args(argv)
    if not Path(args.db).is_file():
        print(f'IG_SKIPPED account={LOGIN} reason=missing_db'); return 0
    conn = connect(args.db)
    try:
        conn.execute('SELECT 1 FROM ig_account_state LIMIT 1')
    except Exception:
        print(f'IG_SKIPPED account={LOGIN} reason=missing_migration'); return 0
    guard = Guard(conn)
    if args.mark_timeout:
        guard.block('timeout')
        print(f'IG_FAILED account={LOGIN} reason=timeout'); return 2
    lockpath = Path(args.db).parent / 'ig_kitchen_genome.lock'
    with open(lockpath, 'a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print(f'IG_SKIPPED account={LOGIN} reason=locked'); return 0
        run = new_id('run')
        conn.execute('INSERT INTO daemon_run(run_id,daemon,status,trigger,cost_cap_units,started_at) VALUES (?,?,?,?,?,?)',
                     (run, 'instagram_kg', 'running', 'daily', args.cost_cap, utcnow()))
        conn.commit()
        meter = CostMeter(conn, run, args.cost_cap, DEFAULT_CONFIG['unit_costs'])
        meter.stage = 'instagram_kg'
        client = Client(Budget(meter, os.environ.get('KG_AI_DAILY_COST_CAP', '20'))) if args.deepseek else None
        seed = json.loads((ROOT / 'data/seed.json').read_text())
        handles = [{'handle': r['instagram'].lstrip('@'), 'target_id': r['id'], 'name': r['name']} for r in seed['restaurants'] if r.get('instagram') and not r.get('closed_at')]
        def terminate(signum, frame):
            raise Stop('timeout')
        signal.signal(signal.SIGTERM, terminate)
        rc, line = collect(conn, handles, client=client, follow=os.environ.get('KG_IG_FOLLOW', '0') == '1', password=os.environ.get('KITCHENGENOME_IG_PASSWORD'))
        conn.execute('UPDATE daemon_run SET status=?,finished_at=? WHERE run_id=?', ('failed' if rc else 'succeeded', utcnow(), run))
        conn.commit()
        print(line)
        return rc


if __name__ == '__main__':
    sys.exit(main())
