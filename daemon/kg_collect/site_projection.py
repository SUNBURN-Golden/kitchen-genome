"""Materialize the public Korea-connected graph from a committed service snapshot."""
from base64 import b64decode, b64encode
from collections import defaultdict, deque
import json

from kg_daemon import claims, ontology
from kg_daemon.db import stable_id
from . import evaluate, lineage_identity as identity
from .net import host_of
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


def retained_fingerprint(hashes):
    # Pack the native career_document recipe (digest(shingle)[:12] over casefolded
    # shingles) so reviewed and native proofs compare on the same overlap set.
    # The seed carries neither publisher text nor unhashed shingles.
    packed = bytes.fromhex(''.join(sorted(hashes)))
    return b64encode(packed).decode('ascii')


def _shingle_set(value):
    if not value:
        return set()
    if value.startswith('['):
        return set(json.loads(value))
    packed = b64decode(value)
    return {packed[i:i + 6] for i in range(0, len(packed), 6)}


def _publisher_domain(url):
    parts = host_of(url).split('.')
    registered = 2
    if len(parts) >= 3 and len(parts[-1]) == 2 and parts[-2] in {'co', 'com', 'net', 'org', 'gov', 'edu', 'ac', 'ne', 'or', 'go'}:
        registered = 3
    return '.'.join(parts[-registered:])


