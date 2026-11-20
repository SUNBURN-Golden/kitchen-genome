"""Materialize the public Korea-connected graph from a committed service snapshot."""
from collections import defaultdict, deque
import json

from kg_daemon import claims, ontology
from kg_daemon.db import stable_id
from . import evaluate, lineage_identity as identity
from .targets import name_key

KEY = 'career_projection'
OVERSEAS = {'atomix', 'atoboy', 'jua', 'moono', 'lysee', 'odre'}
PAST = {'former', 'training'}


def _career_rows(conn, branch=None):
    scope = 'AND a.branch_id=?' if branch else ''
    return conn.execute(f"""SELECT a.*,e.source_url AS src,e.quote AS verified_quote,c.status,d.body_fingerprint,
               COALESCE(emp.state,'active') AS employment_state
        FROM career_assertion a JOIN evidence e USING(evidence_id) JOIN claim c USING(claim_id)
        LEFT JOIN employment emp USING(employment_id)
        LEFT JOIN career_document d ON d.fetch_id=a.fetch_id AND d.extractor_version=a.extractor_version
        WHERE a.state='source_claim' AND c.status IN ('source_claim','verified')
          AND (emp.state IS NULL OR emp.state<>'rejected')
          AND (a.relation<>'current' OR (emp.state='active' AND emp.valid_to_kind='open'))
          AND a.quote=e.quote AND e.quote IS NOT NULL AND length(e.quote)<=200
          AND NOT EXISTS (SELECT 1 FROM career_document newer
            WHERE newer.fetch_id=a.fetch_id AND newer.rowid>d.rowid
              AND newer.extractor_version<>a.extractor_version)
          AND NOT EXISTS (SELECT 1 FROM conflict f WHERE f.state='open'
            AND (a.claim_id=f.claim_a OR a.claim_id=f.claim_b))
        {scope} ORDER BY a.assertion_id""", (branch,) if branch else ())


def _verified(rows):
    trusted = [(r, set(json.loads(r['body_fingerprint'] or '[]'))) for r in rows if r['independent_eligible']]
    for i, (left, left_shingles) in enumerate(trusted):
        for right, right_shingles in trusted[i + 1:]:
            if (left['publisher_group'] == right['publisher_group'] or left['origin_group'] == right['origin_group']
                    or left['content_hash'] == right['content_hash'] or left['quote_key'] == right['quote_key']
                    or evaluate.jaccard(left_shingles, right_shingles) >= evaluate.REPOST_THRESHOLD):
                continue
            if claims.independent_sources([(left['src'], left['quote']), (right['src'], right['quote'])]) == 2:
                return True
    return False


def _before(origin, current):
    if current['previous_branch_id'] == origin['branch_id'] and origin['canonical_url'] == current['canonical_url']:
        return True
    start = current['valid_from']
    end = origin['valid_to']
    return bool(start and end and ontology.date_range(end)[1] < ontology.date_range(start)[0])


def _compatible(left, right):
    if left['relation'] != right['relation']:
        return False
    if left['role_id'] and right['role_id'] and left['role_id'] != right['role_id']:
        return False
    for field in ('valid_from', 'valid_to'):
        if left[field] and right[field]:
            a, b = sorted((ontology.date_range(left[field]), ontology.date_range(right[field])))
            if a[1] < b[0]:
                return False
    return True


def _stint_groups(rows):
    groups = []
    # Specific rows anchor a stint so an undated source cannot join two tenures.
    for row in sorted(rows, key=lambda r: (not bool(r['valid_from'] or r['valid_to']),
                                          not bool(r['role_id']), r['assertion_id'])):
        possible = [group for group in groups if all(_compatible(row, peer) for peer in group)]
        if len(possible) == 1:
            possible[0].append(row)
        else:
            groups.append([row])
    return groups


def _refs(rows, kind, safe_url, public_quote):
    refs = {(r['src'], r['quote']) for r in rows if safe_url(r['src']) and public_quote(r['quote'])}
    return [dict(kind=kind, src=src, quote=quote) for src, quote in sorted(refs)]


