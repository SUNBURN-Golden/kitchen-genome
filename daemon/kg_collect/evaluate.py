"""Source evaluator: trial-fetch a NEW domain, score it, promote or deprioritise, and grow the registry.
Scores (0-1): relevance (does it talk about our targets/food-industry facts), novelty (is the information new vs what we already hold),
extractability (do our rules get labelled facts out of it), originality (original vs repost/syndication), permission (robots, login, ToS list).
An excluded host is blocked. Korean press robots permission contributes to its source score."""
import json
import os
import re

import yaml
from kg_daemon.db import ROOT, utcnow

from .net import CFG, PoliteNet, host_of, is_banned
from .pipeline import mentions_of
from .targets import Gazetteer

W = {'relevance': 0.30, 'novelty': 0.20, 'extractability': 0.25, 'originality': 0.15, 'permission': 0.10}
PROMOTE_AT, DEPRIORITIZE_AT, MIN_TRIAL_OK, TRIAL_SAMPLE = 0.50, 0.30, 2, 3
FOOD_CUES = re.compile(r'셰프|쉐프|레스토랑|오마카세|파인다이닝|미쉐린|미슐랭|블루리본|수셰프|주방|오픈|폐업|이전|출신')
DISCOVERED_REGISTRY = os.path.join(ROOT, 'sources', 'registry.discovered.yaml')
SHINGLE = 6
REPOST_THRESHOLD = 0.55
REEVALUATE_AFTER_DAYS = 7   # an unchanged host is re-scored (and its robots.txt re-read) at most this often


def _stale(evaluated_at: str | None, now: str, days: int = REEVALUATE_AFTER_DAYS) -> bool:
    if not evaluated_at:
        return True
    import datetime as _dt
    f = '%Y-%m-%dT%H:%M:%SZ'
    try:
        return (_dt.datetime.strptime(now, f) - _dt.datetime.strptime(evaluated_at, f)).days >= days
    except ValueError:
        return True


def shingles(text: str) -> set:
    t = re.sub(r'\s+', '', text)
    return {t[i:i + SHINGLE] for i in range(0, max(0, len(t) - SHINGLE), 3)} if len(t) > 60 else set()


def jaccard(a: set, b: set) -> float:
    return len(a & b) / len(a | b) if a and b else 0.0


def is_repost(conn, host: str, text: str, raw_store, threshold: float = REPOST_THRESHOLD) -> bool:
    """Near-duplicate of a document we already hold from ANOTHER host (syndication/repost)."""
    sh = shingles(text[:3000])
    if not sh:
        return False
    for r in conn.execute("SELECT text_ref FROM fetch_record WHERE host<>? AND outcome='ok' AND text_ref IS NOT NULL ORDER BY fetched_at DESC LIMIT 400", (host,)):
        try:
            if jaccard(sh, shingles(raw_store.read_text(r['text_ref'])[:3000])) >= threshold:
                return True
        except OSError:
            continue
    return False


def guess_tier(host: str, jsonld_types: list, official: set, has_dates: bool) -> str:
    if host in official:
        return 'official'
    if re.search(r'(tistory|blog\.|brunch|medium\.com|velog|wordpress)', host):
        return 'specialist_blog'
    if any('Restaurant' in t or 'LocalBusiness' in t for t in jsonld_types):
        return 'official'
    if 'wikipedia.org' in host:
        return 'specialist_blog'
    if has_dates or any('Article' in t for t in jsonld_types) or re.search(r'news|press|times|daily|journal|media', host):
        return 'press'
    return 'search_lead'


def ensure_profile(conn, host: str, in_registry: bool = False, now=None):
    conn.execute('INSERT OR IGNORE INTO domain_profile(host,in_initial_registry,first_seen_at) VALUES (?,?,?)', (host, int(in_registry), now or utcnow()))


