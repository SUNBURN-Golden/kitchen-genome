"""Synthetic identity/HTTP/model tests; never read the real secret directory."""
import datetime as dt
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import pytest
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'crawler'))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'daemon'))
import instagram_kg as ig
from kg_daemon.db import connect, migrate
from kg_ai.budget import Budget
from kg_ai.deepseek import Client
from test_deepseek_client import meter, response


class Clock:
    def __init__(self): self.time = dt.datetime(2026, 11, 5, 7, tzinfo=ig.KST); self.sleeps = []
    def now(self): return self.time
    def sleep(self, seconds): self.sleeps.append(seconds); self.time += dt.timedelta(seconds=seconds)


class Backend:
    follow_available = True
    def __init__(self, guard, calls, *, fail=None, private=False, count=7):
        self.guard, self.calls, self.fail, self.private, self.count = guard, calls, fail, private, count
    def __enter__(self): return self
    def __exit__(self, *a): pass
    def request(self, kind):
        def send():
            self.calls.append((kind, self.guard.now().timestamp()))
            if kind == self.fail: raise Exception('checkpoint synthetic-password')
        self.guard.dispatch(send)
    def login(self, session, password):
        assert password == 'synthetic-password'
        self.request('login'); self.request('session_validate')
    def profile(self, handle):
        self.request('profile')
        return SimpleNamespace(is_private=self.private, biography='영업 종료 안내 ' + 'x'*80, followed_by_viewer=False)
    def posts(self, profile):
        for i in range(self.count):
            self.request('caption_' + str(i))
            yield SimpleNamespace(shortcode=str(i), date_utc=dt.datetime(2026, 11, 4), caption='새 이름으로 오픈 ' + 'x'*80)
    def follow(self, handle): self.request('follow')


@pytest.fixture
def world(tmp_path):
    c = connect(str(tmp_path / 'state.db')); migrate(c)
    clock, calls = Clock(), []
    opts = dict(flag=tmp_path/'ig_kg_blocked.flag', out=tmp_path/'ig_kg', session=tmp_path/'secrets/session', now=clock.now,
                sleep=clock.sleep, password='synthetic-password')
    return c, clock, calls, opts


def handles(n=1): return [{'handle': f'brand{i}', 'target_id': f'r{i}'} for i in range(n)]


def test_request_delay_caption_limit_and_pending_only(world):
    conn, clock, calls, opts = world
    rc, line = ig.collect(conn, handles(), lambda g: Backend(g, calls), **opts)
    assert rc == 0 and line.startswith('IG_OK account=kitchen.genome') and 'ai=disabled' in line
    assert len(calls) == 9  # login, validation, profile, SIX lazy captions; never next() for seventh
    assert all(b[1]-a[1] >= 8 for a,b in zip(calls, calls[1:]))
    rows = [json.loads(r[0]) for r in conn.execute("SELECT payload_json FROM review_queue WHERE subject_type='ig_kg'")]
    assert all(json.loads(r['payload_json'])['review_id'] == r['review_id'] for r in conn.execute("SELECT review_id,payload_json FROM review_queue WHERE subject_type='ig_kg'"))
    assert rows and all(r['pending_only'] and r['collector_account'] == 'kitchen.genome' and len(r['excerpt']) <= 40 for r in rows)
    assert conn.execute('SELECT COUNT(*) FROM claim').fetchone()[0] == 0
    assert not list(Path(opts['out']).parent.glob('ig-*.json'))
    count = len(calls)
    assert 'daily_cap_or_no_handles' in ig.collect(conn, handles(), lambda g: Backend(g,calls), **opts)[1]
    assert len(calls) == count
    assert ig.LOGIN == 'kitchen.genome' and str(ig.SESSION) == '/home/box/.secrets/kg_ig_kitchen_genome.session'


