"""Render public lineage evidence in the static restaurant pages."""
from html import escape
import re
import unicodedata
from urllib.parse import urlsplit


def evidence_html(row, heading, labels, source_label, english=False):
    items = []
    for ref in row.get('lineage_evidence', []):
        url, quote = ref.get('src'), ref.get('quote')
        if not url or not quote or re.search(r'[\s\x00-\x1f\x7f\\#]', url):
            continue
        try:
            parts = urlsplit(url)
            if parts.scheme not in ('http', 'https') or not parts.hostname or parts.username is not None:
                continue
        except ValueError:
            continue
        label = labels.get(ref.get('kind'), source_label)
        badge = (' <span class="tag ai">' + ('AI-reviewed' if english else 'AI 검토') + '</span>'
                 if ref.get('badge') == 'AI 검토' else '')
        items.append(f'<li>{escape(label)}{badge} · “{escape(quote)}” '
                     f'(<a href="{escape(url)}" rel="nofollow noopener" target="_blank">{escape(source_label)}</a>)</li>')
    return f'<h2>{escape(heading)}</h2><ul>{"".join(items)}</ul>' if items else ''


def _name_key(value):
    """Fold display spellings (case, spacing, hyphens) for identity comparison."""
    if not isinstance(value, str):
        return ''
    return ''.join(ch for ch in unicodedata.normalize('NFKC', value).casefold() if ch.isalnum())


def _same_chef(edge, row):
    """A curated scalar may be romanized while the generated edge prefers the sourced
    Korean spelling; compare canonical ids and folded spellings in both languages."""
    if edge.get('chef_name') == row.get('chef'):
        return True
    if edge.get('chef_id') and edge['chef_id'] == row.get('chef_id'):
        return True
    curated = {_name_key(row.get('chef')), _name_key(row.get('chef_en'))} - {''}
    generated = {_name_key(edge.get('chef_name')), _name_key(edge.get('chef_name_en'))} - {''}
    return bool(curated & generated)


def public_seed(seed):
    """Overlay generated public fields onto the curated build input."""
    result = dict(seed)
    projection = seed.get('career_projection', {})
    withdrawn = {row['id'] for row in seed.get('publication_rollbacks', [])}
    for kind in ('kitchens', 'restaurants'):
        rows = {row['id']: dict(row) for row in seed[kind]}
        for row in projection.get(kind, ()):
            if kind == 'restaurants' and row['id'] in withdrawn:
                continue
            if row['id'] in rows:
                curated = rows[row['id']]
                rows[row['id']] = {**row, **curated,
                                   **{key: row[key] for key in ('origins', 'career_affiliations', 'restaurant_id') if key in row}}
            else:
                rows[row['id']] = dict(row)
        result[kind] = list(rows.values())
    kitchens = {row['id'] for row in result['kitchens']}
    for row in result['restaurants']:
        original = row.get('kitchen')
        edges = [edge for edge in row.get('origins', ()) if edge['kitchen_id'] in kitchens]
        if original in kitchens and not any(edge['kitchen_id'] == original and
                _same_chef(edge, row) for edge in edges):
            edges.insert(0, {'kitchen_id': original, 'chef_id': None, 'chef_name': row.get('chef'),
                             'chef_name_en': row.get('chef_en'), 'role': row.get('role'), 'period': row.get('period'),
                             'lineage_status': row.get('lineage_status'), 'career_checking': row.get('career_checking', []),
                             'evidence': row.get('lineage_evidence', [])})
        row['origins'] = edges
        row['kitchen'] = original if original in kitchens else edges[0]['kitchen_id'] if edges else None
        if edges:
            row['type'] = 'alumni'
    canonical_paths = {f'/{kind}/{row["id"]}/' for kind, key in (('r', 'restaurants'), ('k', 'kitchens')) for row in result[key]}
    allowed = re.compile(r'/[rk]/[a-z0-9_-]+/', re.I)
    result['route_aliases'] = {old: target for old, target in projection.get('route_aliases', {}).items()
                               if allowed.fullmatch(old) and old not in canonical_paths and target in canonical_paths}
    return result


def is_alum(row, kitchen_id):
    return any(edge['kitchen_id'] == kitchen_id for edge in row['origins'])


def status(edge, english=False):
    checking = edge.get('career_checking') or []
    if edge.get('lineage_status') == 'verified' and not {'kitchen', 'chef'}.intersection(checking):
        return 'Source verified' if english else '출처 확인'
    if checking:
        return 'Checking' if english else '확인 중'
    return ''


def kitchen_crews(row, kitchen_id, english=False):
    names = []
    for edge in row['origins']:
        if edge['kitchen_id'] != kitchen_id:
            continue
        name = (edge.get('chef_name_en') if english else None) or edge.get('chef_name')
        if name:
            names.append(name)
    return ' · '.join(dict.fromkeys(names))


def kitchen_badge(row, kitchen_id, english=False):
    values = sorted({status(edge, english) for edge in row['origins'] if edge['kitchen_id'] == kitchen_id} - {''})
    return ''.join(f' <span class="tag">{escape(label)}</span>' for label in values)


