"""Target list = regions x cuisines x restaurants x chefs, each with KR/EN names and aliases. Initial list comes from data/seed.json
(read-only); the bot adds `discovered` targets. Matching is EXACT on normalised aliases only (no fuzzy merge)."""
import json
import re
import unicodedata

from kg_daemon.db import stable_id, utcnow

PLACEHOLDER = re.compile(r'이름 미기재|미확인|미기재')
CHEF_SPLIT = re.compile(r'\s*(?:[·&+/]|\s및\s)\s*')
PAREN = re.compile(r'\([^)]*\)')


def norm(s: str) -> str:
    s = unicodedata.normalize('NFKC', s or '').lower()
    return re.sub(r'[\s\-\'’`.·]+', '', s)


def romanization_variants(en: str | None) -> set[str]:
    """'Eom Tae-cheol' -> {'Eom Tae-cheol','Eom Taecheol','Taecheol Eom','Tae-cheol Eom'}. Deterministic, order/hyphen only."""
    if not en:
        return set()
    en = PAREN.sub('', en).strip()
    out = {en, en.replace('-', '')}
    parts = en.split()
    if len(parts) == 2 and not re.search(r'[&+]', en):
        out |= {f'{parts[1]} {parts[0]}', f'{parts[1]} {parts[0]}'.replace('-', '')}
    return {o.strip() for o in out if len(o.strip()) >= 4}


def tid(kind: str, key: str) -> str:
    return stable_id('tgt', kind, norm(key))


def upsert_target(conn, kind, name_ko, name_en=None, origin='seed', via=None, status='active', attrs=None, aliases=(), now=None) -> str:
    key = name_ko or name_en
    t = tid(kind, key)
    conn.execute("""INSERT OR IGNORE INTO target(target_id,kind,name_ko,name_en,origin,discovered_via,discovered_at,status,attrs_json) VALUES (?,?,?,?,?,?,?,?,?)""",
                 (t, kind, name_ko, name_en, origin, via, now or utcnow(), status, json.dumps(attrs or {}, ensure_ascii=False, sort_keys=True)))
    for a, lang in [(name_ko, 'ko'), (name_en, 'en'), *[(x, 'en') for x in romanization_variants(name_en)], *[(x, 'x') for x in aliases]]:
        if a and len(a) >= 2:
            conn.execute('INSERT OR IGNORE INTO target_alias(target_id,alias,lang) VALUES (?,?,?)', (t, a, lang))
    return t


def chef_names(raw_ko: str | None, raw_en: str | None) -> list[tuple[str, str | None]]:
    """'김범주 (파티시에)' / '박지현·윤송이 (파티시에)' / '김성일·차도영' -> [(ko, en)]. Unnamed placeholders are skipped, never invented."""
    if not raw_ko or PLACEHOLDER.search(raw_ko):
        return []
    kos = [PAREN.sub('', p).strip() for p in CHEF_SPLIT.split(PAREN.sub('', raw_ko).replace('(+', '·(+')) if p.strip()]
    kos = [k for k in kos if re.fullmatch(r'[가-힣]{2,4}', k)]
    ens = [PAREN.sub('', e).strip() for e in re.split(r'\s*[&+]\s*', raw_en or '') if e.strip()] if raw_en else []
    return [(k, ens[i] if len(ens) == len(kos) else (ens[0] if len(kos) == 1 and ens else None)) for i, k in enumerate(kos)]


