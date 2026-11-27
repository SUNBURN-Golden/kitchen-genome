"""Instagram step, logged-in via 아가마중's account (준태님 decision 2026-10-21).
Rules (hard):
- Runs ONLY between 06:45 and 08:00 Asia/Seoul; otherwise exits with IG_SKIPPED.
- Login id below; password ONLY from env AGAMAJUNG_IG_PASSWORD, read at runtime, never printed/logged/stored.
- Session file outside the repo: /home/box/.secrets/kg_ig_agamajung.session (chmod 600); reused so login is rare.
- Any checkpoint / challenge / 2FA / email code / suspicious login / 429 / bad login: STOP at once, no retry,
  write crawler/out/ig_blocked.flag (reason) and print IG_BLOCKED. While the flag exists the step does not run
  (a human must clear it after 준태님/BabyShower check the account).
- <=25 handles/day, >=8s between requests, profile + up to 6 recent post captions/dates only.
- Stores excerpt <=40 chars, post URL, date. No images, no full captions. Findings are pending-only (apply.py).
Output lines: IG_SKIPPED reason=... | IG_BLOCKED reason=... | IG_OK handles=N findings=M
Collection is off unless KG_INSTAGRAM_ENABLED=1 (@kitchen.genome disabled 2026-10-22)."""
import json, os, re, sys, time, datetime as dt
from zoneinfo import ZoneInfo
from ig_config import DISABLED_REASON, instagram_enabled
H = os.path.dirname(os.path.abspath(__file__)); ROOT = os.path.dirname(H)
OUT = os.path.join(H, 'out'); FLAG = os.path.join(OUT, 'ig_blocked.flag')
LOGIN = 'agamajung'
SESSION = '/home/box/.secrets/kg_ig_agamajung.session'
WINDOW = (dt.time(6, 45), dt.time(8, 0))
DELAY, CAP, POSTS = 8, 25, 6
KW = {'closure': ['폐업', '영업 종료', '영업을 종료', '마지막 영업', '문을 닫'], 'rename': ['새 이름', '상호 변경', '이름이 바뀝', '리브랜딩'],
      'chef_move': ['헤드셰프', '합류', '새 셰프', '떠나'], 'opening': ['오픈', '개업', '그랜드 오픈']}
BLOCK_WORDS = ('checkpoint', 'challenge', 'two-factor', 'two factor', '2fa', 'verify', 'suspicious', 'please wait', '429',
               'too many', 'login_required', 'feedback_required', 'security code', 'confirm')

class Blocked(Exception): pass
class Disabled(Exception): pass

def in_window(now=None):
    now = now or dt.datetime.now(ZoneInfo('Asia/Seoul'))
    return WINDOW[0] <= now.time() < WINDOW[1]

BLACKOUT = ((dt.time(9, 30), dt.time(10, 30)), (dt.time(20, 30), dt.time(21, 30)))  # never log in (KST)

def in_blackout(now=None):
    t = (now or dt.datetime.now(ZoneInfo('Asia/Seoul'))).time()
    return any(a <= t < b for a, b in BLACKOUT)

def block(reason):
    os.makedirs(OUT, exist_ok=True)
    reason = re.sub(r'\s+', ' ', str(reason))[:200]
    open(FLAG, 'w').write(json.dumps(dict(at=dt.datetime.now(ZoneInfo('Asia/Seoul')).isoformat(timespec='seconds'), reason=reason), ensure_ascii=False))
    notify = ' notify=BabyShower(BabyShower에게 알릴 것)' if 'checkpoint' in reason.lower() else ''
    print(f'IG_BLOCKED reason={reason}{notify}'); return 2

def classify_exc(e):
    """Every instaloader/network problem during login or fetch is treated as a stop signal."""
    name = type(e).__name__; msg = str(e).lower()
    for w in BLOCK_WORDS:
        if w in msg: return f'{name}: {w}'
    return f'{name}'

