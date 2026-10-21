"""Instagram step (added at 준태님's explicit decision, 2026-10-21). LOGGED-OUT ONLY.
- No login, no credentials, no cookies, no browser session. Honest UA. No bypass of login walls/captchas/429.
- Reads only the public profile page HTML of handles in seed.json `instagram` (og:description + any post captions
  embedded for logged-out visitors). Stores excerpt <=40 chars, post URL, date. No images, no full captions.
- 8s between requests, max 25 handles/day, stops for the day after 3 blocks (login wall / 429 / challenge).
- Findings are single-source: apply.py never auto-applies them alone.
Output: crawler/out/ig-YYYYMMDD.json; findings are merged by apply.py."""
import json, os, re, sys, time, html, datetime as dt, urllib.request, urllib.error
H = os.path.dirname(os.path.abspath(__file__)); ROOT = os.path.dirname(H)
C = json.load(open(os.path.join(H, 'config.json'), encoding='utf-8'))
UA = C['user_agent']; DELAY = 8; CAP = 25; MAX_BLOCKS = 3
KW = {'closure': ['폐업', '영업 종료', '영업을 종료', '마지막 영업', '문을 닫'], 'rename': ['새 이름', '상호 변경', '이름이 바뀝', '리브랜딩'],
      'chef_move': ['헤드셰프', '합류', '새 셰프', '떠나'], 'opening': ['오픈', '개업', '그랜드 오픈']}

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k): return None
OPENER = urllib.request.build_opener(NoRedirect)  # no cookie processor: nothing stored or sent

def fetch(handle):
    req = urllib.request.Request(f'https://www.instagram.com/{handle}/', headers={'User-Agent': UA, 'Accept-Language': 'ko,en'})
    try:
        r = OPENER.open(req, timeout=20); body = r.read().decode('utf-8', 'replace')
    except urllib.error.HTTPError as e:
        loc = e.headers.get('Location', '') if e.headers else ''
        return None, 'blocked:login_wall' if 'login' in loc or 'challenge' in loc else f'blocked:{e.code}' if e.code in (401, 403, 429) else f'error:{e.code}'
    except Exception as e: return None, f'error:{type(e).__name__}'
    if '/accounts/login' in body[:5000] and 'og:description' not in body: return None, 'blocked:login_wall'
    return body, 'ok'

def parse(handle, body):
    out = []
    for m in re.finditer(r'"shortcode":"([A-Za-z0-9_-]+)".{0,800}?"text":"((?:[^"\\]|\\.){0,600})".{0,800}?"taken_at_timestamp":(\d+)', body):
        code, txt, ts = m.group(1), json.loads('"' + m.group(2) + '"'), int(m.group(3))
        out.append((f'https://www.instagram.com/p/{code}/', dt.date.fromtimestamp(ts).isoformat(), txt))
    og = re.search(r'<meta property="og:description" content="([^"]*)"', body)
    if og: out.append((f'https://www.instagram.com/{handle}/', None, html.unescape(og.group(1))))
    return out

def excerpt(txt, kw):
    i = max(0, txt.find(kw) - 12); return re.sub(r'\s+', ' ', txt[i:i + 40]).strip()

def main(day=None):
    day = day or dt.date.today().isoformat()
    seed = json.load(open(os.path.join(ROOT, 'data', 'seed.json'), encoding='utf-8'))
    rs = [r for r in seed['restaurants'] if r.get('instagram') and not r.get('closed_at')][:CAP]
    F, status, blocks = [], {}, 0
    for i, r in enumerate(rs):
        if blocks >= MAX_BLOCKS: status[r['instagram']] = 'skipped:too_many_blocks'; continue
        if i: time.sleep(DELAY)
        body, st = fetch(r['instagram']); status[r['instagram']] = st
        if body is None:
            blocks += st.startswith('blocked'); continue
        for url, date, txt in parse(r['instagram'], body):
            for kind, ws in KW.items():
                w = next((w for w in ws if w in txt), None)
                if w: F.append(dict(id=r['id'], kind=kind, url=url, host='instagram.com', date=date or day, title=excerpt(txt, w), instagram=True))
    os.makedirs(os.path.join(H, 'out'), exist_ok=True)
    p = os.path.join(H, 'out', f'ig-{day.replace("-", "")}.json')
    json.dump(dict(day=day, status=status, findings=F), open(p, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
    print(p, {s: list(status.values()).count(s) for s in set(status.values())}, 'findings', len(F))

if __name__ == '__main__': main(*(sys.argv[1:2]))
