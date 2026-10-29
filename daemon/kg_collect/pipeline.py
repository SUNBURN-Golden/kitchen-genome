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
def check_extractor_health(conn, doc: Doc, n_extractions: int, raw_store, now=None, gaz: Gazetteer | None = None) -> list[dict]:
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
    # host-level: prose documents that mention our targets (outside site boilerplate) but never yield anything — ONE open alert per host
    alerts.extend(host_yield_check(conn, doc.host, raw_store, gaz=gaz, now=now, trigger_url=doc.canonical_url))
    return alerts


HOST_WINDOW = 6
NON_PROSE_METHODS = ('sitemap', 'feed', 'api')         # URL lists / JSON: never carry labelled sentences (sitemap items have no title)
HOST_YIELD_PREFIX = 'last 6 documents on '
_CTX_CACHE: dict = {}
CTX_CHARS = 24


def _mention_contexts(conn, raw_store, gaz, fetch_id, text_ref) -> list[tuple[str, str, str]] | None:
    """[(target_id, left context, right context)] (24 chars each side incl. the mention, whitespace-normalised) per gazetteer mention."""
    key = (id(gaz), fetch_id)
    if key not in _CTX_CACHE:
        if len(_CTX_CACHE) > 5000:
            _CTX_CACHE.clear()
        try:
            text = raw_store.read_text(text_ref) if text_ref else ''
        except Exception:  # noqa: BLE001 — a missing raw file must not break extraction; fall back to doc_target counts
            text = None
        ws = lambda x: re.sub(r'\s+', ' ', x)  # noqa: E731
        # a side cut off by the start/end of the text is not evidence of anything (None): only full-width contexts can recur
        _CTX_CACHE[key] = None if text is None else [
            (m['target_id'], 'L' + ws(text[m['start'] - CTX_CHARS:m['end']]) if m['start'] >= CTX_CHARS else None,
             'R' + ws(text[m['start']:m['end'] + CTX_CHARS]) if m['end'] + CTX_CHARS <= len(text) else None) for m in gaz.find(text)]
    return _CTX_CACHE[key]


def host_yield_window(conn, host, raw_store=None, gaz=None) -> dict:
    """Last HOST_WINDOW prose docs on the host. A mention whose surrounding text recurs in >= half of the window is site boilerplate
    (sidebar / most-read / related-articles widgets), not something the article says, so it does not count. Without gaz/raw_store the
    stored doc_target counts are used as before."""
    rows = conn.execute(f"""SELECT f.fetch_id, f.canonical_url, f.text_ref, (SELECT COUNT(*) FROM extraction e WHERE e.fetch_id=f.fetch_id) n,
                           (SELECT COALESCE(SUM(mentions),0) FROM doc_target d WHERE d.fetch_id=f.fetch_id) m
                           FROM fetch_record f WHERE f.host=? AND f.outcome='ok' AND f.method NOT IN ({','.join('?' * len(NON_PROSE_METHODS))})
                           ORDER BY f.fetched_at DESC, f.rowid DESC LIMIT ?""", (host, *NON_PROSE_METHODS, HOST_WINDOW)).fetchall()
    docs = [{'fetch_id': r['fetch_id'], 'url': r['canonical_url'], 'n': r['n'], 'm': r['m']} for r in rows]
    if gaz is not None and raw_store is not None and docs:
        ctxs = [_mention_contexts(conn, raw_store, gaz, r['fetch_id'], r['text_ref']) for r in rows]
        from collections import Counter
        spread = Counter(c for cs in ctxs if cs for c in {side for x in cs for side in x[1:] if side})
        for d, cs in zip(docs, ctxs):
            if cs is not None:      # boilerplate = the text on EITHER side of the mention recurs in >= half of the window's documents
                d['m'] = sum(1 for _, lc, rc in cs if max(spread[lc] if lc else 0, spread[rc] if rc else 0) * 2 < len(docs))
    return {'docs': docs, 'silent': len(docs) >= HOST_WINDOW and all(d['m'] >= 2 and d['n'] == 0 for d in docs),
            'yielding': any(d['n'] > 0 for d in docs)}


