"""Candidate name matching and reversible redirects for sourced career assertions."""
import re
import unicodedata

from kg_daemon import claims
from kg_daemon.db import stable_id
from .net import host_of
from .targets import name_key

HANGUL = re.compile(r'[가-힣]{2,5}')
KANA = re.compile(r'[ぁ-ゖァ-ヺー・]+')
INITIALS = 'g kk n d tt r m b pp s ss _ j jj ch k t p h'.split()
VOWELS = 'a ae ya yae eo e yeo ye o wa wae oe yo u wo we wi yu eu ui i'.split()
FINALS = '_ k k k n n n t l k m l l l p l m p p t t ng t t k t p t'.split()
SURNAMES = {'류': ('ryu', 'yu'), '유': ('yu', 'yoo'), '이': ('lee', 'yi'), '김': ('kim',),
            '박': ('park', 'pak'), '정': ('jung', 'chung'), '최': ('choi',)}
KANA_ROWS = (
    ('アイウエオ', 'a i u e o'), ('カキクケコ', 'ka ki ku ke ko'),
    ('サシスセソ', 'sa shi su se so'), ('タチツテト', 'ta chi tsu te to'),
    ('ナニヌネノ', 'na ni nu ne no'), ('ハヒフヘホ', 'ha hi fu he ho'),
    ('マミムメモ', 'ma mi mu me mo'), ('ヤユヨ', 'ya yu yo'), ('ラリルレロ', 'ra ri ru re ro'),
    ('ワヲン', 'wa o n'), ('ガギグゲゴ', 'ga gi gu ge go'), ('ザジズゼゾ', 'za ji zu ze zo'),
    ('ダヂヅデド', 'da ji zu de do'), ('バビブベボ', 'ba bi bu be bo'), ('パピプペポ', 'pa pi pu pe po'),
)
KATAKANA = {c: value for chars, row in KANA_ROWS for c, value in zip(chars, row.split())}
KATAKANA.update({'リュ': 'ryu', 'テァ': 'tae', 'ファ': 'hwa', 'フィ': 'hwi', 'ウォ': 'wo'})


def candidate_keys(name):
    name = unicodedata.normalize('NFKC', name.strip())
    chunks = re.findall(r"[^\W\d_]+(?:[-’'][^\W\d_]+)*", name)
    keys = {name_key(name)}
    if len(chunks) in (2, 3):
        keys.update((name_key(chunks[-1] + ''.join(chunks[:-1])), name_key(''.join(chunks[1:]) + chunks[0])))
    compact = name_key(name)
    if HANGUL.fullmatch(compact):
        rendered = []
        for c in compact:
            initial, rem = divmod(ord(c) - 0xAC00, 588)
            vowel, final = divmod(rem, 28)
            rendered.append((INITIALS[initial] + VOWELS[vowel] + FINALS[final]).replace('_', ''))
        for surname in {rendered[0], *SURNAMES.get(compact[0], ())}:
            given = ''.join(rendered[1:])
            keys.update((surname + given, given + surname))
    if KANA.fullmatch(name):
        kana = ''.join(chr(ord(c) + 96) if 'ぁ' <= c <= 'ゖ' else c for c in name)
        parts = []
        for chunk in kana.replace('ー', '').split('・'):
            tokens = re.findall(r'.[ァィゥェォャュョ]|.', chunk)
            parts.append(''.join(KATAKANA.get(token, token) for token in tokens))
        for values in (parts, list(reversed(parts))):
            roman = ''.join(values)
            keys.update((roman, roman.replace('tehwan', 'taehwan')))
    return keys - {''}


def redirects(conn, kind):
    return dict(conn.execute('SELECT from_id,to_id FROM entity_redirect WHERE entity_type=? AND reversed_at IS NULL', (kind,)))


def canonical(mapping, value):
    while value in mapping:
        value = mapping[value]
    return value


def merge(conn, kind, old, new, evidence_id, reason, now):
    """Append a redirect. Existing facts and site IDs retain their original owners."""
    table, key = {'chef': ('chef', 'chef_id'), 'branch': ('branch', 'branch_id')}[kind]
    mapping = redirects(conn, kind)
    if old == new or canonical(mapping, old) == new:
        return
    if old in mapping or canonical(mapping, new) == old:
        raise ValueError('redirect would overwrite a decision or create a cycle')
    if conn.execute(f'SELECT COUNT(*) FROM {table} WHERE {key} IN (?,?)', (old, new)).fetchone()[0] != 2:
        raise ValueError('both redirect entities must exist')
    if not conn.execute('SELECT 1 FROM evidence WHERE evidence_id=? AND quote IS NOT NULL', (evidence_id,)).fetchone():
        raise ValueError('redirect requires quoted evidence')
    redirect_id = stable_id('redirect', kind, old, new, evidence_id, now)
    conn.execute('INSERT INTO entity_redirect VALUES (?,?,?,?,?,?,?,NULL)',
                 (redirect_id, kind, old, new, evidence_id, reason, now))
    return redirect_id


