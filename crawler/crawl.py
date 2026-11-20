"""Kitchen Genome crawler. Deterministic public HTTP reads under the shared access policy.
Sources: (1) Bing News RSS per tracked restaurant (lead signal; the finding stores the publisher URL),
(2) publisher RSS feeds in config.json, (3) the restaurant's own website (status only).
Korean press uses robots and spacing. Every host is serialized and access refusals stop its run.
Output: crawler/out/findings-YYYYMMDD.json  (list of {id, kind, url, host, date, title, new_name?})."""
import json, os, re, sys, datetime as dt, urllib.parse, xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
H = os.path.dirname(os.path.abspath(__file__)); ROOT = os.path.dirname(H)
C = json.load(open(os.path.join(H, 'config.json'), encoding='utf-8'))
sys.path.insert(0, os.path.join(ROOT, 'daemon'))
from kg_collect.net import PoliteNet, host_of as host, is_banned as banned
from kg_daemon.db import connect, migrate

_net = None


def network():
    global _net
    if _net is None:
        db_path = os.environ.get('KG_SERVICE_DB') or os.path.join(ROOT, 'data_store', 'kg_service.db')
        conn = connect(db_path)
        migrate(conn)
        _net = PoliteNet(conn=conn)
    return _net


def get(u, timeout=20):
    response = network().get(u, timeout=timeout)
    if response.status != 200 or response.error:
        return None, response.error or response.status
    return response.text(), response.status

def rss_items(xml):
    try: root = ET.fromstring(xml)
    except Exception: return []
    out = []
    for it in root.iter('item'):
        link = it.findtext('link') or ''
        q = urllib.parse.parse_qs(urllib.parse.urlparse(link).query)
        if 'url' in q: link = q['url'][0]          # unwrap bing apiclick redirect
        try: d = parsedate_to_datetime(it.findtext('pubDate')).date().isoformat()
        except Exception: d = None
        txt = re.sub('<[^>]+>', ' ', (it.findtext('title') or '') + ' ' + (it.findtext('description') or ''))
        out.append(dict(url=link, date=d, text=re.sub(r'\s+', ' ', txt).strip()))
    return out

def classify(text):
    t = text.lower()
    return [k for k, ws in C['keywords'].items() if any(w.lower() in t for w in ws)]

def mentions(r, text):
    names = [r['name']] + ([r['name_en']] if r.get('name_en') else [])
    return any(n and len(n) >= 2 and n.lower() in text.lower() for n in names)

def chef_names(r):
    return [c for c in re.split(r'[·,/ ]+', re.sub(r'\(.*?\)', '', r.get('chef') or '')) if len(c) >= 2]

def main(day=None):
    from kg_collect.proxynet import DEFAULT_ROUTE

    net = network()
    daily_scope = os.environ.get('KG_WEB_RUN_ID')
    net.start_run(run_id=daily_scope, join=bool(daily_scope))
    try:
        return collect(day)
    finally:
        if not daily_scope:
            net.finish_run()
        if DEFAULT_ROUTE.name:
            print(f'egress={DEFAULT_ROUTE.name} egress_reason={DEFAULT_ROUTE.reason}')


def collect(day=None):
    day = day or dt.date.today().isoformat()
    seed = json.load(open(os.path.join(ROOT, 'data', 'seed.json'), encoding='utf-8'))
    cutoff = (dt.date.fromisoformat(day) - dt.timedelta(days=C['lookback_days'])).isoformat()
    rs = [r for r in seed['restaurants'] if not r.get('closed_at')]
    F = []; stats = dict(queries=0, items=0, robots_blocked=0, errors=0)
    def add(r, kind, it, extra=None):
        if banned(it['url']) or it['url'] in set(C.get('ignored_urls', ())) or (it['date'] and it['date'] < cutoff): return
        f = dict(id=r['id'], kind=kind, url=it['url'], host=host(it['url']), date=it['date'] or day, title=it['text'][:200])
        f.update(extra or {}); F.append(f)
    # 1) per-restaurant news search
    for r in rs:
        q = f'"{r["name"]}" ' + ' '.join(chef_names(r)[:1])
        body, st = get('https://www.bing.com/news/search?' + urllib.parse.urlencode({'q': q, 'format': 'rss', 'setlang': 'ko'}))
        stats['queries'] += 1
        if body is None: stats['robots_blocked' if st == 'robots' else 'errors'] += 1; continue
        for it in rss_items(body)[:C['max_items_per_query']]:
            # Feed items become findings, not fetch jobs; do not spend the dispatch-time
            # candidate budget on links that will never be requested.
            stats['items'] += 1
            if not mentions(r, it['text']): continue
            for k in classify(it['text']): add(r, k, it)
    # 2) publisher feeds
    for fu in C['publisher_feeds']:
        body, st = get(fu)
        if body is None: stats['robots_blocked' if st == 'robots' else 'errors'] += 1; continue
        for it in rss_items(body):
            stats['items'] += 1
            for r in rs:
                if mentions(r, it['text']):
                    for k in classify(it['text']): add(r, k, it)
    # 3) official websites (non-Instagram/Naver) -> only a 'site_down' lead, never auto-applied
    for r in rs:
        u = r.get('url') or ''
        if not u.startswith('http') or banned(u): continue
        body, st = get(u)
        if body is None and st not in ('robots',) and st != 200 and (isinstance(st, int) and st in (404, 410) or st in ('URLError',)):
            F.append(dict(id=r['id'], kind='site_down', url=u, host=host(u), date=day, title=f'official site returned {st}'))
    os.makedirs(os.path.join(H, 'out'), exist_ok=True)
    p = os.path.join(H, 'out', f'findings-{day.replace("-", "")}.json')
    json.dump(dict(day=day, stats=stats, findings=F), open(p, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
    print(p, stats, 'findings', len(F)); return p

if __name__ == '__main__': main(*(sys.argv[1:2]))
