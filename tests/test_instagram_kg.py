from tests_balance_fake import healthy_balance
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
    time = dt.datetime(2026, 11, 5, 9, 45, tzinfo=ig.KST)

    def now(self):
        return self.time


class LoginRequiredException(Exception):
    """Name mirrors instaloader's exception; the class-name signal must classify without a separator."""


class ProfileNotExistsException(Exception):
    """instaloader raises this with 'Profile X has blocked you' for an explicit per-profile block."""


class Backend:
    def __init__(self, guard, calls, *, failure=None, private=False, posts=None):
        self.guard, self.calls, self.failure, self.private = guard, calls, failure, private
        self.timeline = list(range(8, 0, -1)) if posts is None else posts

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def request(self, kind):
        def send():
            self.calls.append(kind)
            if self.failure and kind == self.failure[0]:
                signal = self.failure[1]
                if isinstance(signal, Exception):
                    raise signal
                return signal
        return self.guard.dispatch(send)

    def load_session(self, session):
        self.request('session_validate')

    def profile(self, handle):
        self.request('profile:' + handle)
        return SimpleNamespace(is_private=self.private, biography='영업 종료 안내 ' + 'x' * 80)

    def posts(self, profile):
        for i in self.timeline:
            self.request(f'post:{i}')
            yield SimpleNamespace(shortcode=str(i), date_utc=dt.datetime(2026, 11, 4, 12) + dt.timedelta(minutes=i),
                                  caption='새 이름으로 오픈 ' + 'x' * 80)


@pytest.fixture
def world(tmp_path):
    conn = connect(str(tmp_path / 'state.db'))
    migrate(conn)
    clock, calls = Clock(), []
    session = tmp_path / 'fixture.session'
    session.write_text('synthetic-session')
    opts = dict(flag=tmp_path/'ig_blocked.flag', out=tmp_path/'ig_kg', session=session, now=clock.now)
    return conn, clock, calls, opts


def handles(n=1):
    return [{'handle': f'brand{i}', 'target_id': f'r{i}'} for i in range(n)]


def test_saved_session_outside_old_window_no_quotas_private_output(world):
    conn, clock, calls, opts = world
    rc, line = ig.collect(conn, handles(), lambda g: Backend(g, calls), **opts)
    assert rc == 0 and line.startswith('IG_OK account=kitchen.genome') and 'ai=disabled' in line
    assert calls == ['session_validate', 'profile:brand0'] + [f'post:{i}' for i in range(8, 0, -1)]
    opts_rows = conn.execute("SELECT review_id,payload_json FROM review_queue WHERE subject_type='ig_kg'").fetchall()
    assert opts_rows
    for row in opts_rows:
        rec = json.loads(row['payload_json'])
        assert rec['review_id'] == row['review_id']
        assert rec['pending_only'] and rec['collector_account'] == 'kitchen.genome' and len(rec['excerpt']) <= 40
    assert conn.execute('SELECT COUNT(*) FROM claim').fetchone()[0] == 0
    assert 'reason=no_handles' in ig.collect(conn, handles(), lambda g: pytest.fail('duplicate call'), **opts)[1]
    ig.collect(conn, handles(30), lambda g: Backend(g, calls, posts=[]), **opts)
    assert sum(k.startswith('profile:') for k in calls) == 30
    assert conn.execute('SELECT COUNT(*) FROM ig_follow_day').fetchone()[0] == 0