def _summarize(rows, kind, people, role_labels, safe_url, public_quote):
    best = rows[0]
    verified = _verified(rows)
    checking = [] if verified else ['kitchen' if kind == 'lineage' else 'chef']
    role_rows = [r for r in rows if r['role_text'] and (
        r['role_id'] == best['role_id'] if best['role_id'] else r['role_text'] == best['role_text'])]
    period = ' – '.join(filter(None, (best['valid_from'], best['valid_to']))) or None
    period_rows = [r for r in rows if (r['valid_from'], r['valid_to']) == (best['valid_from'], best['valid_to'])]
    if best['role_text'] and not _verified(role_rows):
        checking.append('role')
    if period and not _verified(period_rows):
        checking.append('period')
    chef = people[best['chef_id']]
    return {'chef_id': best['chef_id'], 'chef_name': chef.get('name_ko') or chef['display_name'],
            'chef_name_en': chef['display_name_en'],
            'role': role_labels.get(best['role_id']) or best['role_text'], 'period': period,
            'relation': best['relation'], 'lineage_status': 'verified' if verified else 'unverified',
            'career_checking': checking, 'evidence': _refs(rows, kind, safe_url, public_quote)}


def _site_links(conn, seed, branch_redirects):
    by_kind = {key: defaultdict(list) for key in ('restaurants', 'kitchens')}
    curated = {kind: {r['id']: r for r in seed[kind]} for kind in by_kind}
    legacy = defaultdict(set)
    for row in conn.execute("""SELECT b.branch_id,a.alias FROM alias a JOIN branch b ON b.restaurant_id=a.entity_id
        WHERE a.entity_type='restaurant' AND a.kind='legacy_id' AND b.label='main'"""):
        legacy[row['alias']].add(row['branch_id'])
    for kind, rows in curated.items():
        for key, row in rows.items():
            choices = legacy[key]
            target = row.get('publication', {}).get('restaurant_target_id')
            if target:
                choices = {r[0] for r in conn.execute('SELECT branch_id FROM target_entity WHERE target_id=? AND branch_id IS NOT NULL', (target,))} or choices
            choices = {identity.canonical(branch_redirects, branch) for branch in choices}
            if len(choices) == 1:
                by_kind[kind][next(iter(choices))].append(key)
    return by_kind, curated


def _stable_site_id(conn, kind, branch, existing, reserved):
    if existing:
        return existing[0]
    site_id = conn.execute('SELECT site_id FROM projection_site_id WHERE branch_id=? AND kind=? ORDER BY site_id LIMIT 1',
                           (branch, kind)).fetchone()
    if site_id:
        return site_id[0]
    code = stable_id('kg', kind, branch).replace('_', '-')
    while code in reserved:
        code += '-g'
    conn.execute('INSERT INTO projection_site_id VALUES (?,?,?)', (branch, kind, code))
    return code


def _details(row, listing):
    fields = {k: row[k] for k in ('city', 'area') if row[k]}
    if row['country_code']:
        fields['country'] = row['country_code']
    if listing:
        fields.update(stars=listing['stars'], guide_year=listing['guide_year'], michelin_src=listing['source_url'],
                      michelin=f"미쉐린 {listing['stars']}스타")
    return fields


def _traverse(careers, roots):
    by_person = defaultdict(list)
    by_person_branch = defaultdict(list)
    for row in careers:
        by_person[row['chef_id']].append(row)
        by_person_branch[row['chef_id'], row['branch_id']].append(row)
    affiliations = defaultdict(list)
    found = defaultdict(list)
    visited = set()
    queue = deque()
    for row in careers:
        if row['branch_id'] in roots and row['relation'] in ('current', 'opening'):
            affiliations[row['branch_id'], row['chef_id']].append(row)
            queue.append(row)
    while queue:
        current = queue.popleft()
        mark = current['assertion_id']
        if mark in visited:
            continue
        visited.add(mark)
        for origin in by_person[current['chef_id']]:
            if (origin['branch_id'] != current['branch_id'] and origin['relation'] in PAST and _before(origin, current)):
                found[current['branch_id'], origin['branch_id'], origin['chef_id']].append(origin)
                queue.append(origin)
                # An explicit training relationship identifies the other career
                # allowed onto the path. Co-employment alone creates no link.
                if origin['mentor_id']:
                    for mentor in by_person_branch[origin['mentor_id'], origin['branch_id']]:
                        if mentor['relation'] != 'training':
                            queue.append(mentor)
    return found, affiliations


def _link_late_names(careers, branches):
    employers, mentors = defaultdict(set), defaultdict(set)
    for row in careers:
        if row.get('seed_anchor'):
            continue
        branch = row['branch_id']
        for name in (row['venue_text'], branches[branch]['name'], branches[branch]['name_en']):
            if name:
                employers[row['canonical_url'], row['chef_id'], name_key(name)].add(branch)
        mentors[branch, name_key(row['chef_text'])].add(row['chef_id'])
    for row in careers:
        if row.get('previous_venue_text') and not row['previous_branch_id']:
            matches = employers[row['canonical_url'], row['chef_id'], name_key(row['previous_venue_text'])]
            if len(matches) == 1:
                row['previous_branch_id'] = next(iter(matches))
        if row.get('mentor_text') and not row['mentor_id']:
            matches = mentors[row['branch_id'], name_key(row['mentor_text'])] - {row['chef_id']}
            if len(matches) == 1:
                row['mentor_id'] = next(iter(matches))


