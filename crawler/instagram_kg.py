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
from urllib.parse import parse_qs, urlsplit
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'daemon'))
from kg_ai.budget import Budget
from kg_ai.deepseek import Client, DeepSeekError
from kg_daemon.claims import queue_review
from kg_daemon.db import DEFAULT_DB, connect, new_id, stable_id, utcnow
from kg_daemon.registry import IG_FLAG
from kg_daemon.runner import CostMeter, DEFAULT_CONFIG
from kg_collect.net import UA
from privacy_filter import sanitize_before_store
from ig_config import DISABLED_REASON, instagram_enabled

LOGIN = 'kitchen.genome'
SESSION = Path('/home/box/.secrets/kg_ig_kitchen_genome.session')
FLAG = Path(IG_FLAG)
SECONDARY_FLAG = 'ig_kg_blocked.flag'
OUT = ROOT / 'crawler/out/ig_kg'
KST = ZoneInfo('Asia/Seoul')
LOOKBACK = dt.timedelta(days=30)
KW = {
    'closure': ['폐업', '영업 종료', '영업을 종료', '마지막 영업', '문을 닫'],
    'rename': ['새 이름', '상호 변경', '이름이 바뀝', '리브랜딩'],
    'chef_move': ['헤드셰프', '합류', '새 셰프', '떠나'],
    'opening': ['오픈', '개업', '그랜드 오픈'],
}
BLOCK_CODES = {'checkpoint', 'challenge', 'two_factor', 'http_429', 'login_failed', 'access_denied', 'invalid_session'}


class Stop(Exception):
    pass


class TransientStop(Stop):
    """An interrupted read with its last committed checkpoint intact."""
    pass


class Skip(Exception):
    pass


def now_kst():
    return dt.datetime.now(KST)


def reason(exc):
    if isinstance(exc, Stop):
        return str(exc) if str(exc) in BLOCK_CODES or str(exc) == 'timeout' else 'transport_unknown'
    if isinstance(exc, (TimeoutError, OSError)):
        return 'transport_unknown'
    return block_code(type(exc).__name__ + ' ' + str(exc)) or 'transport_unknown'


def block_code(text):
    text = text.lower()
    for pattern, code in (
        (r'\b429\b', 'http_429'),
        (r'checkpoint', 'checkpoint'),
        (r'two[\s_-]*factor|\b2fa\b', 'two_factor'),
        (r'challenge|captcha|feedback_required|suspicious_login|email_code', 'challenge'),
        (r'login[_ ]?required|loginexception|loginfailed|badcredentials', 'login_failed'),
        (r'has blocked you|blocked you', 'access_denied'),
        (r'\b(401|403)\b', 'access_denied'),
    ):
        if re.search(pattern, text):
            return code
    return None


def atomic_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + '.tmp')
    with open(tmp, 'w', encoding='utf-8') as f:
        os.chmod(tmp, 0o600)
        json.dump(data, f, ensure_ascii=False)
    os.replace(tmp, path)


