"""Mocked tests: no network, no login. Run: python3 -m unittest tests/test_instagram.py"""
import datetime as dt, os, sys, tempfile, unittest, types
from zoneinfo import ZoneInfo
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'crawler')); import instagram as ig
KST = ZoneInfo('Asia/Seoul'); IN = dt.datetime(2026, 10, 22, 7, 0, tzinfo=KST)

def fake_il(login_exc=None, profile_exc=None, valid_session=True, captions=('마지막 영업일 안내드립니다 감사했습니다',)):
    m = types.SimpleNamespace(calls=[])
    class RC:
        def __init__(s, ctx): pass
    class L:
        def __init__(s, **k): s.context = 'ctx'; m.kwargs = k
        def login(s, u, p): m.calls.append(('login', u)); 
        def test_login(s): return 'user' if valid_session else None
        def load_session_from_file(s, u, f): m.calls.append(('load', u))
        def save_session_to_file(s, f): open(f, 'w').write('x'); m.calls.append(('save', f))
    if login_exc:
        def bad(s, u, p): raise login_exc
        L.login = bad
    class Post:
        def __init__(s, c): s.shortcode, s.caption, s.date_utc = 'ABC', c, dt.datetime(2026, 10, 20)
    class Profile:
        biography = ''
        @staticmethod
        def from_username(ctx, h):
            if profile_exc: raise profile_exc
            p = Profile(); p.get_posts = lambda: iter([Post(c) for c in captions]); return p
    m.RateController, m.Instaloader, m.Profile = RC, L, Profile
    return m

class T(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(); ig.OUT = self.d; ig.FLAG = os.path.join(self.d, 'ig_blocked.flag')
        ig.SESSION = os.path.join(self.d, 'secrets', 's.session'); os.environ['AGAMAJUNG_IG_PASSWORD'] = 'dummy-not-real'
    def test_window(self):
        self.assertFalse(ig.in_window(dt.datetime(2026, 10, 21, 17, 0, tzinfo=KST)))
        self.assertFalse(ig.in_window(dt.datetime(2026, 10, 22, 6, 44, tzinfo=KST)))
        self.assertTrue(ig.in_window(dt.datetime(2026, 10, 22, 6, 45, tzinfo=KST)))
        self.assertFalse(ig.in_window(dt.datetime(2026, 10, 22, 8, 0, tzinfo=KST)))
    def test_outside_window_never_logs_in(self):
        m = fake_il(); ig.main(now=dt.datetime(2026, 10, 21, 17, 0, tzinfo=KST), il=m, sleep=lambda s: None)
        self.assertEqual(m.calls, [])
    def test_ok_saves_session_600_and_excerpt(self):
        m = fake_il(); self.assertEqual(ig.main(now=IN, il=m, sleep=lambda s: None), 0)
        self.assertEqual(oct(os.stat(ig.SESSION).st_mode & 0o777), '0o600')
        import json; f = json.load(open(os.path.join(self.d, 'ig-20261022.json')))['findings']
        self.assertTrue(f and all(len(x['title']) <= 40 and x['kind'] for x in f)); self.assertIn('closure', {x['kind'] for x in f})
        self.assertEqual(m.kwargs['max_connection_attempts'], 1)
    def test_reuses_session(self):
        os.makedirs(os.path.dirname(ig.SESSION)); open(ig.SESSION, 'w').write('x')
        m = fake_il(); ig.main(now=IN, il=m, sleep=lambda s: None)
        self.assertNotIn('login', [c[0] for c in m.calls])
    def _blocked(self, exc, **kw):
        m = fake_il(**kw) if kw else fake_il(login_exc=exc)
        self.assertEqual(ig.main(now=IN, il=m, sleep=lambda s: None), 2); self.assertTrue(os.path.exists(ig.FLAG))
        self.assertNotIn('dummy-not-real', open(ig.FLAG).read()); return open(ig.FLAG).read()
    def test_checkpoint(self): self.assertIn('checkpoint', self._blocked(Exception('Login: Checkpoint required. Point your browser to /challenge/')))
    def test_2fa(self):
        class TwoFactorAuthRequiredException(Exception): pass
        self.assertIn('TwoFactor', self._blocked(TwoFactorAuthRequiredException('Login error: two-factor authentication required')))
    def test_429_during_fetch(self): self.assertIn('429', self._blocked(None, profile_exc=Exception('429 Too Many Requests')))
    def test_expired_session_no_relogin(self):
        os.makedirs(os.path.dirname(ig.SESSION)); open(ig.SESSION, 'w').write('x')
        m = fake_il(valid_session=False); self.assertEqual(ig.main(now=IN, il=m, sleep=lambda s: None), 2)
        self.assertNotIn('login', [c[0] for c in m.calls])
    def test_flag_stops_next_run(self):
        open(ig.FLAG, 'w').write('x'); m = fake_il(); ig.main(now=IN, il=m, sleep=lambda s: None); self.assertEqual(m.calls, [])
    def test_no_password_in_stdout(self):
        import io, contextlib; buf = io.StringIO()
        with contextlib.redirect_stdout(buf): ig.main(now=IN, il=fake_il(login_exc=Exception('checkpoint')), sleep=lambda s: None)
        self.assertNotIn('dummy-not-real', buf.getvalue())
if __name__ == '__main__': unittest.main()

class TestBlackout(unittest.TestCase):
    def test_blackout(self):
        import datetime as dt
        from zoneinfo import ZoneInfo
        k = ZoneInfo('Asia/Seoul')
        for h, m, exp in [(9, 30, True), (10, 29, True), (10, 30, False), (20, 45, True), (21, 30, False), (7, 0, False)]:
            self.assertEqual(ig.in_blackout(dt.datetime(2026, 10, 23, h, m, tzinfo=k)), exp)