def materialize(conn, seed):
    from .global_queue import write
    from .site_publish import TOMBSTONES, _public_quote, safe_url

    active = conn.execute("SELECT 1 FROM sqlite_master WHERE name='career_assertion'").fetchone()
    if not active:
        return False
    old = seed.get(KEY, {})
    with write(conn):
        projection = _projection(conn, seed, TOMBSTONES, safe_url, _public_quote)
        if projection is None:
            # An empty/uninitialized replacement DB may be used for a build.
            return False
        if projection == old or not (projection or old):
            return False
        seed[KEY] = projection
    return True


def _projection(conn, seed, tombstones, safe_url, public_quote):
    branches = {r['branch_id']: dict(r) for r in conn.execute('SELECT b.*,r.name,r.name_en FROM branch b JOIN restaurant r USING(restaurant_id)')}
    if not branches:
        return None
    generated = {row['id'] for kind in ('restaurants', 'kitchens') for row in seed.get(KEY, {}).get(kind, []) if row['id'].startswith('kg-')}
    if generated:
        mapped = {r[0] for r in conn.execute('SELECT site_id FROM projection_site_id')}
        if generated - mapped:
            return None
    branch_redirects = identity.redirects(conn, 'branch')
    chef_redirects = identity.redirects(conn, 'chef')
    site, curated = _site_links(conn, seed, branch_redirects)
    reserved = {r['id'] for key in curated for r in seed[key]}
    blocked = {name_key(r['creation_event']['name']) for r in seed.get(tombstones, [])
               if r.get('creation_event', {}).get('name')}
    blocked.update(name_key(alias) for r in seed.get(tombstones, []) for alias in r.get('aliases', []))
    denied = {bid for bid, row in branches.items() if name_key(row['name']) in blocked}
    denied.update(identity.canonical(branch_redirects, bid) for bid in tuple(denied))
    roots = {identity.canonical(branch_redirects, branch) for branch, r in branches.items() if r['country_code'] == 'KR'}
    roots.update(branch for branch, ids in site['restaurants'].items() if OVERSEAS.intersection(ids))
    careers = []
    for row in _career_rows(conn):
        r = dict(row)
        if not safe_url(r['src']) or not public_quote(r['quote']):
            continue
        for field in ('branch_id', 'previous_branch_id'):
            r[field] = identity.canonical(branch_redirects, r[field])
        for field in ('chef_id', 'mentor_id'):
            r[field] = identity.canonical(chef_redirects, r[field])
        if r['branch_id'] not in denied:
            careers.append(r)
    # Seed opening dates are curated; they can anchor quote-backed earlier work.
    for row in conn.execute("""SELECT s.* FROM restaurant_status_event s JOIN claim c USING(claim_id)
        WHERE s.kind='open' AND s.state='active' AND s.by_chef_id IS NOT NULL AND s.effective_date IS NOT NULL
          AND c.created_by='migration:seed' AND c.status IN ('source_claim','verified')
          AND EXISTS (SELECT 1 FROM claim_evidence ce WHERE ce.claim_id=s.claim_id AND ce.stance='supports')"""):
        branch = identity.canonical(branch_redirects, row['branch_id'])
        if branch in site['restaurants']:
            careers.append(dict(assertion_id=row['event_id'], branch_id=branch, relation='opening', mentor_id=None,
                                chef_id=identity.canonical(chef_redirects, row['by_chef_id']), valid_from=row['effective_date'],
                                valid_to=None, previous_branch_id=None, canonical_url=None, seed_anchor=True))
    _link_late_names(careers, branches)
    links, affiliations = _traverse(careers, roots - denied)
    kitchens = {origin for _, origin, _ in links}
    displayed = ((roots | kitchens | {destination for destination, _, _ in links}) - denied)
    if not displayed:
        return {}
    people = {r['chef_id']: dict(r) for r in conn.execute('SELECT * FROM chef')}
    for alias in conn.execute("""SELECT a.*,c.status FROM alias a JOIN claim c USING(claim_id)
        WHERE a.entity_type='chef' AND a.lang IN ('ko','en','und-Latn') AND c.status IN ('source_claim','verified')
        ORDER BY a.created_at,a.alias_id"""):
        chef = people[identity.canonical(chef_redirects, alias['entity_id'])]
        if alias['lang'] in ('en', 'und-Latn') and not chef['display_name_en']:
            chef['display_name_en'] = alias['alias']
        if alias['lang'] == 'ko':
            chef.setdefault('name_ko', alias['alias'])
    roles = dict(conn.execute('SELECT role_id,label_ko FROM role'))
    listings = {}
    for row in conn.execute("""SELECT l.*,s.guide_year FROM current_guide_listing l JOIN guide_selection s USING(selection_id)
        WHERE l.source_kind IN ('michelin','michelin_announcement')
        ORDER BY guide_year DESC, l.observed_at DESC, l.selection_id"""):
        bid = identity.canonical(branch_redirects, row['branch_id'])
        if bid in displayed and safe_url(row['source_url']):
            listings.setdefault(bid, row)
    rids = {branch: _stable_site_id(conn, 'restaurant', branch, site['restaurants'][branch], reserved) for branch in sorted(displayed)}
    kids = {branch: _stable_site_id(conn, 'kitchen', branch, site['kitchens'][branch], reserved) for branch in sorted(kitchens)}
    origins = defaultdict(list)
    for (destination, origin, _), rows in sorted(links.items()):
        for group in _stint_groups({r['assertion_id']: r for r in rows}.values()):
            entry = _summarize(group, 'lineage', people, roles, safe_url, public_quote)
            entry['kitchen_id'] = kids[origin]
            origins[destination].append(entry)
    jobs = defaultdict(list)
    for (branch, _), rows in sorted(affiliations.items()):
        sourced = [r for r in rows if not r.get('seed_anchor')]
        for group in _stint_groups(sourced):
            jobs[branch].append(_summarize(group, 'affiliation', people, roles, safe_url, public_quote))

    korean_names = {}
    for alias in conn.execute("""SELECT a.*,c.status FROM alias a JOIN claim c USING(claim_id)
        WHERE a.entity_type='branch' AND a.lang='ko' AND c.status IN ('source_claim','verified')
        ORDER BY a.created_at,a.alias_id"""):
        branch = identity.canonical(branch_redirects, alias['entity_id'])
        korean_names.setdefault(branch, alias['alias'])

    def node(branch, site_id, kind):
        row = branches[branch]
        out = {'id': site_id, **_details(row, listings.get(branch))}
        if site_id not in curated[kind]:
            name, localized = row['name'], korean_names.get(branch)
            if localized and row['country_code'] != 'KR' and name_key(localized) != name_key(name):
                name += f' ({localized})'
            out.update(name=name, name_en=row['name_en'] or row['name'])
        return out

    restaurants = []
    for branch in sorted(rids, key=rids.get):
        ids = site['restaurants'][branch] or [rids[branch]]
        for rid in ids:
            r = node(branch, rid, 'restaurants')
            r['origins'] = sorted(origins[branch], key=lambda e: (e['lineage_status'] != 'verified',
                                                                 e['kitchen_id'], e['chef_id'], e['period'] or ''))
            if jobs[branch]:
                r['career_affiliations'] = jobs[branch]
            if rid not in curated['restaurants']:
                primary = next(iter(r['origins'] or jobs[branch]), None)
                r.update(type='alumni' if r['origins'] else 'established',
                         kitchen=r['origins'][0]['kitchen_id'] if r['origins'] else None)
                if primary:
                    r.update(chef=primary['chef_name'], chef_en=primary['chef_name_en'] or primary['chef_name'],
                             role=primary['role'], lineage_status=primary['lineage_status'],
                             career_checking=primary['career_checking'], lineage_src=primary['evidence'][0]['src'])
            restaurants.append(r)
    kitchen_rows = []
    for branch in sorted(kids, key=kids.get):
        for kid in site['kitchens'][branch] or [kids[branch]]:
            kitchen_rows.append({**node(branch, kid, 'kitchens'), 'restaurant_id': rids[branch]})
    projection = {'restaurants': restaurants, 'kitchens': kitchen_rows}
    aliases = {}
    for row in conn.execute('SELECT branch_id,kind,site_id FROM projection_site_id ORDER BY kind,site_id'):
        canonical_branch = identity.canonical(branch_redirects, row['branch_id'])
        selected = (rids if row['kind'] == 'restaurant' else kids).get(canonical_branch)
        kind = 'r' if row['kind'] == 'restaurant' else 'k'
        if selected and selected != row['site_id'] and row['site_id'] not in reserved:
            aliases[f"/{kind}/{row['site_id']}/"] = f"/{kind}/{selected}/"
    if aliases:
        projection['route_aliases'] = aliases
    return projection