class Guard:
    """Check both pause files before every saved-session dispatch."""

    def __init__(self, conn, flag=FLAG, now=now_kst):
        self.conn, self.flag, self.now = conn, Path(flag), now
        self.secondary = self.flag.with_name(SECONDARY_FLAG)
        self.day = now().astimezone(KST).date().isoformat()
        self.requests = 0
        self.stopped = False

    def check(self):
        if self.stopped:
            raise Skip('blocked_for_run')
        if os.path.lexists(self.flag) or os.path.lexists(self.secondary):
            raise Skip('blocked_flag')

    def block(self, code):
        self.conn.rollback()
        if self.stopped or code not in BLOCK_CODES:
            return
        self.stopped = True
        if not os.path.lexists(self.secondary):
            self.flag.parent.mkdir(parents=True, exist_ok=True)
            try:
                fd = os.open(self.flag, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                pass
            else:
                with os.fdopen(fd, 'w', encoding='utf-8') as f:
                    json.dump({'at': self.now().isoformat(), 'reason': code, 'account': LOGIN}, f)
        self.conn.execute('INSERT INTO ig_account_state(account,stopped_day) VALUES (?,?) ON CONFLICT(account) DO UPDATE SET stopped_day=excluded.stopped_day',
                          (LOGIN, self.day))
        self.conn.commit()

    def dispatch(self, send):
        self.check()
        self.requests += 1
        try:
            response = send()
            if hasattr(response, 'status_code'):
                status = response.status_code
                # Provider bodies are inspected in memory and discarded.
                if status == 429:
                    code = 'http_429'
                elif status in (401, 403):
                    code = 'access_denied'
                else:
                    body = response.text[:20000]
                    messages = re.findall(r'"(?:message|error_type|error_message)"\s*:\s*"([^"]+)"', body)
                    if re.search(r'"two_factor_required"\s*:\s*true', body):
                        messages.append('two_factor')
                    if re.search(r'"checkpoint_url"\s*:\s*"', body):
                        messages.append('checkpoint')
                    if re.search(r'"challenge"\s*:\s*["{]', body):
                        messages.append('challenge')
                    messages.extend(re.findall(r'<(?:title|h1)\b[^>]*>([^<]+)', body, re.I))
                    code = block_code(' '.join(messages))
                if code:
                    raise Stop(code)
                if status >= 500:
                    raise TransientStop('transport_unknown')
            return response
        except (Skip, TransientStop):
            raise
        except Exception as exc:
            code = reason(exc)
            if code in BLOCK_CODES:
                self.block(code)
                raise Stop(code) from None
            raise TransientStop(code) from None


class LoaderBackend:
    """Guard saved-session reads, including Instaloader's temporary request sessions."""

    POST_READ_QUERIES = {'28975909992013618', '7898261790222653'}

    def __init__(self, guard):
        import instaloader as il
        self.il, self.guard = il, guard

    def __enter__(self):
        import requests
        self.original = requests.sessions.Session.send
        original, guard = self.original, self.guard

        def send(session, request, **kwargs):
            if kwargs.get('proxies') or not re.fullmatch(r'https://(?:[A-Za-z0-9-]+\.)*instagram\.com(?::443)?/.*', request.url):
                raise TransientStop('unsupported_transport')
            path = urlsplit(request.url).path
            if request.method == 'POST':
                # Recent Instaloader versions use a read-only timeline GraphQL query over POST.
                form = parse_qs(request.body or '') if isinstance(request.body, str) else {}
                if path != '/graphql/query' or len(form.get('doc_id', ())) != 1 or form['doc_id'][0] not in self.POST_READ_QUERIES:
                    raise TransientStop('unsupported_transport')
            elif request.method != 'GET':
                raise TransientStop('unsupported_transport')
            kwargs['allow_redirects'] = False
            request.headers['User-Agent'] = UA

            def read():
                response = original(session, request, **kwargs)
                if 300 <= response.status_code < 400:
                    loc = response.headers.get('location', '')
                    code = block_code(loc) or ('login_failed' if '/login' in loc.lower() else None)
                    if code:
                        raise Stop(code)
                    raise TransientStop('redirect_denied')
                return response

            return guard.dispatch(read)
        self.requests = requests
        requests.sessions.Session.send = send
        try:
            il = self.il
            class NoRetry(il.RateController):
                def wait_before_query(self, query_type):
                    pass

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

    def load_session(self, session):
        if not instagram_enabled():
            raise Skip(DISABLED_REASON)
        if not Path(session).is_file():
            raise Skip('missing_session')
        self.loader.load_session_from_file(LOGIN, str(session))
        if self.loader.test_login() != LOGIN:
            raise Stop('invalid_session')

    def profile(self, handle):
        return self.il.Profile.from_username(self.loader.context, handle)

    def posts(self, profile):
        return iter(profile.get_posts())


def model_call(client, task, instruction, data, validate, default):
    if client is None or client.status != 'enabled':
        return default
    try:
        return client.call(task, instruction, data, validate, max_tokens=2048)
    except DeepSeekError:
        # Real failures already set client.stopped; a purged cache entry or one bad task
        # must not disable model calls for the rest of the run.
        return default


def rank_handles(client, handles):
    known = {r['handle']: r for r in handles[:100]}
    def validate(obj):
        if set(obj) != {'handles'} or not isinstance(obj['handles'], list) or len(obj['handles']) > len(known) or len(set(obj['handles'])) != len(obj['handles']) or any(h not in known for h in obj['handles']):
            raise ValueError()
        return obj['handles']
    ranked = model_call(client, 'ig-plan-1', 'Return JSON {"handles":["known handle"]}. Prioritize public restaurant/brand handles for culinary gaps. No new handles.',
                        {'handles': handles[:100]}, validate, [])
    selected = set(ranked)
    return [known[h] for h in ranked] + [r for r in handles if r['handle'] not in selected]


def scan_text(conn, client, handle, target, url, date, text, day=None, *, commit=True):
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
        if not isinstance(obj, dict) or obj.get('follow') not in ('follow', 'skip', 'review') or not isinstance(obj.get('findings'), list) or len(obj['findings']) > 4:
            raise ValueError()
        out = []
        for f in obj['findings']:
            if not isinstance(f, dict) or f.get('kind') not in KW or not isinstance(f.get('excerpt'), str) or not 1 <= len(f['excerpt']) <= 40 or f['excerpt'] not in text:
                raise ValueError()
            out.append({'kind': f['kind'], 'excerpt': f['excerpt']})
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
    if commit:
        conn.commit()
    return out, result['follow']


def purge(conn, out, now):
    cutoff = (now.astimezone(KST).date() - LOOKBACK).isoformat()
    for row in conn.execute("SELECT review_id,payload_json FROM review_queue WHERE subject_type='ig_kg'").fetchall():
        payload = json.loads(row['payload_json'])
        if payload.get('collection_day', payload.get('collected_at', '')[:10]) <= cutoff:
            payload.pop('excerpt', None)
            conn.execute('UPDATE review_queue SET payload_json=? WHERE review_id=?', (json.dumps(payload), row['review_id']))
    # Mark purged cache entries explicitly. They stay in the audit/accounting trail, but a
    # repeat caption must not be mistaken for an unknown prior attempt that disables the run.
    conn.execute("UPDATE ai_request SET result_json=NULL, state='expired' WHERE task='ig-extract-1' AND day<=? AND state='done'", (cutoff,))
    conn.execute('DELETE FROM ig_post_checkpoint WHERE posted_at<?', (cutoff,))
    for path in Path(out).glob('*.json'):
        if path.stem <= cutoff.replace('-', ''):
            path.unlink()
    conn.commit()


def read_posts(conn, backend, profile, client, handle, target, day, cutoff):
    row = conn.execute('SELECT shortcode,posted_at FROM ig_post_watermark WHERE handle=?', (handle,)).fetchone()
    completed = {tuple(r) for r in conn.execute('SELECT posted_at,shortcode FROM ig_post_checkpoint WHERE handle=?', (handle,))}
    newest = max(completed, default=None)
    for post in backend.posts(profile):
        posted = post.date_utc
        if posted.tzinfo is not None:
            posted = posted.astimezone(dt.timezone.utc).replace(tzinfo=None)
        key = posted.isoformat(timespec='seconds'), post.shortcode
        if posted < cutoff or (row and (key[0] < row['posted_at'] or key[1] == row['shortcode'])):
            if getattr(post, 'is_pinned', False):
                continue
            break
        if key in completed:
            continue
        scan_text(conn, client, handle, target, f'https://www.instagram.com/p/{post.shortcode}/',
                  posted.date().isoformat(), post.caption, day, commit=False)
        conn.execute('INSERT OR IGNORE INTO ig_post_checkpoint VALUES (?,?,?)', (handle, key[1], key[0]))
        conn.commit()
        if newest is None or key[0] > newest[0]:
            newest = key
    if newest:
        conn.execute("""INSERT INTO ig_post_watermark VALUES (?,?,?) ON CONFLICT(handle) DO UPDATE
            SET shortcode=excluded.shortcode, posted_at=excluded.posted_at""", (handle, newest[1], newest[0]))
    conn.execute('DELETE FROM ig_post_checkpoint WHERE handle=?', (handle,))


def collect(conn, handles, backend_factory=LoaderBackend, *, client=None, flag=FLAG, out=OUT, session=SESSION, now=now_kst):
    guard = Guard(conn, flag, now)
    checked = 0
    ai = lambda: client.status if client else 'disabled'
    purge(conn, out, now())
    try:
        guard.check()
        if not instagram_enabled():
            raise Skip(DISABLED_REASON)
        if not Path(session).is_file():
            raise Skip('missing_session')
        # Only restaurant/brand seed records enter this allowlist.
        valid = {}
        for row in handles:
            h = (row.get('handle') or '').lower()
            if h != LOGIN and re.fullmatch(r'[a-z0-9_.]{1,30}', h):
                valid.setdefault(h, {**row, 'handle': h})
        attempted = {r[0].lower() for r in conn.execute('SELECT handle FROM ig_handle_day WHERE day=? AND complete=1', (guard.day,))}
        recent = {r['handle']: r['last_day'] for r in conn.execute('SELECT handle,MAX(day) last_day FROM ig_handle_day GROUP BY handle')}
        handles = sorted((r for h, r in valid.items() if h not in attempted), key=lambda r: (recent.get(r['handle'], ''), r['handle']))
        if not handles:
            raise Skip('no_handles')
        for r in handles[:100]:
            row = conn.execute("SELECT target_id FROM target WHERE json_extract(attrs_json,'$.seed_id')=? AND kind='restaurant' LIMIT 1", (r['target_id'],)).fetchone()
            r['gaps'] = [x[0] for x in conn.execute("SELECT field FROM gap WHERE target_id=? AND state='open' LIMIT 10", (row[0],))] if row else []
            r['last_checked'] = recent.get(r['handle'])
        handles = rank_handles(client, handles)
        cutoff = (now().astimezone(dt.timezone.utc) - LOOKBACK).replace(tzinfo=None)
        with backend_factory(guard) as backend, contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            backend.load_session(session)
            for r in handles:
                guard.check()
                h = r['handle']
                profile = backend.profile(h)
                checked += 1
                if not profile.is_private:
                    scan_text(conn, client, h, r['target_id'], f'https://www.instagram.com/{h}/', None, profile.biography, guard.day)
                    read_posts(conn, backend, profile, client, h, r['target_id'], guard.day, cutoff)
                conn.execute("""INSERT INTO ig_handle_day(day,handle,complete) VALUES (?,?,1)
                    ON CONFLICT(day,handle) DO UPDATE SET complete=1""", (guard.day, h))
                conn.commit()
        line, rc = f'IG_OK account={LOGIN} handles={checked} ai={ai()} pending_only=1', 0
    except TransientStop as exc:
        conn.rollback()
        line, rc = f'IG_FAILED account={LOGIN} reason={exc} ai={ai()}', 1
    except ImportError:
        conn.rollback()
        if guard.requests:
            line, rc = f'IG_FAILED account={LOGIN} reason=transport_unknown ai={ai()}', 1
        else:
            line, rc = f'IG_SKIPPED account={LOGIN} reason=dependency_unavailable ai={ai()}', 0
    except Skip as exc:
        line, rc = f'IG_SKIPPED account={LOGIN} reason={exc} ai={ai()}', 0
    except Exception as exc:
        code = reason(exc)
        if code in BLOCK_CODES:
            guard.block(code)
            line, rc = f'IG_BLOCKED account={LOGIN} reason={code} flag=crawler/out/ig_blocked.flag ai={ai()}', 2
        else:
            conn.rollback()
            line, rc = f'IG_FAILED account={LOGIN} reason={code} ai={ai()}', 1
    finally:
        # Rebuild from the private review inbox so reruns never overwrite earlier partial findings.
        findings = [json.loads(r[0]) for r in conn.execute("SELECT payload_json FROM review_queue WHERE subject_type='ig_kg'")]
        findings = [f for f in findings if f.get('collection_day') == guard.day]
        atomic_json(Path(out) / (guard.day.replace('-', '') + '.json'), {'day': guard.day, 'pending_only': True, 'findings': findings})
    if line.startswith('IG_OK '):
        line += f' findings={len(findings)}'
    if client is not None and hasattr(client, 'key') and not client.key:
        line += ' ai_reason=missing_key'
    elif client is not None and client.stopped:
        line += ' ai_reason=' + client.stopped
    return rc, line


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument('--db', default=DEFAULT_DB)
    ap.add_argument('--deepseek', action='store_true')
    ap.add_argument('--mark-timeout', action='store_true')
    args = ap.parse_args(argv)
    if not instagram_enabled():
        print(f'IG_SKIPPED account={LOGIN} reason={DISABLED_REASON}'); return 0
    if args.mark_timeout:
        print(f'IG_FAILED account={LOGIN} reason=timeout'); return 1
    if not Path(args.db).is_file():
        print(f'IG_SKIPPED account={LOGIN} reason=missing_db'); return 0
    conn = connect(args.db)
    try:
        conn.execute('SELECT 1 FROM ig_post_watermark LIMIT 1')
    except Exception:
        print(f'IG_SKIPPED account={LOGIN} reason=missing_migration'); return 0
    lockpath = Path(args.db).parent / 'ig_kitchen_genome.lock'
    with open(lockpath, 'a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print(f'IG_SKIPPED account={LOGIN} reason=locked'); return 0
        run = new_id('run')
        conn.execute('INSERT INTO daemon_run(run_id,daemon,status,trigger,cost_cap_units,started_at) VALUES (?,?,?,?,?,?)',
                     (run, 'instagram_kg', 'running', 'daily', 0, utcnow()))
        conn.commit()
        meter = CostMeter(conn, run, DEFAULT_CONFIG['unit_costs'])
        meter.stage = 'instagram_kg'
        client = Client(Budget(meter)) if args.deepseek else None
        seed = json.loads((ROOT / 'data/seed.json').read_text())
        handles = [{'handle': r['instagram'].lstrip('@'), 'target_id': r['id'], 'name': r['name']} for r in seed['restaurants'] if r.get('instagram') and not r.get('closed_at')]
        def terminate(signum, frame):
            raise TransientStop('timeout')
        signal.signal(signal.SIGTERM, terminate)
        rc, line = collect(conn, handles, client=client)
        conn.execute('UPDATE daemon_run SET status=?,finished_at=? WHERE run_id=?', ('failed' if rc else 'succeeded', utcnow(), run))
        conn.commit()
        print(line)
        return rc


if __name__ == '__main__':
    sys.exit(main())