def _verified(rows):
    trusted = [(r, _shingle_set(r['body_fingerprint'])) for r in rows if r['independent_eligible']]
    for i, (left, left_shingles) in enumerate(trusted):
        for right, right_shingles in trusted[i + 1:]:
            if (left['publisher_group'] == right['publisher_group'] or left['origin_group'] == right['origin_group']
                    or _publisher_domain(left['src']) == _publisher_domain(right['src'])
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
    refs = {}
    for row in rows:
        src, quote = row['src'], row['quote']
        if safe_url(src) and public_quote(quote):
            ref = refs.setdefault((src, quote), dict(kind=kind, src=src, quote=quote))
            if row.get('ai_reviewed'):
                ref['badge'] = 'AI 검토'
    return [refs[key] for key in sorted(refs)]


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
    mentors = {row['mentor_id'] for row in rows if row.get('mentor_id') in people}
    mentor_names = {row['mentor_text'] for row in rows if row.get('mentor_text')}
    mentor = people[next(iter(mentors))] if len(mentors) == 1 else {}
    mentorship = {}
    if len(mentor_names) == 1:
        mentorship = {'mentor_name': mentor.get('name_ko') or mentor.get('display_name') or next(iter(mentor_names))}
        if mentor.get('display_name_en'):
            mentorship['mentor_name_en'] = mentor['display_name_en']
    return {**mentorship, 'chef_id': best['chef_id'], 'chef_name': chef.get('name_ko') or chef['display_name'],
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

    if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='career_assertion'").fetchone():
        return False
    with write(conn):
        changed = refresh(conn, seed)
        if seed.get('ai_promotion', {}).get('supports'):
            from kg_ai import promotion
            from kg_daemon.db import utcnow

            promotion.record_links(seed, utcnow())
        return changed


def refresh(conn, seed):
    """Refresh native and reviewed careers inside the caller's write transaction."""
    from kg_ai import promotion

    old = seed.get(KEY, {})
    state = seed.get(promotion.KEY, {})
    owned = (set(r[0] for r in conn.execute("SELECT entity_id FROM ai_promotion_identity WHERE kind='branch'"))
             if state else set())
    snapshot = _snapshot(conn, seed, owned)
    if snapshot is None:
        return False

    def allocate(kind, branch, ids, reserved):
        site_id = _stable_site_id(conn, kind, branch, ids, reserved)
        if not ids:
            snapshot['site_ids'][kind][branch] = site_id
        return site_id

    if state.get('supports'):
        promotion.ensure_origin_holders(conn, seed, snapshot, state)
    projection = compose(seed, snapshot, allocate)
    if state.get('supports'):
        snapshot['aliases'] = [dict(r) for r in conn.execute(
            'SELECT branch_id,kind,site_id FROM projection_site_id ORDER BY kind,site_id')]
    cached = state.get('snapshot')
    if state.get('supports'):
        state['snapshot'] = snapshot
    else:
        state.pop('snapshot', None)
    if projection != old:
        seed[KEY] = projection
    return projection != old or cached != state.get('snapshot')


SNAPSHOT_FIELDS = ('assertion_id', 'canonical_url', 'src', 'quote', 'relation', 'chef_id', 'branch_id',
                   'mentor_id', 'previous_branch_id', 'venue_text', 'chef_text', 'mentor_text',
                   'previous_venue_text', 'valid_from', 'valid_to', 'role_text', 'role_id', 'independent_eligible',
                   'content_hash', 'body_fingerprint', 'quote_key', 'publisher_group', 'origin_group', 'published_at')


def _snapshot(conn, seed, owned):
    from .site_publish import TOMBSTONES, _public_quote, safe_url

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
    site, _ = _site_links(conn, seed, branch_redirects)
    blocked = {name_key(r['creation_event']['name']) for r in seed.get(TOMBSTONES, [])
               if r.get('creation_event', {}).get('name')}
    blocked.update(name_key(alias) for r in seed.get(TOMBSTONES, []) for alias in r.get('aliases', []))
    denied = {bid for bid, row in branches.items() if name_key(row['name']) in blocked}
    denied.update(identity.canonical(branch_redirects, bid) for bid in tuple(denied))
    roots = {identity.canonical(branch_redirects, branch) for branch, r in branches.items()
             if r['country_code'] == 'KR' and branch not in owned}
    roots.update(branch for branch, ids in site['restaurants'].items() if OVERSEAS.intersection(ids))
    careers = []
    for row in _career_rows(conn):
        r = dict(row)
        if not safe_url(r['src']) or not _public_quote(r['quote']):
            continue
        for field in ('branch_id', 'previous_branch_id'):
            r[field] = identity.canonical(branch_redirects, r[field])
        for field in ('chef_id', 'mentor_id'):
            r[field] = identity.canonical(chef_redirects, r[field])
        if r['branch_id'] not in denied:
            career = {key: r[key] for key in SNAPSHOT_FIELDS}
            career['body_fingerprint'] = retained_fingerprint(json.loads(r['body_fingerprint'] or '[]'))
            careers.append(career)
            if r['branch_id'] in owned and branches[r['branch_id']]['country_code'] == 'KR':
                roots.add(r['branch_id'])
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
        if safe_url(row['source_url']):
            listings.setdefault(bid, {key: row[key] for key in ('stars', 'guide_year', 'source_url')})
    korean_names = {}
    for alias in conn.execute("""SELECT a.*,c.status FROM alias a JOIN claim c USING(claim_id)
        WHERE a.entity_type='branch' AND a.lang='ko' AND c.status IN ('source_claim','verified')
        ORDER BY a.created_at,a.alias_id"""):
        branch = identity.canonical(branch_redirects, alias['entity_id'])
        korean_names.setdefault(branch, alias['alias'])
    aliases = [dict(r) for r in conn.execute('SELECT branch_id,kind,site_id FROM projection_site_id ORDER BY kind,site_id')]
    site_ids = {'restaurant': {}, 'kitchen': {}}
    for row in aliases:
        site_ids[row['kind']].setdefault(row['branch_id'], row['site_id'])
    state = seed.get('ai_promotion', {})
    reviewed_ids = {row[k] for row in state.get('supports', {}).values()
                    for k in ('chef_id', 'mentor_id') if row.get(k)}
    reviewed_ids.update(identity.canonical(chef_redirects, value) for value in tuple(reviewed_ids))
    person_ids = reviewed_ids | {r['chef_id'] for r in careers}
    needed = roots | {r['branch_id'] for r in careers} | {
        identity.canonical(branch_redirects, r['branch_id']) for r in state.get('supports', {}).values()}
    return dict(branches={k: {f: branches[k][f] for f in ('name', 'name_en', 'city', 'area', 'country_code')}
                          for k in sorted(needed) if k in branches},
                careers=careers, roots=sorted(roots - denied), denied=sorted(denied),
                site={kind: dict(mapping) for kind, mapping in site.items()},
                people={k: {f: v for f, v in row.items() if f in ('display_name', 'display_name_en', 'name_ko')}
                        for k, row in people.items() if k in person_ids}, roles=roles,
                listings={k: row for k, row in listings.items() if k in needed},
                korean_names={k: name for k, name in korean_names.items() if k in needed},
                aliases=aliases, site_ids=site_ids, owned=sorted(owned),
                branch_redirects=branch_redirects, chef_redirects=chef_redirects)


def graph(seed, snapshot):
    from kg_ai import promotion
    from .lineage import conflicting_stints

    branches = {bid: dict(row) for bid, row in snapshot['branches'].items()}
    people = {cid: dict(row) for cid, row in snapshot['people'].items()}
    careers = [dict(row) for row in snapshot['careers']]
    supports = seed.get(promotion.KEY, {}).get('supports', {})
    canonical_branches, canonical_chefs = snapshot['branch_redirects'], snapshot['chef_redirects']
    reviewed = []
    for original in supports.values():
        row = dict(original)
        for field in ('branch_id', 'previous_branch_id'):
            row[field] = identity.canonical(canonical_branches, row[field])
        for field in ('chef_id', 'mentor_id'):
            row[field] = identity.canonical(canonical_chefs, row[field])
        if row['branch_id'] in snapshot['denied'] or row['branch_id'] not in branches or row['chef_id'] not in people:
            continue
        reviewed.append(row)
    by_pair = defaultdict(list)
    for row in (*careers, *reviewed):
        if not row.get('seed_anchor'):
            by_pair[row['branch_id'], row['chef_id']].append(row)
    def consistent(row):
        return not any(conflicting_stints(row, other) for other in by_pair[row['branch_id'], row['chef_id']]
                       if other is not row)

    reviewed = [row for row in reviewed if consistent(row)]
    for row in reviewed:
        person = people[row['chef_id']]
        for value in (row['chef_text'], row.get('bilingual_name')):
            if value and not person.get('display_name_en') and value.isascii():
                person['display_name_en'] = value
            if value and not person.get('name_ko') and any('가' <= ch <= '힣' for ch in value):
                person['name_ko'] = value
    reviewed_branches = {r['branch_id'] for r in reviewed}
    native_branches = {r['branch_id'] for r in careers}
    for branch in reviewed_branches & set(snapshot['owned']) - native_branches:
        for field in ('city', 'country_code'):
            values = {r[field] for r in reviewed if r['branch_id'] == branch and r[field]}
            branches[branch][field] = next(iter(values)) if len(values) == 1 else None
    careers.extend(reviewed)
    _link_late_names(careers, branches)
    roots = set(snapshot['roots']) | {r['branch_id'] for r in reviewed}
    links, affiliations = _traverse(careers, roots)
    # Every reviewed stint appears at its own employer, including standalone
    # former and training claims. Native rows at that employer may corroborate it.
    for pair, rows in by_pair.items():
        if not any(r.get('ai_reviewed') for r in rows) or not any(r in reviewed for r in rows):
            continue
        present = {r['assertion_id'] for r in affiliations[pair]}
        affiliations[pair].extend(r for r in rows if r['assertion_id'] not in present
                                  and (not r.get('ai_reviewed') or r in reviewed))
    return branches, people, roots, links, affiliations, reviewed_branches


def compose(seed, snapshot, allocate=None):
    from kg_ai import promotion
    from .site_publish import _public_quote, safe_url

    branches, people, roots, links, affiliations, reviewed = graph(seed, snapshot)
    promotion.refresh_holder_fields(seed, snapshot, branches)
    kitchens = {origin for _, origin, _ in links}
    displayed = roots | kitchens | {destination for destination, _, _ in links}
    if not displayed:
        return {}
    curated = {kind: {row['id']: row for row in seed[kind]} for kind in ('restaurants', 'kitchens')}
    reserved = {sid for rows in curated.values() for sid in rows}
    site = {kind: defaultdict(list, {branch: [sid for sid in ids if sid in curated[kind]]
                                    for branch, ids in snapshot['site'][kind].items()}) for kind in curated}
    # A reviewed kitchen can publish career evidence without a restaurant page.
    kitchen_only = {b for b in reviewed - set(snapshot['roots']) if site['kitchens'][b]
                    and not site['restaurants'][b] and not any(destination == b for destination, _, _ in links)}
    kitchens.update(kitchen_only)
    kitchens.update(b for b in reviewed if site['kitchens'][b])
    if allocate is None:
        def allocate(kind, branch, ids, reserved):
            return ids[0] if ids else snapshot['site_ids'][kind][branch]

    roles, listings, korean_names = (snapshot[k] for k in ('roles', 'listings', 'korean_names'))
    rids = {branch: allocate('restaurant', branch, site['restaurants'][branch], reserved) for branch in sorted(displayed - kitchen_only)}
    kids = {branch: allocate('kitchen', branch, site['kitchens'][branch], reserved) for branch in sorted(kitchens)}
    origins = defaultdict(list)
    for (destination, origin, _), rows in sorted(links.items()):
        for group in _stint_groups({r['assertion_id']: r for r in rows}.values()):
            entry = _summarize(group, 'lineage', people, roles, safe_url, _public_quote)
            entry['kitchen_id'] = kids[origin]
            origins[destination].append(entry)
    jobs = defaultdict(list)
    for (branch, _), rows in sorted(affiliations.items()):
        sourced = [r for r in rows if not r.get('seed_anchor')]
        for group in _stint_groups(sourced):
            jobs[branch].append(_summarize(group, 'affiliation', people, roles, safe_url, _public_quote))

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
            item = node(branch, kid, 'kitchens')
            if branch in rids:
                item['restaurant_id'] = rids[branch]
            if branch in reviewed and jobs[branch]:
                item['career_affiliations'] = jobs[branch]
            kitchen_rows.append(item)
    projection = {'restaurants': restaurants, 'kitchens': kitchen_rows}
    aliases = {}
    for row in snapshot['aliases']:
        canonical_branch = identity.canonical(snapshot['branch_redirects'], row['branch_id'])
        selected = (rids if row['kind'] == 'restaurant' else kids).get(canonical_branch)
        kind = 'r' if row['kind'] == 'restaurant' else 'k'
        if selected and selected != row['site_id'] and row['site_id'] not in reserved:
            aliases[f"/{kind}/{row['site_id']}/"] = f"/{kind}/{selected}/"
    if aliases:
        projection['route_aliases'] = aliases
    return projection