def load_seed_targets(conn, seed: dict, now=None) -> dict:
    n = {'restaurant': 0, 'chef': 0, 'region': 0, 'cuisine': 0}
    for k in seed.get('kitchens', []):
        upsert_target(conn, 'restaurant', k['name'], k.get('en') or k.get('name_en'), attrs={'role': 'origin_kitchen', 'seed_id': k['id']}, now=now)
        n['restaurant'] += 1
    regions, cuisines = set(), set()
    for r in seed.get('restaurants', []):
        official = r.get('official_source_url') or ''
        attrs = {'seed_id': r['id'], 'area': r.get('area'), 'cuisine': r.get('cuisine'), 'kitchen': r.get('kitchen'), 'opened': r.get('opened'), 'site': r.get('url'),
                 'role': r.get('role'), 'chef': r.get('chef')}
        rt = upsert_target(conn, 'restaurant', r['name'], r.get('name_en'), attrs=attrs, now=now)
        n['restaurant'] += 1
        for ko, en in chef_names(r.get('chef'), r.get('chef_en')):
            ct = upsert_target(conn, 'chef', ko, en, attrs={'restaurants': [r['id']], 'origin_kitchen': r.get('kitchen')}, now=now)
            row = conn.execute('SELECT attrs_json FROM target WHERE target_id=?', (ct,)).fetchone()
            a = json.loads(row['attrs_json'])
            if r['id'] not in a.get('restaurants', []):
                a.setdefault('restaurants', []).append(r['id'])
                conn.execute('UPDATE target SET attrs_json=? WHERE target_id=?', (json.dumps(a, ensure_ascii=False, sort_keys=True), ct))
            n['chef'] += 1
        if r.get('area'):
            regions.add(re.split(r'[ (/]', r['area'])[0])
        if r.get('cuisine'):
            cuisines.add(r['cuisine'])
    for reg in sorted(regions):
        upsert_target(conn, 'region', reg, now=now)
    for c in sorted(cuisines):
        upsert_target(conn, 'cuisine', c, now=now)
    conn.commit()
    n['region'], n['cuisine'] = len(regions), len(cuisines)
    return n


class Gazetteer:
    """alias -> [target rows]; longest-match scan over text. Built from DB so discovered targets join immediately."""

    def __init__(self, conn, kinds=('chef', 'restaurant')):
        self.conn = conn
        self.by_alias: dict[str, list[dict]] = {}
        rows = conn.execute(f"""SELECT a.alias, t.target_id, t.kind, t.name_ko, t.name_en, t.origin, t.status, t.attrs_json FROM target_alias a JOIN target t USING(target_id)
                                WHERE t.kind IN ({','.join('?' * len(kinds))}) AND t.status<>'rejected'""", kinds).fetchall()
        for r in rows:
            self.by_alias.setdefault(norm(r['alias']), []).append(dict(r))
        self.keys = sorted(self.by_alias, key=len, reverse=True)
        self._ko = [k for k in self.keys if re.search(r'[가-힣]', k)]
        self._en = [k for k in self.keys if not re.search(r'[가-힣]', k)]

    def lookup(self, text: str) -> list[dict]:
        return self.by_alias.get(norm(text), [])

    def find(self, sentence: str) -> list[dict]:
        """Mentions with offsets. Korean: substring with non-Hangul-or-particle boundary check on the left. Latin: word-boundary."""
        out, taken = [], []
        # per-character NFKC that keeps offsets aligned with the caller's sentence ('…' -> '...' would shift every later span)
        low = ''.join(n if len(n := unicodedata.normalize('NFKC', ch)) == 1 else ch for ch in sentence)
        for alias in self.keys:
            if len(alias) < 2:
                continue
            pat = r'\s?'.join(map(re.escape, alias)) if re.search(r'[가-힣]', alias) else r'(?<![A-Za-z0-9])' + r'[\s\-\'’.]*'.join(map(re.escape, alias)) + r'(?![A-Za-z0-9])'
            for m in re.finditer(pat, low, re.I):
                s, e = m.span()
                if re.search(r'[가-힣]', alias) and s > 0 and re.match(r'[가-힣]', low[s - 1]) and not re.match(r'(셰프|쉐프)', low[s - 1:s + 1]):
                    # '…레스토랑 모수' fine; '새모수' not: require a non-Hangul or space on the left, except short chef names glued to a title/particle
                    if len(alias) < 3:
                        continue
                if any(not (e <= a or s >= b) for a, b in taken):
                    continue
                taken.append((s, e))
                for ent in self.by_alias[alias]:
                    out.append({**ent, 'start': s, 'end': e, 'surface': low[s:e]})
        return sorted(out, key=lambda x: x['start'])
