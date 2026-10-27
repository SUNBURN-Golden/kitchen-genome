"""Doc -> extraction rows (+ target mentions, resolution, extractor health). Idempotent: extraction_id is a hash of (content hash, span, label, extractor version)."""
import json
import re
import statistics

from kg_daemon.db import stable_id, utcnow

from . import EXTRACTOR_VERSION
from .extract import extract_text, needs_model
from .fetch import Doc
from .resolve import resolve_extractions
from .targets import Gazetteer, norm

HTML_TAG = re.compile(r'<([a-zA-Z][a-zA-Z0-9]*)')


def official_hosts(conn) -> set:
    from .net import host_of
    hosts = set()
    for r in conn.execute("SELECT attrs_json FROM target WHERE kind='restaurant'"):
        site = json.loads(r['attrs_json']).get('site')
        if site and not re.search(r'instagram|catchtable|facebook|naver', site):
            hosts.add(host_of(site))
    for r in conn.execute("SELECT host FROM domain_profile WHERE tier_guess='official'"):
        hosts.add(r['host'])
    return hosts


def structure_signature(html: bytes) -> str:
    tags = HTML_TAG.findall(html.decode('utf-8', 'ignore')[:300000])
    from collections import Counter
    c = Counter(t.lower() for t in tags)
    return ','.join(f'{t}:{min(9, n // 5)}' for t, n in sorted(c.items()) if t in ('article', 'p', 'table', 'li', 'h1', 'h2', 'section', 'main', 'div', 'script'))


def mentions_of(gaz: Gazetteer, text: str) -> dict:
    out: dict = {}
    for m in gaz.find(text):
        out[m['target_id']] = out.get(m['target_id'], 0) + 1
    return out