def _novel_fraction(conn, host: str) -> tuple[int, int]:
    """(# extractions from this host whose (label, chef, restaurant/org) triple is NOT already stated by any other host, # extractions)."""
    rows = conn.execute("SELECT label, COALESCE(resolved_chef_target,chef_text,'') c, COALESCE(resolved_restaurant_target,restaurant_text,'') r, COALESCE(resolved_org_target,org_text,'') o FROM extraction WHERE host=?", (host,)).fetchall()
    novel = 0
    for x in rows:
        other = conn.execute("""SELECT 1 FROM extraction WHERE host<>? AND label=? AND COALESCE(resolved_chef_target,chef_text,'')=? AND COALESCE(resolved_restaurant_target,restaurant_text,'')=?
                                AND COALESCE(resolved_org_target,org_text,'')=? LIMIT 1""", (host, x['label'], x['c'], x['r'], x['o'])).fetchone()
        novel += int(other is None)
    return novel, len(rows)


def score_host(conn, host: str, net: PoliteNet, raw_store, gaz: Gazetteer, official: set, now=None, in_initial: bool = False) -> dict:
    """Decide on a host from its TRIAL fetches (fetch_record rows) + what the rules extracted from them. No extra network except robots.txt."""
    now = now or utcnow()
    ensure_profile(conn, host, in_registry=in_initial, now=now)
    prof = conn.execute('SELECT * FROM domain_profile WHERE host=?', (host,)).fetchone()
    if prof['in_initial_registry'] or prof['state'] == 'blocked':
        return dict(prof)
    recs = conn.execute('SELECT * FROM fetch_record WHERE host=? ORDER BY fetched_at', (host,)).fetchall()
    # Nothing new fetched from this host since its last decision and the decision is recent: keep it. Re-scoring every known host each cycle
    # re-read ~100 robots.txt files a day and once ran the evaluate stage past the daily collect timeout (cycle 2026-11-01 abandoned).
    if (prof['evaluated_at'] and not _stale(prof['evaluated_at'], now) and prof['trial_fetches'] == len(recs)
            and not any((r['fetched_at'] or '') > prof['evaluated_at'] for r in recs)):
        return dict(prof)
    oks = [r for r in recs if r['outcome'] == 'ok']
    perm, robots = 1.0, None
    banned = is_banned(f'https://{host}/')
    if banned:
        perm = 0.0
    else:
        info = net.robots_info(f'https://{host}/')
        robots = info['allowed']
        perm = 0.0 if not info['allowed'] else (0.5 if (info['crawl_delay'] or 0) > 30 else 1.0)
    n = len(oks)
    rel = ext = rep = 0
    types = []
    has_dates = False
    for r in oks:
        text = raw_store.read_text(r['text_ref']) if r['text_ref'] else ''
        men = conn.execute('SELECT COALESCE(SUM(mentions),0) m FROM doc_target WHERE fetch_id=?', (r['fetch_id'],)).fetchone()['m']
        rel += int(men > 0 or len(FOOD_CUES.findall(text)) >= 3)
        ext += int(conn.execute('SELECT 1 FROM extraction WHERE fetch_id=? LIMIT 1', (r['fetch_id'],)).fetchone() is not None)
        rep += int(is_repost(conn, host, text, raw_store))
        has_dates = has_dates or bool(r['published_at'])
    novel, tot_ext = _novel_fraction(conn, host)
    scores = {'relevance': rel / n if n else 0.0, 'novelty': (novel / tot_ext) if tot_ext else 0.0, 'extractability': ext / n if n else 0.0,
              'originality': 1 - rep / n if n else 0.0, 'permission': perm}
    total = sum(W[k] * v for k, v in scores.items())
    tried = len(recs)
    if banned:
        state, reason = 'blocked', 'banned host'
    elif perm == 0:
        state, reason = 'deprioritized', 'Korean press robots permission unavailable or disallowed'
    elif n < MIN_TRIAL_OK and tried >= TRIAL_SAMPLE:
        state, reason = 'deprioritized', f'only {n}/{tried} trial fetches usable'
    elif tried >= 2 * TRIAL_SAMPLE and not (n >= MIN_TRIAL_OK and total >= PROMOTE_AT and ext >= 1):
        state, reason = 'deprioritized', f'no promotion after {tried} trial fetches (score {total:.2f}, {n} usable)'
    elif n >= MIN_TRIAL_OK and total >= PROMOTE_AT and ext >= 1:
        state, reason = 'promoted', f'score {total:.2f}>={PROMOTE_AT}; {ext}/{n} docs yielded labelled extractions'
    elif total < DEPRIORITIZE_AT and n >= 1:
        state, reason = 'deprioritized', f'score {total:.2f}<{DEPRIORITIZE_AT}'
    else:
        state, reason = 'trial', f'needs more samples ({n} usable of {tried}, score {total:.2f})'
    tier = guess_tier(host, types, official, has_dates)
    conn.execute("""UPDATE domain_profile SET state=?, tier_guess=?, robots_ok=?, trial_fetches=?, trial_ok=?, s_relevance=?, s_novelty=?, s_extractability=?,
                    s_originality=?, s_permission=?, score=?, yield_extractions=?, reason=?, evaluated_at=? WHERE host=?""",
                 (state, tier, None if robots is None else int(robots), tried, n, scores['relevance'], scores['novelty'], scores['extractability'], scores['originality'],
                  scores['permission'], total, tot_ext, reason, now, host))
    conn.commit()
    return {'host': host, 'state': state, 'score': round(total, 3), 'scores': {k: round(v, 3) for k, v in scores.items()}, 'tier': tier, 'reason': reason, 'tried': tried, 'ok': n}