def excerpt(txt, kw):
    i = max(0, txt.find(kw) - 12); return re.sub(r'\s+', ' ', txt[i:i + 40]).strip()

def scan(rid, url, date, txt):
    out = []
    for kind, ws in KW.items():
        w = next((w for w in ws if w in (txt or '')), None)
        if w: out.append(dict(id=rid, kind=kind, url=url, host='instagram.com', date=date, title=excerpt(txt, w), instagram=True))
    return out

def make_loader(il):
    class StopRC(il.RateController):           # never sleep-and-retry on 429: stop instead
        def handle_429(self, query_type): raise Blocked('429 too many requests')
    return il.Instaloader(download_pictures=False, download_videos=False, download_video_thumbnails=False,
                          download_geotags=False, download_comments=False, save_metadata=False, compress_json=False,
                          max_connection_attempts=1, quiet=True, rate_controller=lambda ctx: StopRC(ctx))

def login(L, il):
    if not instagram_enabled():
        raise Disabled(DISABLED_REASON)
    if os.path.exists(SESSION):
        L.load_session_from_file(LOGIN, SESSION)
        if L.test_login(): return
        raise Blocked('saved session no longer valid (not re-logging automatically same day)')
    pw = os.environ.get('AGAMAJUNG_IG_PASSWORD')
    if not pw: raise Blocked('AGAMAJUNG_IG_PASSWORD not set')
    try: L.login(LOGIN, pw)
    finally: pw = None
    os.makedirs(os.path.dirname(SESSION), mode=0o700, exist_ok=True)
    old = os.umask(0o077)
    try: L.save_session_to_file(SESSION)
    finally: os.umask(old)
    os.chmod(SESSION, 0o600)

def main(now=None, il=None, sleep=time.sleep):
    if not instagram_enabled():
        print(f'IG_SKIPPED reason={DISABLED_REASON}'); return 0
    if in_blackout(now): print('IG_SKIPPED reason=blackout_0930_1030_or_2030_2130_KST'); return 0
    if not in_window(now): print('IG_SKIPPED reason=outside_window_0645_0800_KST'); return 0
    if os.path.exists(FLAG): print('IG_SKIPPED reason=blocked_flag_present (clear crawler/out/ig_blocked.flag after a human check)'); return 0
    if il is None:
        try: import instaloader as il
        except ImportError: print('IG_SKIPPED reason=instaloader_not_installed'); return 0
    seed = json.load(open(os.path.join(ROOT, 'data', 'seed.json'), encoding='utf-8'))
    rs = [r for r in seed['restaurants'] if r.get('instagram') and not r.get('closed_at')][:CAP]
    day = (now or dt.datetime.now(ZoneInfo('Asia/Seoul'))).date().isoformat()
    F, status = [], {}
    try:
        L = make_loader(il); login(L, il)
        for i, r in enumerate(rs):
            sleep(DELAY)
            p = il.Profile.from_username(L.context, r['instagram'])
            status[r['instagram']] = 'ok'
            F += scan(r['id'], f'https://www.instagram.com/{r["instagram"]}/', day, p.biography or '')
            for j, post in enumerate(p.get_posts()):
                if j >= POSTS: break
                F += scan(r['id'], f'https://www.instagram.com/p/{post.shortcode}/', post.date_utc.date().isoformat(), post.caption or '')
    except Exception as e:
        os.makedirs(OUT, exist_ok=True)
        json.dump(dict(day=day, status=status, findings=F), open(os.path.join(OUT, f'ig-{day.replace("-", "")}.json'), 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
        return block(classify_exc(e) if not isinstance(e, Blocked) else str(e))
    os.makedirs(OUT, exist_ok=True)
    json.dump(dict(day=day, status=status, findings=F), open(os.path.join(OUT, f'ig-{day.replace("-", "")}.json'), 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
    print(f'IG_OK handles={len(status)} findings={len(F)}'); return 0

if __name__ == '__main__': sys.exit(main())