def process_doc(conn, doc: Doc, gaz: Gazetteer, *, tier: str, ctx: dict | None = None, via: str = '', ocr_conf=None, model_hook=None, now=None) -> dict:
    """Returns {'new': n, 'total': n, 'labels': {...}, 'alerts': [...]}. Never raises on empty extraction (that is information)."""
    now = now or utcnow()
    texts = [(doc.text, doc.published_at)]
    if doc.items:                                  # feed/sitemap: every item is its own short document with its own date
        texts = [(f"{i['title']}. {i['summary']}", i.get('date')) for i in doc.items if i.get('title')]
    recs_all = []
    off_hosts = official_hosts(conn)
    for text, pub in texts:
        recs = extract_text(text, gaz=gaz, host=doc.host, published_at=pub or doc.published_at, tier=tier, ctx=ctx, ocr_conf=ocr_conf or doc.ocr_conf,
                            official_hosts=off_hosts, content_hash=(doc.content_hash or '') + (pub or ''))
        resolve_extractions(conn, recs, gaz, doc.url, via or doc.url)
        recs_all.extend(recs)
    if model_hook:                                 # ambiguous only; output is epistemic=inference and never applied
        for r in list(recs_all):
            if needs_model(r):
                alt = model_hook(r)
                if alt:
                    alt.update({'epistemic': 'inference', 'extractor': 'model:hook', 'extraction_id': r['extraction_id'] + '_m'})
                    recs_all.append({**r, **alt})
    new = 0
    labels: dict = {}
    for r in recs_all:
        cur = conn.execute("""INSERT OR IGNORE INTO extraction(extraction_id,fetch_id,canonical_url,host,label,epistemic,chef_text,restaurant_text,org_text,role_text,date_text,tenure_from,tenure_to,
                              span_start,span_end,quote,extractor,extractor_version,ambiguity,resolved_chef_target,resolved_restaurant_target,resolved_org_target,resolve_note,published_at,created_at)
                              VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                           (r['extraction_id'], doc.fetch_id, doc.canonical_url, doc.host, r['label'], r['epistemic'], r.get('chef_text'), r.get('restaurant_text'), r.get('org_text'),
                            r.get('role_text'), r.get('date_iso') or r.get('date_text'), r.get('tenure_from'), r.get('tenure_to'), r['span_start'], r['span_end'], r['quote'],
                            r['extractor'], r['extractor_version'], r.get('ambiguity'), r.get('resolved_chef_target'), r.get('resolved_restaurant_target'), r.get('resolved_org_target'),
                            r.get('resolve_note'), r.get('published_at'), now))
        new += cur.rowcount
        labels[r['label']] = labels.get(r['label'], 0) + 1
    for tid, n in mentions_of(gaz, doc.text or ' '.join(f"{i['title']} {i['summary']}" for i in doc.items)).items():
        conn.execute('INSERT OR IGNORE INTO doc_target(fetch_id,target_id,mentions) VALUES (?,?,?)', (doc.fetch_id, tid, n))
    conn.commit()
    return {'new': new, 'total': len(recs_all), 'labels': labels, 'mentions': len(mentions_of(gaz, doc.text or ''))}


# ───────────── extractor health: a broken extractor must be detected, not silently produce empty output ─────────────
def check_extractor_health(conn, doc: Doc, n_extractions: int, raw_store, now=None) -> list[dict]:
    now = now or utcnow()
    alerts = []

    def alert(kind, detail, baseline, observed):
        aid = stable_id('alr', doc.host, doc.canonical_url, kind, doc.content_hash or '')
        cur = conn.execute('INSERT OR IGNORE INTO extractor_alert(alert_id,host,canonical_url,kind,detail,baseline_json,observed_json,created_at) VALUES (?,?,?,?,?,?,?,?)',
                           (aid, doc.host, doc.canonical_url, kind, detail, json.dumps(baseline), json.dumps(observed), now))
        if cur.rowcount:
            alerts.append({'kind': kind, 'detail': detail, 'url': doc.canonical_url})

    prev = conn.execute("""SELECT f.fetch_id, f.text_chars, f.raw_ref, f.content_hash,
                           (SELECT COUNT(*) FROM extraction e WHERE e.fetch_id=f.fetch_id) AS n_ext
                           FROM fetch_record f WHERE f.canonical_url=? AND f.outcome='ok' AND f.fetch_id<>? AND f.content_hash<>?
                           ORDER BY f.fetched_at DESC, f.rowid DESC LIMIT 1""", (doc.canonical_url, doc.fetch_id, doc.content_hash or '')).fetchone()
    if prev:
        cur_chars = len(doc.text or '')
        if prev['text_chars'] >= 600 and cur_chars < 0.25 * prev['text_chars']:
            alert('text_collapse', f"text fell {prev['text_chars']}->{cur_chars} chars on the same URL", {'chars': prev['text_chars']}, {'chars': cur_chars})
        if prev['n_ext'] >= 2 and n_extractions == 0:
            sig_changed = False
            try:
                if prev['raw_ref'] and prev['raw_ref'].endswith('.html'):
                    cur_raw = conn.execute('SELECT raw_ref FROM fetch_record WHERE fetch_id=?', (doc.fetch_id,)).fetchone()['raw_ref']
                    sig_changed = structure_signature(raw_store.read(prev['raw_ref'])) != structure_signature(raw_store.read(cur_raw))
            except Exception:
                pass
            alert('structure_change' if sig_changed else 'yield_drop',
                  f"previously {prev['n_ext']} labelled extractions on this URL, now 0" + (' and the page structure signature changed' if sig_changed else ''),
                  {'extractions': prev['n_ext']}, {'extractions': 0, 'structure_changed': sig_changed})
    # host-level: documents that mention our targets but never yield anything
    rows = conn.execute("""SELECT f.fetch_id, (SELECT COUNT(*) FROM extraction e WHERE e.fetch_id=f.fetch_id) n,
                           (SELECT COALESCE(SUM(mentions),0) FROM doc_target d WHERE d.fetch_id=f.fetch_id) m
                           FROM fetch_record f WHERE f.host=? AND f.outcome='ok' ORDER BY f.fetched_at DESC, f.rowid DESC LIMIT 6""", (doc.host,)).fetchall()
    from .net import is_search_host
    if len(rows) >= 6 and not is_search_host(doc.host) and all(r['m'] >= 2 and r['n'] == 0 for r in rows):
        alert('yield_drop', f'last 6 documents on {doc.host} mention targets but produced no labelled extraction', {}, {'docs': 6})
    return alerts


def fetch_regression_alert(conn, doc, now=None) -> dict | None:
    """A URL that used to give us a real page now answers 200 with an empty shell / login wall: the SITE (or our access) changed. Alert, don't just retry."""
    prev = conn.execute("SELECT text_chars FROM fetch_record WHERE canonical_url=? AND outcome='ok' ORDER BY fetched_at DESC, rowid DESC LIMIT 1", (doc.canonical_url,)).fetchone()
    if not prev or prev['text_chars'] < 300 or doc.outcome not in ('fail_empty', 'fail_login_page'):
        return None
    kind = 'login_wall' if doc.outcome == 'fail_login_page' else 'text_collapse'
    aid = stable_id('alr', doc.host, doc.canonical_url, kind, doc.fetch_id)
    conn.execute('INSERT OR IGNORE INTO extractor_alert(alert_id,host,canonical_url,kind,detail,baseline_json,observed_json,created_at) VALUES (?,?,?,?,?,?,?,?)',
                 (aid, doc.host, doc.canonical_url, kind, f"previously {prev['text_chars']} chars of text, now {doc.outcome} ({doc.error})", json.dumps({'chars': prev['text_chars']}),
                  json.dumps({'outcome': doc.outcome}), now or utcnow()))
    return {'kind': kind, 'url': doc.canonical_url}