def grow_registry(conn, path: str = DISCOVERED_REGISTRY, now=None) -> list[str]:
    """Write promoted domains into sources/registry.discovered.yaml in the same schema as sources/registry.yaml (machine-written; the curated file is untouched)."""
    rows = conn.execute("SELECT * FROM domain_profile WHERE state='promoted' AND in_initial_registry=0 ORDER BY host").fetchall()
    sources = {}
    for r in rows:
        sid = 'disc_' + re.sub(r'[^a-z0-9]+', '_', r['host'])
        tier = r['tier_guess'] if r['tier_guess'] in ('official', 'press', 'specialist_blog', 'search_lead') else 'search_lead'
        sources[sid] = {
            'name': f"Discovered: {r['host']}", 'kind': {'official': 'official_site', 'press': 'press', 'specialist_blog': 'community', 'search_lead': 'community'}[tier], 'tier': tier,
            'role': 'discovery' if tier in ('specialist_blog', 'search_lead') else 'fact_source', 'collect_permission': 'allowed_with_limits',
            'collectible_fields': ['title', 'url', 'published_at', 'short_quote'] if tier in ('specialist_blog', 'search_lead') else ['chef', 'restaurant', 'origin_kitchen', 'opening', 'closure', 'relocation', 'rename'],
            'refresh_cadence': 'weekly', 'retention_days': 90 if tier in ('official', 'press') else 30, 'fulltext_allowed': False,
            'unsupported_scope': ['login-walled pages', 'full article text (short quotes + structured facts only)'],
            'pause_on': ['http_429', 'http_403', 'http_503', 'robots_disallow'], 'adapter': 'kg_collect',
            'evaluation': {'score': round(r['score'] or 0, 3), 'relevance': round(r['s_relevance'] or 0, 2), 'novelty': round(r['s_novelty'] or 0, 2),
                           'extractability': round(r['s_extractability'] or 0, 2), 'originality': round(r['s_originality'] or 0, 2), 'evaluated_at': r['evaluated_at']}}
    doc = {'version': (now or utcnow())[:10] + '.discovered', 'generated_by': 'kg_collect.evaluate.grow_registry', 'sources': sources}
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', encoding='utf-8') as f:
        f.write('# MACHINE-WRITTEN by the source evaluator. Do not edit by hand: promoted domains only; curated sources live in registry.yaml.\n')
        yaml.safe_dump(doc, f, allow_unicode=True, sort_keys=True)
    return sorted(sources)