def reverse(conn, redirect_id, now):
    return conn.execute('UPDATE entity_redirect SET reversed_at=? WHERE redirect_id=? AND reversed_at IS NULL',
                        (now, redirect_id)).rowcount


def branch_candidates(conn, name, gaz):
    key = name_key(name)
    ids = {r[0] for r in conn.execute('''SELECT b.branch_id FROM branch b JOIN restaurant r USING(restaurant_id)
        LEFT JOIN michelin_branch_location l USING(branch_id)
        WHERE r.name=? COLLATE NOCASE OR r.name_en=? COLLATE NOCASE OR replace(l.name_key,' ','')=?
           OR EXISTS (SELECT 1 FROM alias a WHERE a.entity_type='restaurant' AND a.entity_id=r.restaurant_id
                       AND a.kind<>'legacy_id' AND a.alias=? COLLATE NOCASE)
           OR EXISTS (SELECT 1 FROM alias a WHERE a.entity_type='branch' AND a.entity_id=b.branch_id
                       AND a.kind<>'legacy_id' AND a.alias=? COLLATE NOCASE)''', (name, name, key, name, name))}
    for match in gaz.lookup(name):
        if match['kind'] == 'restaurant':
            ids.update(r[0] for r in conn.execute('SELECT branch_id FROM target_entity WHERE target_id=? AND branch_id IS NOT NULL',
                                                 (match['target_id'],)))
    return ids


def resolve_branch(conn, name, quote, gaz, focus, publisher_group, source_url, now, *, create=True, city=None, country=None):
    candidates = branch_candidates(conn, name, gaz)
    mapping = redirects(conn, 'branch')
    candidates = {canonical(mapping, branch) for branch in candidates}
    if candidates:
        located, scoped, established = set(), set(), set()
        for branch in candidates:
            row = conn.execute("""SELECT b.city,l.address,p.publisher_group FROM branch b
                LEFT JOIN michelin_branch_location l USING(branch_id)
                LEFT JOIN career_branch_context p USING(branch_id) WHERE branch_id=?""", (branch,)).fetchone()
            official = conn.execute("""SELECT 1 FROM target_entity t JOIN official_site s USING(target_id)
                WHERE t.branch_id=? AND s.host=? AND s.verified=1 AND s.basis IN ('seed','article_link')""",
                                    (branch, host_of(source_url))).fetchone()
            if city and row['city'] and name_key(city) != name_key(row['city']):
                continue
            if branch in focus or official or any(value and _located(quote, value) for value in (row['city'], row['address'])):
                located.add(branch)
            if row['publisher_group'] is None:
                established.add(branch)
            elif row['publisher_group'] == publisher_group:
                scoped.add(branch)
        choices = located or established or scoped
        if len(choices) == 1:
            return next(iter(choices)), choices
        if choices:
            return None, choices
    if not create:
        return None, candidates
    # Until a verified branch is identified, scope unknown locations to the
    # originating publisher. Identical brand or trade names can occur worldwide.
    rid = stable_id('rst', 'career', name_key(name), name_key(city) if city else publisher_group)
    bid = stable_id('br', rid)
    conn.execute('INSERT OR IGNORE INTO restaurant(restaurant_id,name,created_at) VALUES (?,?,?)', (rid, name, now))
    conn.execute('INSERT OR IGNORE INTO branch(branch_id,restaurant_id,city,country_code,created_at) VALUES (?,?,?,?,?)',
                 (bid, rid, city, country, now))
    conn.execute('INSERT OR IGNORE INTO career_branch_context VALUES (?,?)', (bid, publisher_group))
    return bid, {bid}


def _located(text, label):
    return bool(re.search(r'(?<!\w)' + re.escape(label) + r'(?!\w)', text, re.I))


def chef_candidates(conn, names, gaz):
    keys = set().union(*(candidate_keys(name) for name in names))
    if not keys:
        return set()
    q = ','.join('?' for _ in keys)
    ids = {r[0] for r in conn.execute(f'SELECT chef_id FROM chef_name_key WHERE name_key IN ({q})', sorted(keys))}
    for name in names:
        ids.update(r[0] for r in conn.execute('''SELECT chef_id FROM chef WHERE display_name=? COLLATE NOCASE
            OR display_name_en=? COLLATE NOCASE UNION SELECT entity_id FROM alias
            WHERE entity_type='chef' AND kind<>'legacy_id' AND alias=? COLLATE NOCASE''', (name, name, name)))
        for match in gaz.lookup(name):
            if match['kind'] == 'chef':
                ids.update(r[0] for r in conn.execute('SELECT chef_id FROM target_entity WHERE target_id=?', (match['target_id'],)))
    # Seed chefs predate the candidate index. Their small curated set is also a source of candidate spellings.
    for key in keys:
        for match in gaz.folded_aliases.get(key, ()):
            if match['kind'] == 'chef' and name_key(match['alias']) in keys:
                ids.update(r[0] for r in conn.execute('SELECT chef_id FROM target_entity WHERE target_id=?', (match['target_id'],)))
    return ids