def _open_host_alerts(conn, host):
    return conn.execute("""SELECT * FROM extractor_alert WHERE host=? AND kind='yield_drop' AND state='open'
                           AND (detail LIKE ? OR baseline_json LIKE '%"scope": "host"%') ORDER BY created_at, alert_id""", (host, HOST_YIELD_PREFIX + '%')).fetchall()


def host_yield_check(conn, host, raw_store=None, gaz=None, now=None, trigger_url=None) -> list[dict]:
    """Opens at most ONE host-level yield_drop alert per host (later evidence updates it), dedupes older per-document copies and
    auto-acknowledges them once the condition no longer holds (a recent prose doc yields again, or the 'mentions' were boilerplate/non-prose)."""
    from .net import is_search_host
    now = now or utcnow()
    if is_search_host(host):
        return []
    w = host_yield_window(conn, host, raw_store, gaz)
    existing = _open_host_alerts(conn, host)
    out = []
    obs = {'docs': len(w['docs']), 'urls': [d['url'] for d in w['docs']], 'checked_at': now}
    if w['silent']:
        keep = existing[0] if existing else None
        if keep is None:
            aid = stable_id('alr', host, 'host_yield_drop', now)
            conn.execute('INSERT OR IGNORE INTO extractor_alert(alert_id,host,canonical_url,kind,detail,baseline_json,observed_json,created_at) VALUES (?,?,?,?,?,?,?,?)',
                         (aid, host, trigger_url, 'yield_drop', f'{HOST_YIELD_PREFIX}{host} mention targets (outside site boilerplate) but produced no labelled extraction',
                          json.dumps({'scope': 'host'}), json.dumps(obs), now))
            out.append({'kind': 'yield_drop', 'detail': f'host {host}: last {HOST_WINDOW} prose docs mention targets, 0 extractions', 'url': trigger_url})
        else:
            conn.execute('UPDATE extractor_alert SET observed_json=? WHERE alert_id=?', (json.dumps({**obs, 'deduped': len(existing) - 1}), keep['alert_id']))
        dupes = existing[1:]
        reason = 'deduplicated: one open host-level yield_drop per host'
    else:
        dupes = existing
        reason = ('a recent prose document on the host yields labelled extractions again' if w['yielding'] else
                  'condition no longer holds: remaining target mentions are site boilerplate / non-prose documents, or fewer than '
                  f'{HOST_WINDOW} prose documents')
    for a in dupes:
        o = json.loads(a['observed_json'] or '{}')
        o.update({'auto_acknowledged_at': now, 'reason': reason})
        conn.execute("UPDATE extractor_alert SET state='acknowledged', observed_json=? WHERE alert_id=?", (json.dumps(o, ensure_ascii=False), a['alert_id']))
    return out


def reconcile_host_yield_alerts(conn, raw_store=None, gaz=None, now=None) -> dict:
    """Re-check every host that has open host-level yield_drop alerts (repairs the per-document alert flood written before this check existed)."""
    before = conn.execute("SELECT COUNT(*) FROM extractor_alert WHERE state='open'").fetchone()[0]
    hosts = [r['host'] for r in conn.execute("""SELECT DISTINCT host FROM extractor_alert WHERE kind='yield_drop' AND state='open'
                                                 AND (detail LIKE ? OR baseline_json LIKE '%"scope": "host"%')""", (HOST_YIELD_PREFIX + '%',))]
    for h in hosts:
        host_yield_check(conn, h, raw_store, gaz, now=now)
    conn.commit()
    after = conn.execute("SELECT COUNT(*) FROM extractor_alert WHERE state='open'").fetchone()[0]
    return {'hosts': len(hosts), 'open_before': before, 'open_after': after}


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