@pytest.mark.parametrize('failure,reason', [
    (('session_validate', ValueError('checkpoint synthetic-sensitive')), 'checkpoint'),
    (('profile:brand0', SimpleNamespace(status_code=429, text='synthetic-sensitive')), 'http_429'),
    (('post:6', SimpleNamespace(status_code=200, text='{"two_factor_required":true}')), 'two_factor'),
    (('post:6', SimpleNamespace(status_code=200, text='{"message":"challenge_required"}')), 'challenge'),
    (('post:6', SimpleNamespace(status_code=401, text='synthetic-sensitive')), 'access_denied'),
    (('profile:brand0', LoginRequiredException('profile brand0 requires login')), 'login_failed'),
    (('profile:brand0', ProfileNotExistsException('Profile brand0 has blocked you')), 'access_denied'),
])
def test_provider_block_stops_and_records_first_signal(world, failure, reason):
    conn, clock, calls, opts = world
    rc, line = ig.collect(conn, handles(2), lambda g: Backend(g, calls, failure=failure), **opts)
    assert rc == 2 and f'reason={reason}' in line
    saved = opts['flag'].read_bytes()
    assert json.loads(saved)['reason'] == reason
    assert b'synthetic-sensitive' not in saved and 'synthetic-sensitive' not in line
    assert calls[-1] == failure[0]
    clock.time += dt.timedelta(days=1)
    assert 'blocked_flag' in ig.collect(conn, handles(), lambda g: pytest.fail('dispatch after block'), **opts)[1]
    assert opts['flag'].read_bytes() == saved


@pytest.mark.parametrize('filename', ['ig_blocked.flag', 'ig_kg_blocked.flag'])
def test_existing_flags_are_immutable_and_gate_contact(world, filename):
    conn, clock, calls, opts = world
    path = opts['flag'].with_name(filename)
    before = b'\xffexisting marker'
    path.write_bytes(before)
    assert 'blocked_flag' in ig.collect(conn, handles(), lambda g: pytest.fail('contact with flag'), **opts)[1]
    ig.Guard(conn, opts['flag'], clock.now).block('checkpoint')
    assert path.read_bytes() == before
    assert len(list(path.parent.glob('*.flag'))) == 1


def test_timeout_keeps_partial_post_checkpoint_without_flag(world):
    conn, clock, calls, opts = world
    failure = ('post:6', TimeoutError('fixture reset'))
    rc, line = ig.collect(conn, handles(2), lambda g: Backend(g, calls, failure=failure), **opts)
    assert rc == 1 and 'transport_unknown' in line and calls[-1] == 'post:6'
    assert not list(opts['flag'].parent.glob('*.flag'))
    assert conn.execute('SELECT COUNT(*) FROM ig_post_checkpoint').fetchone()[0] == 2
    assert conn.execute('SELECT COUNT(*) FROM ig_post_watermark').fetchone()[0] == 0
    assert ig.main(['--db', str(opts['session']), '--mark-timeout']) == 1
    assert not list(opts['flag'].parent.glob('*.flag'))
    assert ig.collect(conn, handles(), lambda g: Backend(g, calls), **opts)[0] == 0
    assert conn.execute('SELECT COUNT(*) FROM ig_post_checkpoint').fetchone()[0] == 0
    assert conn.execute('SELECT shortcode FROM ig_post_watermark').fetchone()[0] == '8'


def test_next_day_reads_only_captions_newer_than_watermark(world, monkeypatch):
    conn, clock, calls, opts = world
    original = ig.scan_text
    inspected = []

    def scan(*args, **kwargs):
        if '/p/' in args[4]:
            inspected.append(args[4].split('/')[-2])
        return original(*args, **kwargs)

    monkeypatch.setattr(ig, 'scan_text', scan)
    assert ig.collect(conn, handles(), lambda g: Backend(g, calls), **opts)[0] == 0
    clock.time += dt.timedelta(days=1)
    calls.clear()
    inspected.clear()
    assert ig.collect(conn, handles(), lambda g: Backend(g, calls, posts=[10, 9, 8, 7]), **opts)[0] == 0
    assert inspected == ['10', '9'] and calls[-1] == 'post:8'
    assert conn.execute('SELECT shortcode FROM ig_post_watermark').fetchone()[0] == '10'


def test_missing_session_and_private_profile(world):
    conn, clock, calls, opts = world
    ig.collect(conn, handles(), lambda g: Backend(g, calls, private=True), **opts)
    assert calls == ['session_validate', 'profile:brand0']
    assert conn.execute('SELECT COUNT(*) FROM review_queue').fetchone()[0] == 0
    opts['session'].unlink()
    assert 'missing_session' in ig.collect(conn, handles(), lambda g: pytest.fail('contact without session'), **opts)[1]