@pytest.mark.parametrize('fail', ['login', 'session_validate', 'profile', 'caption_2', 'follow'])
def test_checkpoint_ends_all_activity_and_keeps_partial_results(world, fail):
    conn, clock, calls, opts = world
    class FollowClient:
        status = 'enabled'; last_meta = {'model': 'fixture'}; stopped = None
        def call(self, task, instruction, data, validate):
            if task == 'ig-plan-1': return validate({'handles': [x['handle'] for x in data['handles']][:25]})
            return validate({'findings': [], 'follow': 'follow'})
    rc, line = ig.collect(conn, handles(2), lambda g: Backend(g, calls, fail=fail), client=FollowClient(), follow=True, **opts)
    assert rc == 2 and 'checkpoint' in line and opts['flag'].exists()
    assert calls[-1][0] == fail
    assert 'synthetic-password' not in opts['flag'].read_text() + line
    if fail in ('caption_2', 'follow'):
        assert conn.execute('SELECT COUNT(*) FROM review_queue').fetchone()[0] > 0
    old = len(calls)
    clock.time += dt.timedelta(days=1)
    ig.collect(conn, handles(), lambda g: Backend(g,calls), **opts)
    assert len(calls) == old  # persistent flag survives midnight


def test_same_day_stop_persists_when_human_removes_flag(world):
    conn, clock, calls, opts = world
    ig.collect(conn, handles(), lambda g: Backend(g, calls, fail='profile'), **opts)
    opts['flag'].unlink()
    n = len(calls)
    assert 'same_day_stop' in ig.collect(conn, handles(), lambda g: Backend(g,calls), **opts)[1]
    assert len(calls) == n and opts['flag'].exists()


def test_25_handle_quota_survives_restart(world):
    conn, clock, calls, opts = world
    ig.collect(conn, handles(20), lambda g: Backend(g, calls, count=0), **opts)
    ig.collect(conn, handles(30), lambda g: Backend(g, calls, count=0), **opts)
    assert conn.execute('SELECT COUNT(*) FROM ig_handle_day').fetchone()[0] == 25
    assert sum(k=='profile' for k,_ in calls) == 25


def test_follows_require_opt_in_and_cap_is_durable(world):
    conn, clock, calls, opts = world
    class Model:
        status = 'enabled'; last_meta = {}; stopped = None
        def call(self, task, instruction, data, validate):
            if task == 'ig-plan-1': return validate({'handles': [x['handle'] for x in data['handles']][:25]})
            return validate({'findings': [], 'follow': 'follow'})
    ig.collect(conn, handles(12), lambda g: Backend(g,calls,count=0), client=Model(), **opts)
    assert not any(k=='follow' for k,_ in calls)
    ig.collect(conn, handles(25), lambda g: Backend(g,calls,count=0), client=Model(), follow=True, **opts)
    assert sum(k=='follow' for k,_ in calls) == 10
    assert conn.execute('SELECT COUNT(*) FROM ig_follow_day').fetchone()[0] == 10


def test_outside_window_and_legacy_flag_are_separate(world):
    conn, clock, calls, opts = world
    legacy = opts['flag'].parent / 'ig_blocked.flag'; legacy.write_text('legacy-paused')
    clock.time = clock.time.replace(hour=9, minute=30)
    assert 'outside_window' in ig.collect(conn, handles(), lambda g: Backend(g,calls), **opts)[1]
    assert calls == [] and legacy.read_text() == 'legacy-paused'
    clock.time = clock.time.replace(hour=7)
    ig.collect(conn, handles(), lambda g: Backend(g,calls), **opts)
    assert calls


def test_private_profile_no_captions(world):
    conn, clock, calls, opts = world
    ig.collect(conn, handles(), lambda g: Backend(g,calls,private=True), **opts)
    assert [k for k,_ in calls] == ['login','session_validate','profile']
    assert conn.execute('SELECT COUNT(*) FROM review_queue').fetchone()[0] == 0


def test_guard_rechecks_window_after_sleep_and_http_429(world):
    conn, clock, calls, opts = world
    guard = ig.Guard(conn, opts['flag'], clock.now, clock.sleep)
    guard.dispatch(lambda: None)
    clock.time = clock.time.replace(hour=7,minute=59,second=59)
    conn.execute('UPDATE ig_account_state SET last_dispatch=?', (clock.time.timestamp(),)); conn.commit()
    with pytest.raises(ig.Skip): guard.dispatch(lambda: pytest.fail('dispatch after window'))
    clock.time = clock.time.replace(hour=7,minute=0,second=0)
    conn.execute('UPDATE ig_account_state SET last_dispatch=NULL'); conn.commit()
    with pytest.raises(ig.Stop, match='http_429'):
        guard.dispatch(lambda: SimpleNamespace(status_code=429,text='sensitive body'))
    with pytest.raises(ig.Stop): guard.dispatch(lambda: pytest.fail('request after block'))
    assert 'sensitive body' not in opts['flag'].read_text()