def _same_stint(conn, ids, branch, row, meta):
    if not row['role_id'] or not (row['valid_from'] or row['valid_to']):
        return set()
    matches = set()
    for chef in ids:
        for stint in conn.execute("""SELECT * FROM career_assertion WHERE chef_id=? AND branch_id=? AND state='source_claim'
                AND role_id=? AND independent_eligible=1""", (chef, branch, row['role_id'])):
            periods = [(stint[k], row[k]) for k in ('valid_from', 'valid_to') if stint[k] and row[k]]
            if (meta['independent_eligible'] and stint['origin_group'] != meta['origin_group']
                    and stint['publisher_group'] != meta['publisher_group'] and stint['content_hash'] != meta['content_hash']
                    and claims.independent_sources([(stint['canonical_url'], stint['quote']), (meta['canonical_url'], row['quote'])]) == 2
                    and periods and all(a == b for a, b in periods) and stint['relation'] == row['relation']):
                matches.add(chef)
    return matches


def resolve_chef(conn, row, branch, gaz, meta, evidence_id, now):
    name, bilingual = row['chef_text'], row.get('bilingual_name')
    names = [name, bilingual] if bilingual else [name]
    ids = chef_candidates(conn, names, gaz)
    mapping = redirects(conn, 'chef')
    canonical_ids = {canonical(mapping, chef) for chef in ids}
    matched = _same_stint(conn, ids | canonical_ids, branch, row, meta)
    matched = {canonical(mapping, chef) for chef in matched}
    exact, local = set(), set()
    for chef in ids | canonical_ids:
        spellings = {name_key(n) for n in conn.execute(
            'SELECT display_name,display_name_en FROM chef WHERE chef_id=?', (chef,)).fetchone() if n}
        spellings.update(name_key(r[0]) for r in conn.execute(
            "SELECT alias FROM alias WHERE entity_id=? AND entity_type='chef' AND kind<>'legacy_id'", (chef,)))
        if not ({name_key(n) for n in names} & spellings):
            continue
        resolved = canonical(mapping, chef)
        exact.add(resolved)
        if conn.execute('''SELECT 1 FROM employment WHERE chef_id=? AND branch_id=? AND state<>'rejected' LIMIT 1''',
                        (chef, branch)).fetchone():
            local.add(resolved)
    if len(canonical_ids) > 1 and bilingual:
        left, right = ({canonical(mapping, chef) for chef in chef_candidates(conn, [n], gaz)} for n in names)
        if len(left | right) == 2 and len(local | matched) == 1:
            chosen = next(iter(local | matched))
            other = next(iter((left | right) - {chosen}))
            # Separate homonyms with established careers require human review.
            if not conn.execute("""SELECT 1 FROM employment WHERE chef_id=? AND branch_id<>?
                    AND state<>'rejected' LIMIT 1""", (other, branch)).fetchone():
                merge(conn, 'chef', other, chosen, evidence_id, 'explicit_bilingual_name', now)
                return other, canonical_ids
        return None, canonical_ids
    if len(canonical_ids) > 1:
        return None, canonical_ids
    if local or matched or bilingual and exact:
        chef = next(iter(local or matched or exact))
        if name_key(name) not in {name_key(r) for r in conn.execute(
                'SELECT display_name, display_name_en FROM chef WHERE chef_id=?', (chef,)).fetchone() if r}:
            # Keep the sourced spelling on its own ID, so reversing the link also separates its evidence.
            if name_key(name) in {name_key(r[0]) for r in conn.execute(
                    "SELECT alias FROM alias WHERE entity_id=? AND entity_type='chef' AND claim_id IS NOT NULL", (chef,))}:
                return chef, canonical_ids
            variant = _new_chef(conn, name, branch, now)
            if variant != chef and variant not in mapping and not conn.execute(
                    'SELECT 1 FROM entity_redirect WHERE entity_type=\'chef\' AND from_id=? AND to_id=? AND reversed_at IS NOT NULL',
                    (variant, chef)).fetchone():
                merge(conn, 'chef', variant, chef, evidence_id, 'explicit_bilingual_name' if bilingual else 'independent_same_stint', now)
            return variant, canonical_ids
        return chef, canonical_ids
    if canonical_ids:
        # Keep the plausible identity in review, then re-evaluate against the completed bio.
        return None, canonical_ids
    return _new_chef(conn, name, branch, now), set()


def _new_chef(conn, name, branch, now):
    chef = stable_id('chf', 'career', name_key(name), branch)
    conn.execute('INSERT OR IGNORE INTO chef(chef_id,display_name,created_at) VALUES (?,?,?)', (chef, name, now))
    index_name(conn, chef, name)
    return chef


def index_name(conn, chef, name):
    conn.executemany('INSERT OR IGNORE INTO chef_name_key VALUES (?,?)',
                     [(key, chef) for key in sorted(candidate_keys(name))])