def test_dedicated_module_never_imports_legacy_crawler():
    import ast
    statements = ast.walk(ast.parse(Path(ig.__file__).read_text()))
    for node in statements:
        if isinstance(node, ast.ImportFrom):
            assert node.module != 'instagram'
        if isinstance(node, ast.Import):
            assert all(name.name != 'instagram' for name in node.names)


def test_deepseek_ig_json_is_metered_filtered_and_pending(world):
    conn, clock, calls, opts = world
    requests = []
    def transport(payload, key):
        data = json.loads(json.loads(payload)['messages'][1]['content']); requests.append(data)
        return response({'findings': [{'kind':'opening','excerpt':'오픈'}], 'follow':'review'})
    client = Client(Budget(meter(conn)), key='dummy', transport=transport, balance_transport=healthy_balance)
    rows, decision = ig.scan_text(conn, client, 'brand0','r0','https://www.instagram.com/p/fake/',None,'오픈 안내. 문의 me@example.test')
    assert requests and 'me@example.test' not in json.dumps(requests)
    assert any(r['asserted_by']=='ai_inference' for r in rows) and decision == 'review'
    assert all(r['pending_only'] and len(r['excerpt'])<=40 for r in rows)
    assert conn.execute('SELECT total_tokens FROM (SELECT prompt_tokens+completion_tokens total_tokens FROM ai_request)').fetchone()[0] == 120


def test_real_backend_guards_temporary_sessions_and_rejects_wrong_identity(world, monkeypatch):
    conn, clock, calls, opts = world
    import requests
    def send(session, request, **kwargs):
        calls.append((request.url, clock.now().timestamp()))
        assert request.headers['User-Agent'] == ig.UA and kwargs['allow_redirects'] is False
        return SimpleNamespace(status_code=200,text='{}')
    monkeypatch.setattr(requests.sessions.Session,'send',send)
    original = requests.sessions.Session.send
    class RC:
        def __init__(self,ctx): pass
    class Loader:
        def __init__(self,**kwargs): assert kwargs['max_connection_attempts']==1
        def load_session_from_file(self,username,path):
            assert username=='kitchen.genome' and Path(path).is_file()
        def login(self,username,password):
            raise AssertionError('password login is not allowed')
        def test_login(self):
            requests.Session().send(requests.Request('GET','https://www.instagram.com/accounts/current/').prepare())
            return 'wrong-account'
    monkeypatch.setitem(sys.modules,'instaloader',SimpleNamespace(Instaloader=Loader,RateController=RC))
    before = opts['session'].read_text()
    rc,line=ig.collect(conn,handles(),ig.LoaderBackend,**opts)
    assert rc==2 and 'invalid_session' in line
    assert len(calls)==1 and 'accounts/login' not in calls[0][0]
    assert requests.sessions.Session.send is original
    assert opts['session'].read_text()==before


def test_retention_purges_review_output_and_model_cache(world):
    conn, clock, calls, opts = world
    client=Client(Budget(meter(conn),day=lambda:'2026-09-01'), key='dummy',transport=lambda *a:response({'findings':[{'kind':'opening','excerpt':'오픈'}],'follow':'review'}), balance_transport=healthy_balance)
    ig.scan_text(conn,client,'brand','target','https://www.instagram.com/p/x/',None,'오픈 안내','2026-09-01')
    opts['out'].mkdir(); (opts['out']/'20260901.json').write_text('{"excerpt":"오픈"}')
    ig.purge(conn,opts['out'],clock.now())
    payload=json.loads(conn.execute("SELECT payload_json FROM review_queue WHERE subject_type='ig_kg' LIMIT 1").fetchone()[0])
    assert 'excerpt' not in payload and payload['review_id']
    assert not (opts['out']/'20260901.json').exists()
    row = conn.execute('SELECT state,result_json FROM ai_request').fetchone()
    assert (row['state'], row['result_json']) == ('expired', None)


def test_missing_backend_dependency_skips_without_account_block(world):
    conn, clock, calls, opts = world
    def absent(guard): raise ImportError('synthetic module unavailable')
    rc,line=ig.collect(conn,handles(),absent,**opts)
    assert rc==0 and 'IG_SKIPPED' in line and 'dependency_unavailable' in line
    assert not opts['flag'].exists() and calls==[]