def test_deepseek_ig_json_is_metered_filtered_and_pending(world):
    conn, clock, calls, opts = world
    requests = []
    def transport(payload, key):
        data = json.loads(json.loads(payload)['messages'][1]['content']); requests.append(data)
        return response({'findings': [{'kind':'opening','excerpt':'오픈'}], 'follow':'review'})
    client = Client(Budget(meter(conn)), key='dummy', transport=transport)
    rows, decision = ig.scan_text(conn, client, 'brand0','r0','https://www.instagram.com/p/fake/',None,'오픈 안내. 문의 me@example.test')
    assert requests and 'me@example.test' not in json.dumps(requests)
    assert any(r['asserted_by']=='ai_inference' for r in rows) and decision == 'review'
    assert all(r['pending_only'] and len(r['excerpt'])<=40 for r in rows)
    assert conn.execute('SELECT total_tokens FROM (SELECT prompt_tokens+completion_tokens total_tokens FROM ai_request)').fetchone()[0] == 120


def test_real_backend_guards_temporary_sessions_and_rejects_wrong_identity(world, monkeypatch):
    conn, clock, calls, opts = world
    import requests
    def send(session, request, **kwargs):
        calls.append((request.url,clock.now().timestamp()))
        return SimpleNamespace(status_code=200,text='{}')
    monkeypatch.setattr(requests.sessions.Session,'send',send)
    original = requests.sessions.Session.send
    class RC:
        def __init__(self,ctx): pass
    class Loader:
        def __init__(self,**kwargs): assert kwargs['max_connection_attempts']==1
        def login(self,username,password):
            assert username=='kitchen.genome' and password=='synthetic-password'
            # New temporary Session() is covered by the transport patch too.
            requests.Session().send(requests.Request('POST','https://www.instagram.com/accounts/login/').prepare())
        def test_login(self):
            requests.Session().send(requests.Request('GET','https://www.instagram.com/accounts/current/').prepare())
            return 'wrong-account'
    monkeypatch.setitem(sys.modules,'instaloader',SimpleNamespace(Instaloader=Loader,RateController=RC))
    rc,line=ig.collect(conn,handles(),ig.LoaderBackend,**opts)
    assert rc==2 and 'invalid_session_identity' in line
    assert len(calls)==2 and calls[1][1]-calls[0][1]>=8
    assert requests.sessions.Session.send is original
    assert not opts['session'].exists()


def test_retention_purges_review_output_and_model_cache(world):
    conn, clock, calls, opts = world
    client=Client(Budget(meter(conn),day=lambda:'2026-09-01'), key='dummy',transport=lambda *a:response({'findings':[{'kind':'opening','excerpt':'오픈'}],'follow':'review'}))
    ig.scan_text(conn,client,'brand','target','https://www.instagram.com/p/x/',None,'오픈 안내','2026-09-01')
    opts['out'].mkdir(); (opts['out']/'20260901.json').write_text('{"excerpt":"오픈"}')
    ig.purge(conn,opts['out'],clock.now())
    payload=json.loads(conn.execute("SELECT payload_json FROM review_queue WHERE subject_type='ig_kg' LIMIT 1").fetchone()[0])
    assert 'excerpt' not in payload and payload['review_id']
    assert not (opts['out']/'20260901.json').exists()
    assert conn.execute('SELECT result_json FROM ai_request').fetchone()[0] is None


def test_missing_backend_dependency_skips_without_account_block(world):
    conn, clock, calls, opts = world
    def absent(guard): raise ImportError('synthetic module unavailable')
    rc,line=ig.collect(conn,handles(),absent,**opts)
    assert rc==0 and 'IG_SKIPPED' in line and 'dependency_unavailable' in line
    assert not opts['flag'].exists() and calls==[]