def career_html(row, kitchens, english=False, *, role_labels=None):
    base = '/en' if english else ''
    role_labels = role_labels or {}
    items = []
    relations = ({'former': 'Former chef', 'current': 'Current chef', 'training': 'Training', 'opening': 'Opened'}
                 if english else {'former': '이전 근무', 'current': '재직', 'training': '수련', 'opening': '개업'})
    for edge in (*row.get('origins', ()), *row.get('career_affiliations', ())):
        if 'relation' not in edge:
            continue
        kid = edge.get('kitchen_id')
        kitchen = kitchens.get(kid)
        name = edge.get('chef_name_en') if english else None
        bits = [escape(name or edge.get('chef_name') or '')]
        if kitchen:
            kname = (kitchen.get('name_en') or kitchen.get('en')) if english else None
            bits.append(f'<a href="{base}/k/{escape(kid)}/">{escape(kname or kitchen["name"])}</a>')
        if relation := relations.get(edge['relation']):
            bits.append(relation)
        if edge.get('mentor_name'):
            name = (edge.get('mentor_name_en') if english else None) or edge['mentor_name']
            bits.append(('Mentor ' if english else '스승 ') + escape(name))
        label = status(edge, english)
        checking = 'Checking' if english else '확인 중'
        for key in ('role', 'period'):
            if edge.get(key):
                value = role_labels.get(edge[key], edge[key]) if key == 'role' else edge[key]
                bits.append(escape(value) + (f' <span class="tag">{checking}</span>'
                                                 if key in edge.get('career_checking', []) else ''))
        if label:
            bits.append(f'<span class="tag">{escape(label)}</span>')
        heading = 'Evidence' if english else '출처'
        evidence = evidence_html({'lineage_evidence': edge['evidence']}, heading, {}, heading, english)
        items.append(f'<li>{" · ".join(bits)}{evidence}</li>')
    heading = 'Career history' if english else '셰프 근무 이력'
    return f'<h2>{heading}</h2><ul>{"".join(items)}</ul>' if items else ''


def people_jsonld(row, kitchens, base, restaurant_id, english=False):
    if not row.get('career_affiliations') and not any('relation' in edge for edge in row['origins']):
        return None
    people = {}
    for edge in (*row['origins'], *row.get('career_affiliations', [])):
        if edge.get('lineage_status') != 'verified' or {'kitchen', 'chef'}.intersection(edge.get('career_checking', [])):
            continue
        key = edge.get('chef_id') or edge.get('chef_name')
        if not key:
            continue
        name = (edge.get('chef_name_en') if english else None) or edge.get('chef_name')
        person = people.setdefault(key, {'@type': 'Person', 'name': name, 'jobTitle': 'Chef' if english else '셰프'})
        kid = edge.get('kitchen_id')
        kitchen = kitchens.get(kid)
        if kitchen:
            name = ((kitchen.get('name_en') or kitchen.get('en')) if english else None) or kitchen['name']
            org = {'@type': 'Organization', 'name': name, 'url': f'{base}/k/{kid}/'}
            alumni = person.setdefault('alumniOf', [])
            if org not in alumni:
                alumni.append(org)
        elif edge.get('relation') == 'current' and not row.get('closed_at'):
            person['worksFor'] = {'@id': restaurant_id}
    # Preserve the structured fields supported by a curated scalar when a
    # separate reviewed career is added to the same restaurant page.
    curated = row.get('chef')
    checks = row.get('career_checking') or []
    has_reviewed = any(ref.get('badge') == 'AI 검토'
                       for edge in (*row['origins'], *row.get('career_affiliations', ()))
                       for ref in edge.get('evidence', ()))
    if curated and has_reviewed and not re.search('미확인|미기재|출신|팀', curated):
        key = next((edge['chef_id'] for edge in row.get('career_affiliations', ()) if _same_chef(edge, row)), curated)
        name = (row.get('chef_en') if english else None) or curated
        person = people.setdefault(key, {'@type': 'Person', 'name': name, 'jobTitle': 'Chef' if english else '셰프'})
        if not {'chef', 'kitchen'}.intersection(checks) and not row.get('closed_at'):
            person['worksFor'] = {'@id': restaurant_id}
        kid = row.get('kitchen')
        if kid in kitchens and row.get('lineage_status') == 'verified' and 'kitchen' not in checks:
            kitchen = kitchens[kid]
            org = {'@type': 'Organization', 'name': (kitchen.get('name_en') or kitchen.get('en') or kitchen['name'])
                   if english else kitchen['name'], 'url': f'{base}/k/{kid}/'}
            if org not in person.setdefault('alumniOf', []):
                person['alumniOf'].append(org)
    return list(people.values())


def alias_pages(seed, base, english=False):
    prefix = '/en' if english else ''
    label = 'Open the current page' if english else '현재 페이지 보기'
    for old, new in seed['route_aliases'].items():
        target = prefix + new
        yield prefix + old, (
            f'<!doctype html><html lang="{"en" if english else "ko"}"><meta charset="utf-8">'
            f'<title>{label}</title><link rel="canonical" href="{escape(base + target)}">'
            f'<meta http-equiv="refresh" content="0; url={escape(target)}">'
            f'<a href="{escape(target)}">{label}</a></html>')
