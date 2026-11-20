"""Private Michelin roster observations, cross-checks and chef discovery in tier order."""
import datetime as dt
import json
import os
import re
import unicodedata
from dataclasses import dataclass, field
from urllib.parse import parse_qsl, quote, unquote, urlencode, urljoin, urlsplit, urlunsplit

import yaml
from bs4 import BeautifulSoup

from kg_daemon.claims import queue_review
from kg_daemon.db import ROOT, stable_id

from . import EXTRACTOR_VERSION, FETCHER_VERSION, global_queue as queue
from .discover import canonical_query
from .fetch import canonicalize_url, parse_feed, unreadable_js_shell
from .htmlx import jsonld_blocks, parse_html
from .net import TRAP, Response, host_of, valid_destination

CONFIG = os.path.join(ROOT, 'sources', 'michelin_selections.yaml')
HOST = 'guide.michelin.com'
VERSION = FETCHER_VERSION + '+michelin1'
TIERS = {1: '1-star-michelin', 2: '2-stars-michelin', 3: '3-stars-michelin'}
LIST_PATH = re.compile(
    r'^/(?:[a-z]{2}(?:-[a-z]{2})?/){1,2}'
    r'(?:(?:selection/)?[\w%-]+/)*restaurants'
    r'(?:/[123]-stars?-michelin)?(?:/page/\d+)?/?$', re.I)
SCOPE_PATH = re.compile(r'^/(?:[a-z]{2}(?:-[a-z]{2})?/){1,2}selection/([\w%-]+)(?:/(?:restaurants/?)?)?$', re.I)
VENUE_PATH = re.compile(r'/restaurant/([^/]+)/?$', re.I)
PAGE_PARAMS = {'page', 'paged', 'offset', 'filter', 'sort', 'order', 'facet'}
NO_AWARD = re.compile(r'green[ -]star|étoile verte|grüne[rn]? stern|그린\s*스타|绿色|綠星|グリーンスター|bib gourmand|recommended|selected restaurant|\bbib\b', re.I)
YEAR = r'(?:19|20)\d{2}'
COUNTRY_FORMAT = re.compile(r'^[A-Z]{2}$')


@dataclass(frozen=True)
class Listing:
    name: str
    stars: int
    locator: str
    year: int | None = None
    country: str | None = None
    city: str = ''
    address: str = ''
    url: str = ''
    listing_id: str = ''
    chef: str = ''


@dataclass
class Parsed:
    rows: list[Listing] = field(default_factory=list)
    links: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    no_list: bool = False
    latest_year: int = 0


def load_config(path=CONFIG):
    with open(path, encoding='utf-8') as config:
        return yaml.safe_load(config)


def norm(value):
    return ' '.join(re.findall(r'[^\W_]+', unicodedata.normalize('NFKC', value or '').casefold()))


def slug(value):
    return norm(value).replace(' ', '-')


def iso_stamp(now):
    return dt.datetime.fromtimestamp(now, dt.timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z')


def list_path(url):
    return host_of(url) == HOST and bool(LIST_PATH.fullmatch(urlsplit(url).path))


def trapped(url):
    # Allow only crawler-trapped parameters on Michelin selection/list routes.
    parts = urlsplit(url.lower())
    if not list_path(url) or any(pattern in unquote(parts.path) for pattern in TRAP):
        return any(pattern in url.lower() for pattern in TRAP)
    pairs = parse_qsl(parts.query, keep_blank_values=True)
    return any(pattern in f'{key}={value}' for key, value in pairs
               for pattern in TRAP
               if not (key in PAGE_PARAMS and pattern == key + '='))


def michelin_url(base, href):
    url = _linked_url(base, href)
    return url if host_of(url) == HOST else ''


def venue_path(url):
    if host_of(url) == HOST and VENUE_PATH.search(urlsplit(url).path):
        path = re.sub(r'^/(?:[a-z]{2}(?:-[a-z]{2})?/){1,2}', '/', urlsplit(url).path, flags=re.I)
        return unquote(path.rstrip('/')).casefold()
    return ''


def year_of(value):
    match = re.search(r'(?<!\d)' + YEAR + r'(?!\d)', str(value or ''))
    return int(match[0]) if match else None


def edition_year(node, url=''):
    explicit = node.select_one('[data-guide-year], [data-edition-year], meta[name="guide-year"], meta[name="guide-edition"]')
    value = node.get('data-guide-year') or node.get('data-edition-year') if hasattr(node, 'attrs') else None
    if explicit:
        value = value or explicit.get('data-guide-year') or explicit.get('data-edition-year') or explicit.get('content')
    if year := year_of(value):
        return year
    for heading in node.select('h1, title, .guide-edition'):
        text = heading.get_text(' ', strip=True)
        if re.search(r'michelin|미쉐린|미슐랭|ミシュラン|米其林', text, re.I) and (year := year_of(text)):
            return year
    return year_of(urlsplit(url).path) if '/article/michelin-guide-ceremony/' in url else None


def star_value(label):
    raw = unicodedata.normalize('NFKC', str(label or '')).strip()
    if NO_AWARD.search(raw):
        return None
    template = re.fullmatch(
        r'\{\{\s*(?:(?:michelin(?:[ _-]?guide|[ _-]stars?)?|étoiles? Michelin|ミシュラン|米其林|'
        r'star[ _-]rating|stars?|étoiles?)\s*\|\s*(?:stars?\s*=\s*)?([123])(?:\s*\|[^{}]*)?'
        r'|([123])[ _-]stars?)\s*\}\}', raw, re.I)
    if template:
        return int(template[1] or template[2])
    if re.fullmatch(r'[★⭐✱✳✽＊*🌟\s\ufe0f]{1,12}', raw):
        count = sum(not ch.isspace() and ch != '\ufe0f' for ch in raw)
        return count if 1 <= count <= 3 else None
    m = re.fullmatch(r'(?:michelin\s*)?([123一二三])\s*(?:[- ]?stars?(?:[- ]michelin)?|étoiles?|sterne?|stelle?|estrellas?|estrelas?|つ星|星|성|스타|ดาว)(?:\s+michelin)?', raw, re.I)
    if m:
        return {'一': 1, '二': 2, '三': 3}.get(m[1]) or int(m[1])
    words = {'one': 1, 'two': 2, 'three': 3, 'une': 1, 'deux': 2, 'trois': 3}
    m = re.fullmatch(r'(one|two|three|une|deux|trois)[ -](?:stars?|étoiles?)(?: michelin)?', raw, re.I)
    return words[m[1].lower()] if m else int(raw) if raw in ('1', '2', '3') else None


def distinction(card):
    for node in [card, *card.select('[data-stars], [data-distinction], img[alt], [title], [aria-label], [href*="star"], use')]:
        for attr in ('data-stars', 'data-distinction', 'alt', 'title', 'aria-label', 'href', 'xlink:href'):
            label = node.get(attr, '')
            token = re.split(r'[#/]', str(label))[-1].removesuffix('.svg').replace('-', ' ')
            if label and (stars := star_value(token)):
                return stars
    icons = card.select('[class*="michelin-star"], [class*="icon-star"], img[src*="etoile"]')
    count = sum(not NO_AWARD.search(' '.join(i.get('class', [])) + ' ' + i.get('alt', '') + ' ' + i.get('src', ''))
                and 'green' not in str(i).lower() for i in icons)
    return count if 1 <= count <= 3 else None


def _text(node, css):
    element = node.select_one(css)
    return element.get_text(' ', strip=True) if element else ''


def _country(value, cfg):
    if not value:
        return None
    key = slug(value)
    if value.upper() in cfg['countries'] or (COUNTRY_FORMAT.fullmatch(value) and value not in ('EN', 'EU')):
        return value.upper()
    for code, definition in cfg['countries'].items():
        if any(slug(name) == key for name in [definition['name'], *definition.get('aliases', [])]):
            return code
    return None


def _venue_geo(url, cfg):
    parts = [unquote(p) for p in urlsplit(url).path.strip('/').split('/')]
    if 'restaurant' not in parts or len(parts) < 4:
        return None, ''
    geo = parts[parts.index('restaurant') - 1]
    for name in sorted(cfg['countries'], key=lambda code: -len(cfg['countries'][code]['name'])):
        definition = cfg['countries'][name]
        for label in sorted([definition['name'], *definition.get('aliases', [])], key=len, reverse=True):
            country_slug = slug(label)
            if geo == country_slug or geo.startswith(country_slug + '-'):
                return name, geo.removeprefix(country_slug).strip('-').removesuffix('-region').replace('-', ' ').title()
    return None, ''


def _country_from_card(card, url, scope, cfg):
    country, city = _venue_geo(url, cfg)
    location = _text(card, '[itemprop=addressLocality], .card__menu-content--localisation, .restaurant-location, .card-location')
    if location:
        parts = [p.strip() for p in location.split(',')]
        city = parts[0]
        if len(parts) > 1:
            country = _country(parts[-1], cfg) or country
    countries = scope.get('countries', [])
    explicit = card.get('data-country-code') or _text(card, '[itemprop=addressCountry]')
    if explicit:
        country = _country(explicit, cfg) or country
    if len(countries) == 1:
        country = country or countries[0]
    return country, card.get('data-city') or city or scope.get('city') or ''


def _tier_link(url):
    parts = urlsplit(url)
    filters = [value for key, value in parse_qsl(parts.query) if key.lower() in ('filter', 'distinction', 'stars', 'tier')]
    tokens = [part for part in unquote(parts.path).split('/') if '-star' in part.lower()]
    for token in [*filters, *tokens]:
        if match := re.fullmatch(r'([123])(?:-stars?(?:-michelin)?)?', token.lower()):
            return int(match[1])
    return None


def _page_links(soup, url, tier):
    current = canonicalize_url(url)
    here = _tier_link(url)
    pages, filters = set(), set()
    current_path = re.sub(r'/page/\d+$', '', urlsplit(url).path.rstrip('/'))
    for anchor in soup.select('a[href], link[rel=next][href]'):
        target = michelin_url(url, anchor['href'])
        if not target or target == current or not list_path(target):
            continue
        target_path = re.sub(r'/page/\d+$', '', urlsplit(target).path.rstrip('/'))
        linked_tier = _tier_link(target)
        same_route = target_path == current_path
        filter_route = target_path.removesuffix('/' + TIERS[tier]) == current_path or same_route
        if not here and linked_tier == tier and filter_route:
            filters.add(target)
            continue
        rel = anchor.get('rel', [])
        pager = anchor.find_parent(class_=re.compile(r'pagination|pager', re.I))
        if same_route and linked_tier == here and ('next' in rel or pager):
            pages.add(target)
    return sorted(filters or pages)


def parse_selection(html, url, tier, scope, cfg):
    soup = BeautifulSoup(html, 'lxml')
    result = Parsed(links=_page_links(soup, url, tier))
    page_year = edition_year(soup, url)
    cards = {}
    for link in soup.select('a[href*="/restaurant/"]'):
        target = michelin_url(url, link['href'])
        if not venue_path(target):
            continue
        card = link.find_parent(class_=re.compile(r'\bcard__menu\b(?![-\w])|restaurant-card')) or link.find_parent(
            attrs={'data-restaurant-id': True}) or link.find_parent('article') or link
        old = cards.get(target)
        if old is None or link.get_text(strip=True):
            cards[target] = (card, link)
    for index, (target, (card, link)) in enumerate(cards.items(), 1):
        if distinction(card) != tier:
            continue
        name = card.get('data-name') or _text(card, '.card__menu-content--title, [itemprop=name], h2, h3, h4') or link.get_text(' ', strip=True)
        country, city = _country_from_card(card, target, scope, cfg)
        row_id = card.get('data-restaurant-id') or card.get('data-id') or link.get('data-restaurant-id') or ''
        locator = f'card {index} ({row_id or venue_path(target)})'
        if not name or not country:
            result.errors.append(locator + ': missing venue name or country')
            continue
        result.rows.append(Listing(name, tier, locator, edition_year(card) or page_year, country, city,
                                   _text(card, '[itemprop=streetAddress], .restaurant-address, address'), target, row_id,
                                   card.get('data-chef') or _text(card, '[itemprop=chef], .chef-name')))
    result.no_list = bool(soup.select_one('[data-no-results], .no-results, .no-restaurant, .search-results-empty')) or bool(
        re.search(r'\b(?:no restaurants found|no matching restaurants|aucun restaurant|0 (?:matching )?restaurants)\b',
                  soup.get_text(' ', strip=True), re.I))
    if not cards and not result.links and not result.no_list:
        result.errors.append('unrecognized Michelin selection layout')
    return result


def parse_directory(html, url, cfg):
    soup = BeautifulSoup(html, 'lxml')
    scopes = {}
    unknown = []
    for tag in soup.select('a[href], option[value], [data-selection-url], [data-coverage], [data-country-code]'):
        href = tag.get('href') or tag.get('data-selection-url') or tag.get('value') or ''
        target = michelin_url(url, href) if href else ''
        path = urlsplit(target).path
        match = SCOPE_PATH.fullmatch(path)
        label = tag.get('data-selection-name') or tag.get_text(' ', strip=True)
        country = _country(tag.get('data-country-code') or label, cfg)
        no_list = tag.get('data-coverage') in ('none', 'unavailable', 'no-list') or (
            tag.name == 'option' and tag.has_attr('disabled') and country is not None)
        if not (match or no_list or (target and list_path(target) and path.count('/') >= 3 and _tier_link(target) is None)):
            continue
        parts = path.strip('/').split('/')
        scope = unquote(match[1]) if match else slug(label)
        region = cfg.get('regions', {}).get(scope, {})
        if not country:
            country = _country(scope, cfg) or next(
                (code for part in parts[:-1] if part != 'en' and (code := _country(part, cfg))), None)
        countries = region.get('countries') or ([country] if country else [])
        if not countries:
            if match or no_list:
                unknown.append(scope or label)
            continue
        if not scope:
            route = [p for p in parts[:-1] if len(p) > 2 and p != 'selection']
            scope = route[-1] if route else slug(cfg['countries'].get(country, {}).get('name', country))
        if match and not list_path(target):
            target = target.rstrip('/') + '/restaurants'
        locale = region.get('locale') or cfg['countries'].get(countries[0], {}).get('locale', 'en')
        entry = {'scope': scope, 'label': label or scope.replace('-', ' ').title(), 'url': None if no_list else target,
                 'locale': locale, 'countries': countries, 'city': tag.get('data-city') or region.get('city', '')}
        existing = scopes.get(scope)
        if not existing or (
            bool(entry['url']), '/en/' in (entry['url'] or ''), entry['url'] or ''
        ) > (bool(existing['url']), '/en/' in (existing['url'] or ''), existing['url'] or ''):
            scopes[scope] = entry
    return list(scopes.values()), sorted(set(unknown))


def wiki_url(locale, title):
    return f'https://{locale}.wikipedia.org/w/api.php?' + urlencode({
        'action': 'query', 'prop': 'revisions', 'rvprop': 'ids|content', 'rvslots': 'main',
        'redirects': '1', 'format': 'json', 'formatversion': '2', 'titles': title})


def _wiki_text(cell):
    text = re.sub(r'<ref\b[^>]*(?:/>|>.*?</ref>)|<!--.*?-->', '', cell, flags=re.I | re.S)
    text = re.sub(r'\[\[(?:[^\[\]|]*\|)*([^\[\]|]+)\]\]', r'\1', text)
    text = re.sub(r'\{\{\s*(?:sort|nowrap|lang)\s*\|(?:[^{}|]*\|)*([^{}|]+)\}\}', r'\1', text, flags=re.I)
    return re.sub(r'<[^>]+>', '', text).strip(" ' \n")


def _table_rows(text, offset):
    cells, spans = [], {}
    header, row_offset = False, offset
    for position, line in enumerate(text.splitlines() + ['|}'], offset):
        line = line.strip()
        if line.startswith(('|-', '|}')):
            if cells:
                values = {}
                for col, (count, value) in list(spans.items()):
                    values[col] = value
                    if count == 1:
                        del spans[col]
                    else:
                        spans[col] = count - 1, value
                col = 0
                for cell in cells:
                    while col in values:
                        col += 1
                    attr, sep, value = cell.partition('|')
                    if not sep or not re.search(r'\b(?:rowspan|colspan|style|align|scope|class|data-sort-value)\s*=', attr):
                        attr, value = '', cell
                    value = _wiki_text(value)
                    row = re.search(r'rowspan\s*=\s*["\']?(\d+)', attr)
                    width = re.search(r'colspan\s*=\s*["\']?(\d+)', attr)
                    for _ in range(min(int(width[1]), 100) if width else 1):
                        values[col] = value
                        if row and int(row[1]) > 1:
                            spans[col] = (int(row[1]) - 1, value)
                        col += 1
                yield header, [values.get(i, '') for i in range(max(values, default=-1) + 1)], row_offset
            cells, header, row_offset = [], False, position + 1
        elif line.startswith(('!', '|')) and not line.startswith('|+'):
            if not cells:
                row_offset = position
            header = header or line.startswith('!')
            # Cell separators outside wiki links/templates are the only inline boundaries.
            chunks = re.split(r'(\[\[|\]\]|\{\{|\}\}|!!|\|\|)', line[1:])
            levels, pieces = 0, ['']
            for chunk in chunks:
                if chunk in ('[[', '{{'):
                    levels += 1
                if chunk in (']]', '}}'):
                    levels = max(0, levels - 1)
                if levels == 0 and chunk == ('!!' if line[0] == '!' else '||'):
                    pieces.append('')
                else:
                    pieces[-1] += chunk
            cells.extend(pieces)
        elif cells and line:
            cells[-1] += ' ' + line


NAME_COLUMN = re.compile(r'restaurant|establishment|nom|店名|餐厅|餐廳|レストラン|음식점|레스토랑|식당|nazwa|nombre|nome')
CITY_COLUMN = re.compile(r'city|location|municipality|town|^ville|^lieu|localidad|localização|ciudad|città|stadt|地域|都市|城市|地区|지역|도시|소재지')
STAR_COLUMN = re.compile(r'stars?|rating|étoiles?|sterne?|estrellas?|stelle?|星|스타|등급')
EMPTY_AWARD = re.compile(r'^(?:[-–—―×0]*|n/?a|closed|fermé|비고|bib gourmand|recommended|green star)$', re.I)


def parse_wikipedia(payload, tier, country, city=''):
    result = Parsed()
    if not isinstance(payload, dict) or not isinstance(payload.get('query'), dict):
        result.errors.append('missing MediaWiki query')
        return result, ''
    pages = payload['query'].get('pages', [])
    if isinstance(pages, dict):
        pages = list(pages.values())
    if not pages or pages[0].get('missing') is not None:
        result.errors.append('Wikipedia list missing')
        return result, ''
    page = pages[0]
    revisions = page.get('revisions') or [{}]
    revision = revisions[0]
    main = revision.get('slots', {}).get('main', {})
    wiki = main.get('content') or main.get('*') or revision.get('*') or ''
    revision_id = revision.get('revid')
    article = '/wiki/' + quote(page.get('title', '').replace(' ', '_')) + (f'?oldid={revision_id}' if revision_id else '')
    recognized = False
    for table in re.finditer(r'^\{\|[^\n]*\n(.*?)^\|\}', wiki, re.M | re.S):
        before = wiki[:table.start()]
        heading = re.findall(r'^=+\s*(.*?)\s*=+\s*$', before, re.M)
        context = ' '.join([heading[-1] if heading else '', *re.findall(r'^\|\+\s*(.*)$', table[1], re.M)])
        context_year = year_of(context)
        heading_tier = star_value(context)
        if heading and not heading_tier:
            clean = re.sub(YEAR, '', heading[-1]).strip(' ()–-')
            heading_tier = star_value(clean)
        name_index = city_index = star_index = None
        years = {}
        offset = before.count('\n') + 2
        for header, cells, line in _table_rows(table[1], offset):
            if header:
                for index, cell in enumerate(cells):
                    key = norm(cell)
                    if NAME_COLUMN.search(key):
                        name_index = index
                    if CITY_COLUMN.search(key):
                        city_index = index
                    if STAR_COLUMN.search(key):
                        star_index = index
                    if re.fullmatch(f'{YEAR}(?:年|년도)?', cell):
                        years[index] = year_of(cell)
                recognized = recognized or (name_index is not None and bool(years or star_index is not None or heading_tier))
                continue
            if name_index is None or name_index >= len(cells) or not cells[name_index]:
                continue
            row_city = cells[city_index] if city_index is not None and city_index < len(cells) else city
            ratings = [(year, cells[index]) for index, year in years.items() if index < len(cells)]
            if not years and (star_index is not None or heading_tier):
                value = cells[star_index] if star_index is not None and star_index < len(cells) else str(heading_tier or '')
                ratings.append((context_year, value))
            for year, cell in ratings:
                stars = star_value(cell)
                locator = f'revision {revision_id or "unknown"}, line {line}, edition {year or "unknown"}'
                if year:
                    result.latest_year = max(result.latest_year, year)
                if stars == tier:
                    result.rows.append(Listing(cells[name_index], stars, locator, year, country, row_city))
                elif stars is None and not (EMPTY_AWARD.fullmatch(cell) or NO_AWARD.search(cell)):
                    result.errors.append(locator + ': unrecognized star notation')
    if not recognized or not revision_id:
        result.errors.append('unrecognized Wikipedia list or missing revision ID')
    return result, article


def _press_star(text):
    # Section and award cells are scoped by the edition announcement parser.
    label = re.sub(r'\b(?:new|restaurants?|awarded|with|the|nouveaux?|neue[nr]?)\b', '', text, flags=re.I)
    return star_value(' '.join(label.split()).strip(' :—-'))


def parse_announcement(html, url, scope, cfg):
    soup = BeautifulSoup(html, 'lxml')
    main = soup.select_one('[itemprop=articleBody], article, main') or soup
    title = _text(soup, 'h1') or _text(soup, 'title')
    year = edition_year(soup, url) or year_of(title)
    result = Parsed()
    guide = any(norm(mod['guide']) in norm(title) for mod in cfg['modifiers'].values())
    if not (guide and year):
        return result, None, None
    canonical = soup.select_one('link[rel=canonical][href]')
    credited = [str(item.get('author', '')) for item in jsonld_blocks(soup)]
    byline = _text(soup, '[rel=author], .byline, .author')
    mirror = (canonical and host_of(urljoin(url, canonical['href'])) != host_of(url)) or bool(
        re.search(r'press release|business wire|pr newswire|provided by|republished from|courtesy of michelin|보도자료', ' '.join([title, byline, *credited]), re.I))
    if host_of(url) == HOST:
        source = 'michelin_announcement'
    elif mirror:
        source = 'press_mirror'
    else:
        source = 'press_original' if byline or any(credited) else 'press_unattributed'
    default_country = scope['countries'][0] if len(scope['countries']) == 1 else None
    for i, tr in enumerate(main.select('tr'), 1):
        cells = tr.find_all('td', recursive=False)
        if len(cells) < 2:
            continue
        awards = [(j, _press_star(c.get_text(' ', strip=True))) for j, c in enumerate(cells)]
        awards = [(j, rating) for j, rating in awards if rating]
        if len(awards) != 1:
            continue
        index, stars = awards[0]
        name = cells[1 if index == 0 else 0].get_text(' ', strip=True)
        geo = [c.get_text(' ', strip=True) for j, c in enumerate(cells) if j not in (index, 1 if index == 0 else 0)]
        country = _country(geo[-1], cfg) or default_country if geo else default_country
        if name and country:
            result.rows.append(Listing(name, stars, f'table row {i}', year, country, geo[0] if geo else scope['city']))
    stars = None
    for i, element in enumerate(main.select('h2, h3, h4, ul > li, ol > li'), 1):
        if element.name != 'li':
            stars = _press_star(element.get_text(' ', strip=True))
        elif stars and default_country and (name := element.get_text(' ', strip=True)):
            link = element.select_one('a[href*="/restaurant/"]')
            result.rows.append(Listing(name, stars, f'announcement section item {i}', year, default_country,
                                       scope['city'], url=michelin_url(url, link['href']) if link else ''))
    for anchor in main.select('a[href]'):
        target = michelin_url(url, anchor['href'])
        if target and list_path(target):
            result.links.append(target)
    if not result.rows:
        result.errors.append('dated Michelin announcement without recognizable award rows')
    result.latest_year = year
    return result, source, year


def _scope_from_row(row):
    return {'scope': row['scope'], 'label': row['label'], 'url': row['url'], 'locale': row['locale'],
            'countries': json.loads(row['countries_json']), 'city': row['city'] or '',
            'wiki': json.loads(row['wiki_json']), 'announcement_year': row['announcement_year']}


def _add_scope(conn, scope, now, cfg, seeds):
    key = scope['scope']
    wiki = []
    for country in scope['countries']:
        options = cfg['countries'].get(country, {})
        country_name = options.get('wiki_name') or options.get('name', country)
        title = f'List of Michelin-starred restaurants in {scope["city"] or country_name}'
        wiki.append({'locale': 'en', 'title': title, 'country': country, 'city': scope['city']})
        wiki.extend(dict(spec, country=country) for spec in cfg.get('wikipedia', {}).get(country, [])
                    if not scope['city'] or spec.get('city') == scope['city'])
    previous = conn.execute('SELECT url FROM global_scope WHERE scope=?', (key,)).fetchone()
    # A locale mirror or temporarily missing link does not invalidate an already known selection.
    if previous and previous['url'] and not scope['url']:
        scope = dict(scope, url=previous['url'])
    conn.execute("""INSERT INTO global_scope(scope,label,url,locale,countries_json,city,wiki_json) VALUES (?,?,?,?,?,?,?)
        ON CONFLICT(scope) DO UPDATE SET url=excluded.url,countries_json=excluded.countries_json,wiki_json=excluded.wiki_json""",
        (key, scope['label'], scope['url'], scope['locale'], json.dumps(scope['countries']), scope['city'], json.dumps(wiki, ensure_ascii=False)))
    for tier in reversed(TIERS):
        conn.execute('INSERT OR IGNORE INTO global_tier_pass(sweep_id,tier,scope,disposition) VALUES (?,?,?,?)',
                     (queue.SWEEP, tier, key, 'pending' if scope['url'] else 'no_list'))
        if scope['url']:
            target = scope['url']
            if key == 'worldwide':
                target = target.rstrip('/') + '/' + TIERS[tier]
            _list_job(conn, key, target, tier, now, root=True)
        for spec in wiki:
            target = wiki_url(spec['locale'], spec['title'])
            queue.enqueue(conn, tier=tier, kind='crosscheck', scope=key, target=target, now=now, priority=50,
                          identity=f'{key}:{tier}:{target}', cursor=dict(spec, mode='wiki'))
        if scope['countries']:
            modifier = cfg['modifiers'].get(scope['locale'], cfg['modifiers']['en'])['guide']
            keyword = f'{modifier} {scope["label"]}'
            target = rss_url(seeds, keyword, scope['locale'])
            queue.enqueue(conn, tier=tier, kind='crosscheck', scope=key, target=target, now=now, priority=35,
                          identity=f'{key}:{tier}:{target}', cursor={'mode': 'press-search'})
            for feed in cfg.get('announcement_feeds', {}).get(key, []):
                queue.enqueue(conn, tier=tier, kind='crosscheck', scope=key, target=feed, now=now, priority=35,
                              identity=f'{key}:{tier}:{feed}', cursor={'mode': 'press-search'})


def bootstrap(conn, now, cfg, seeds):
    with queue.write(conn):
        conn.execute('INSERT OR IGNORE INTO global_sweep VALUES (?,3)', (queue.SWEEP,))
        directory = cfg['directory']
        queue.enqueue(conn, tier=3, kind='list', target=directory, now=now, priority=110, cursor={'mode': 'directory'})
        _add_scope(conn, {'scope': 'worldwide', 'label': 'Worldwide', 'url': directory, 'locale': 'en',
                          'countries': [], 'city': ''}, now, cfg, seeds)
        for scope in cfg.get('daily_scopes', []):
            if not conn.execute('SELECT 1 FROM global_scope WHERE scope=?', (scope['scope'],)).fetchone():
                _add_scope(conn, {'city': '', **scope}, now, cfg, seeds)


def _list_job(conn, scope, target, tier, now, root=False):
    return queue.enqueue(conn, tier=tier, kind='list', scope=scope, target=target, now=now, priority=100,
                         identity=f'{scope}:{tier}:{target}', cursor={'mode': 'selection', 'root': root})


def rss_url(seeds, query, locale):
    endpoint = seeds['keyword_search']['endpoint'].replace('setlang=ko', 'setlang=' + locale)
    return endpoint.format(q=quote(query))


def _branch(conn, row, source, now):
    keys = [f'id:{row.listing_id}'] if row.listing_id and source == 'michelin' else []
    if path := venue_path(row.url):
        keys.append('path:' + path)
    matches = {match[0] for key in keys
               for match in conn.execute('SELECT branch_id FROM michelin_venue_identity WHERE identity=?', (key,))}
    identity = keys[0] if keys else ''
    if len(matches) > 1:
        return None
    branch = next(iter(matches), None)
    name, city, address = norm(row.name), norm(row.city), norm(row.address)
    if not branch and city:
        candidates = conn.execute("""SELECT branch_id,address_key,official FROM michelin_branch_location
            WHERE country_code=? AND name_key=? AND city_key=?""", (row.country, name, city)).fetchall()
        verified = [c[0] for c in candidates if (address and c['address_key'] == address)
                    or (not address and (not keys or not c['official']))
                    or (address and not c['address_key'] and not c['official'])]
        if len(verified) > 1:
            return None
        if verified:
            branch = verified[0]
        if not branch and not candidates:
            existing = conn.execute("""SELECT b.branch_id,b.city,r.name,r.name_en FROM restaurant r JOIN branch b USING(restaurant_id)
                WHERE b.country_code=? AND (r.name=? COLLATE NOCASE OR r.name_en=? COLLATE NOCASE
                  OR EXISTS (SELECT 1 FROM alias a WHERE a.entity_type='restaurant' AND a.entity_id=r.restaurant_id
                             AND a.alias=? COLLATE NOCASE))""", (row.country, row.name, row.name, row.name)).fetchall()
            existing = [r['branch_id'] for r in existing if norm(r['city']) == city or
                        (row.country == 'KR' and {'서울': 'seoul', '부산': 'busan'}.get(r['city']) == city)]
            if len(existing) > 1:
                return None
            if existing:
                branch = existing[0]
    if not branch and not city and not keys:
        # An unlocated cross-check row can match a single known location for this country.
        candidates = conn.execute("""SELECT branch_id FROM michelin_branch_location
            WHERE country_code=? AND name_key=?""", (row.country, name)).fetchall()
        if len(candidates) != 1:
            return None
        branch = candidates[0][0]
    if branch:
        country = conn.execute('SELECT country_code FROM branch WHERE branch_id=?', (branch,)).fetchone()[0]
        if country and country != row.country:
            return None
        conn.execute('UPDATE branch SET country_code=COALESCE(country_code,?),city=COALESCE(city,?) WHERE branch_id=?',
                     (row.country, row.city or None, branch))
    else:
        identity = '\x1e'.join((row.country, name, city, address, identity if not address else ''))
        restaurant_id = stable_id('res', 'michelin', identity)
        branch = stable_id('br', restaurant_id, 'main')
        conn.execute('INSERT OR IGNORE INTO restaurant(restaurant_id,name,created_at) VALUES (?,?,?)', (restaurant_id, row.name, now))
        conn.execute('INSERT OR IGNORE INTO branch(branch_id,restaurant_id,country_code,city,created_at) VALUES (?,?,?,?,?)',
                     (branch, restaurant_id, row.country, row.city or None, now))
    conn.execute("""INSERT INTO michelin_branch_location VALUES (?,?,?,?,?,?,?)
        ON CONFLICT(branch_id) DO UPDATE SET
          city_key=CASE WHEN city_key='' THEN excluded.city_key ELSE city_key END,
          address_key=CASE WHEN address_key='' OR excluded.official>official AND excluded.address_key<>''
                       THEN excluded.address_key ELSE address_key END,
          address=CASE WHEN address IS NULL OR excluded.official>official AND excluded.address IS NOT NULL
                    THEN excluded.address ELSE address END, official=MAX(official,excluded.official)""",
        (branch, row.country, name, city, address, row.address or None, source == 'michelin'))
    conn.executemany('INSERT OR IGNORE INTO michelin_venue_identity VALUES (?,?)', ((key, branch) for key in keys))
    return branch


def record_listing(conn, row, source, url, fetch_id, scope, now):
    """Return branch and insertion state. Awards and dated changes remain append-only."""
    branch = _branch(conn, row, source, now)
    if branch is None:
        return None, False
    edition = str(row.year) if row.year else 'undated'
    selection_id = stable_id('guide', scope, row.country, edition)
    conn.execute("""INSERT OR IGNORE INTO guide_selection(selection_id,scope,country_code,edition_key,guide_year,observed_at)
        VALUES (?,?,?,?,?,?)""", (selection_id, scope, row.country, edition, row.year, now))
    previous = conn.execute("""SELECT stars,fetch_id,observation_fingerprint FROM guide_listing
        WHERE selection_id=? AND branch_id=? AND source_kind=? AND source_url=?
        ORDER BY observed_at DESC,rowid DESC LIMIT 1""", (selection_id, branch, source, url)).fetchone()
    if previous and previous['stars'] == row.stars and previous['fetch_id'] == fetch_id:
        return branch, False
    fingerprint = stable_id('award', str(row.stars), row.locator, fetch_id or '', previous['observation_fingerprint'] if previous else '')
    new = conn.execute("""INSERT OR IGNORE INTO guide_listing(selection_id,branch_id,stars,source_kind,source_url,fetch_id,locator,observed_at,observation_fingerprint)
        VALUES (?,?,?,?,?,?,?,?,?)""", (selection_id, branch, row.stars, source, url, fetch_id, row.locator, now, fingerprint)).rowcount
    if new and row.year:
        conflicts = conn.execute("""WITH latest_by_source AS (
            SELECT listing.stars,listing.source_kind,
                ROW_NUMBER() OVER (PARTITION BY listing.selection_id,listing.source_kind
                    ORDER BY listing.observed_at DESC,listing.rowid DESC) AS rank
            FROM guide_listing listing JOIN guide_selection edition USING(selection_id)
            WHERE branch_id=? AND country_code=? AND guide_year=?)
            SELECT DISTINCT stars,source_kind FROM latest_by_source WHERE rank=1 AND stars<>?""",
            (branch, row.country, row.year, row.stars))
        for conflict in conflicts:
            if (source == 'michelin') == (conflict['source_kind'] == 'michelin'):
                continue
            tiers = sorted([row.stars, conflict['stars']])
            key = stable_id('guide-conflict', branch, edition, *map(str, tiers))
            queue_review(conn, 'guide_list_discrepancy', 'branch', branch, {'year': row.year, 'stars': tiers, 'url': url},
                         dedupe_key=key, now=now)
    return branch, bool(new)


def _focus(conn, job_key):
    return [dict(r) for r in conn.execute("""SELECT DISTINCT r.name,b.city,b.country_code,o.branch_id
        FROM global_work_origin o JOIN branch b USING(branch_id) JOIN restaurant r USING(restaurant_id)
        WHERE o.job_key=?""", (job_key,))]


def _news_url(url):
    # Bing News RSS wraps publisher URLs as a link parameter.
    parts = urlsplit(url)
    if host_of(url) == 'bing.com' and parts.path.rstrip('/') == '/news/apiclick':
        return dict(parse_qsl(parts.query)).get('url', '')
    return url


def _linked_url(url, href):
    try:
        absolute = urljoin(url, href)
        if not valid_destination(absolute):
            return ''
        target = canonicalize_url(absolute)
        return target if not trapped(target) else ''
    except ValueError:
        return ''


class Roster:
    def __init__(self, conn, net, raw, cfg, seeds):
        self.conn, self.net, self.raw, self.cfg, self.seeds = conn, net, raw, cfg, seeds
        self.clock = net.clock

    def _review(self, job, reason, details, stamp):
        key = stable_id('guide-review', job['job_key'], reason, str(details))
        queue_review(self.conn, reason, 'global_work', job['job_key'], {'url': job['target'], 'details': details},
                     dedupe_key=key, now=stamp)

    def _refresh(self, job, now, *, short=False):
        if short:
            return now + 86400
        period = self.cfg.get('refresh_days', 7) * 86400
        # Deterministic phase spreads weekly refreshes while keeping each scope's tier ordering.
        phase = int(job['job_key'][-4:], 16) % period
        return (int((now - phase) // period) + 1) * period + phase

    def _seed(self, job, row, branch, now):
        parent_scope = self.conn.execute('SELECT locale FROM global_scope WHERE scope=?', (job['scope'],)).fetchone()
        default_locale = self.cfg['countries'].get(row.country, {}).get('locale', 'en')
        locale = parent_scope['locale'] if parent_scope and job['scope'] != 'worldwide' else default_locale
        mods = self.cfg['modifiers'].get(locale, self.cfg['modifiers']['en'])
        origins = [(branch, row.stars)]
        subject = ' '.join(filter(None, (row.chef, row.name, row.city)))
        if row.url:
            queue.enqueue(self.conn, tier=row.stars, kind='venue', scope=job['scope'], target=row.url, now=now, priority=85,
                          identity=f'branch:{branch}', origins=origins)
        for modifier in mods['chef']:
            query = f'{subject} {modifier}'
            queue.enqueue(self.conn, tier=row.stars, kind='discover', scope=job['scope'],
                          target=rss_url(self.seeds, query, locale), now=now, priority=65,
                          identity=locale + ':' + canonical_query(query), cursor={'query': query}, origins=origins)

    def _observations(self, job, scope, parsed, source, url, fetch_id, now, stamp):
        latest = parsed.latest_year or max((row.year or 0 for row in parsed.rows), default=0)
        edition_by_country = {}
        for row in parsed.rows:
            branch, new = record_listing(self.conn, row, source, url, fetch_id, scope['scope'], stamp)
            if not branch:
                self._review(job, 'guide_venue_location',
                             {'row': row.locator, 'name': row.name, 'year': row.year, 'stars': row.stars,
                              'country': row.country, 'city': row.city, 'source': source, 'source_url': url,
                              'fetch_id': fetch_id, 'venue_url': row.url}, stamp)
                continue
            if not row.year:
                self._review(job, 'guide_edition_unknown', row.locator, stamp)
            if not new:
                continue
            if source == 'michelin':
                self._seed(job, row, branch, now)
                continue
            if source == 'press_mirror' or not row.year:
                continue
            if row.country not in edition_by_country:
                edition = self.conn.execute("""SELECT MAX(guide_year) FROM guide_selection s
                    WHERE s.scope=? AND s.country_code=? AND EXISTS (
                      SELECT 1 FROM guide_listing l WHERE l.selection_id=s.selection_id AND l.source_kind='michelin')""",
                    (scope['scope'], row.country)).fetchone()[0]
                edition_by_country[row.country] = max(edition or 0, scope['announcement_year'] or 0) or latest
            if row.year != edition_by_country[row.country]:
                continue
            preferred = self.conn.execute("""SELECT stars FROM guide_listing l JOIN guide_selection s USING(selection_id)
                WHERE branch_id=? AND country_code=? AND guide_year=? AND source_kind='michelin'
                ORDER BY l.observed_at DESC,l.rowid DESC LIMIT 1""", (branch, row.country, row.year)).fetchone()
            if not preferred or preferred[0] == row.stars:
                self._seed(job, row, branch, now)

    def _page(self, job, body, url, headers, now, stamp, *, unchanged, cached, doc):
        if unchanged and cached:
            self.conn.execute("""UPDATE global_page SET checked_at=?,etag=COALESCE(?,etag),
                last_modified=COALESCE(?,last_modified) WHERE url=?""",
                (now, headers.get('etag'), headers.get('last-modified'), job['target']))
            return cached['fetch_id']
        host = host_of(url)
        kind = job['kind']
        mode = json.loads(job['cursor']).get('mode')
        method = 'api' if mode == 'wiki' else 'feed' if kind == 'discover' or mode == 'press-search' else 'html'
        raw_ref, content_hash = self.raw.put(host, body, {'api': 'json', 'feed': 'xml', 'html': 'html'}[method])
        canonical = canonicalize_url(url)
        fid = stable_id('fch', VERSION, canonical, method, content_hash)
        # An article already collected by the ordinary crawler has the same private raw snapshot.
        if kind in ('venue', 'article'):
            existing = self.conn.execute("""SELECT fetch_id FROM fetch_record WHERE canonical_url=? AND content_hash=?
                AND method='html' AND outcome='ok' AND raw_ref IS NOT NULL AND text_ref IS NOT NULL
                ORDER BY fetched_at,fetch_id LIMIT 1""", (canonical, content_hash)).fetchone()
            if existing:
                fid = existing[0]
        text = doc.get('text', '')
        text_ref = self.raw.put_text(host, text) if text else None
        self.conn.execute("""INSERT OR IGNORE INTO fetch_record(fetch_id,url,canonical_url,host,method,http_status,
            outcome,content_type,bytes,content_hash,raw_ref,text_ref,text_chars,
            fetcher_version,extractor_version,published_at,fetched_at)
            VALUES (?,?,?,?,?,200,'ok',?,?,?,?,?,?,?,?,?,?)""",
            (fid, url, canonical, host, method, headers.get('content-type'), len(body), content_hash, raw_ref,
             text_ref, len(text), VERSION, EXTRACTOR_VERSION, doc.get('published_at'), stamp))
        self.conn.execute("""INSERT INTO global_page VALUES (?,?,?,?,?,?) ON CONFLICT(url) DO UPDATE SET
            fetch_id=excluded.fetch_id,final_url=excluded.final_url,etag=excluded.etag,
            last_modified=excluded.last_modified,checked_at=excluded.checked_at""",
            (job['target'], fid, url, headers.get('etag'), headers.get('last-modified'), now))
        if kind in ('article', 'venue'):
            self.conn.execute("""INSERT INTO fetch_priority VALUES (?,?,?,?) ON CONFLICT(fetch_id)
                DO UPDATE SET score=MAX(score,excluded.score)""", (fid, 1 + job['tier'] / 10, 'michelin', stamp))
        return fid

    def _press_job(self, job, target, now):
        self._related(job, 'crosscheck', target, now, priority=40,
                      identity=f'{job["scope"]}:{job["tier"]}:{target}', cursor={'mode': 'press'})

    def _related(self, job, kind, target, now, *, priority, identity=None, cursor=None, fetch_id=None):
        return queue.enqueue(self.conn, tier=job['tier'], kind=kind, scope=job['scope'], target=target, now=now,
                             priority=priority, identity=identity, cursor=cursor,
                             origins=queue.origins(self.conn, job['job_key']), parent=job['job_key'], fetch_id=fetch_id)

    def _news(self, job, items, url, now, *, announcements):
        names = {norm(row['name']) for row in _focus(self.conn, job['job_key'])}
        for item in items:
            target = _linked_url(url, _news_url(item['url']))
            if not target or target == canonicalize_url(url) or host_of(target) == 'bing.com':
                continue
            if announcements:
                title = norm(item.get('title'))
                guide = any(norm(mod['guide']) in title for mod in self.cfg['modifiers'].values())
                if guide and (year_of(title) or '/michelin-guide-ceremony/' in target):
                    self._press_job(job, target, now)
            elif any(name and name in norm(' '.join([item.get('title', ''), item.get('summary', '')]))
                     for name in names):
                self._related(job, 'article', target, now, priority=30)

    def _venue(self, job, soup, url, fid, now, stamp):
        for item in jsonld_blocks(soup):
            address = item.get('address')
            main_name = _text(soup, 'h1') or item.get('name')
            if not (isinstance(address, dict) and isinstance(main_name, str)):
                continue
            country = _country(address.get('addressCountry'), self.cfg)
            for focus in _focus(self.conn, job['job_key']):
                if country and country != focus['country_code']:
                    continue
                row = Listing(focus['name'], job['tier'], 'venue JSON-LD address', edition_year(soup), focus['country_code'],
                              address.get('addressLocality') or focus['city'] or '', address.get('streetAddress') or '', url,
                              chef=_text(soup, '[itemprop=chef], .chef-name'))
                if _branch(self.conn, row, 'michelin', stamp) == focus['branch_id'] and row.chef:
                    self._seed(job, row, focus['branch_id'], now)
        focus = _focus(self.conn, job['job_key'])
        for anchor in soup.select('a[href]'):
            label = norm(anchor.get_text(' ', strip=True))
            target = _linked_url(url, anchor['href'])
            official_link = label in ('visit website', 'official website', 'website', 'site web', '웹사이트', '公式サイト')
            if target and official_link and len(focus) == 1 and host_of(target) != HOST:
                self.conn.execute('INSERT OR IGNORE INTO career_official_branch VALUES (?,?,?)',
                                  (focus[0]['branch_id'], host_of(target), fid))
            if target and (official_link
                           or re.search(r'/(?:chef|team|about|bio|interview)(?:[/-]|$)', urlsplit(target).path, re.I)
                           and anchor.find_parent(['main', 'article'])):
                if host_of(target) != HOST or '/article/' in urlsplit(target).path:
                    self._related(job, 'article', target, now, priority=60)
        self._extract_seed(job, fid, now)

    def _extract_seed(self, job, fid, now):
        fetch = self.conn.execute('SELECT text_ref FROM fetch_record WHERE fetch_id=?', (fid,)).fetchone()
        if not fetch or not fetch['text_ref']:
            return
        self._related(job, 'extract', job['target'], now, priority=25, identity=f'{fid}:{EXTRACTOR_VERSION}',
                      cursor={'fetch_id': fid}, fetch_id=fid)

    def _scope(self, job):
        row = self.conn.execute('SELECT * FROM global_scope WHERE scope=?', (job['scope'],)).fetchone()
        return _scope_from_row(row) if row else {'scope': 'worldwide', 'countries': [], 'city': '', 'announcement_year': None}

    def _parse_page(self, job, mode, payload, url):
        scope = self._scope(job)
        if mode == 'directory':
            return parse_directory(payload, url, self.cfg)
        if mode == 'selection':
            parsed = parse_selection(payload, url, job['tier'], scope, self.cfg)
            soup = BeautifulSoup(payload, 'lxml')
            announcements = [target for anchor in soup.select('a[href*="michelin-guide-ceremony/"]')
                             if (target := michelin_url(url, anchor['href']))]
            return parsed, announcements
        if mode == 'wiki':
            spec = json.loads(job['cursor'])
            try:
                data = json.loads(payload)
            except ValueError:
                data = {}
            return parse_wikipedia(data, job['tier'], spec['country'], spec.get('city', ''))
        if mode == 'press':
            return parse_announcement(payload, url, scope, self.cfg)
        if mode == 'press-search' or job['kind'] == 'discover':
            return parse_feed(payload, url)
        return BeautifulSoup(payload, 'lxml')

    def _apply(self, job, mode, page, url, fid, now, stamp):
        scope = self._scope(job)
        refresh = None
        errors = []
        if mode == 'directory':
            scopes, unknown = page
            for found in scopes:
                _add_scope(self.conn, found, now, self.cfg, self.seeds)
            errors = [f'unmapped selection: {scope}' for scope in unknown]
            if not scopes:
                errors.append('country and selection links unavailable')
            refresh = self._refresh(job, now)
        elif mode == 'selection':
            parsed, announcements = page
            self._observations(job, scope, parsed, 'michelin', url, fid, now, stamp)
            for continuation in parsed.links:
                _list_job(self.conn, scope['scope'], continuation, job['tier'], now)
            if scope['countries']:
                for announcement in announcements:
                    self._press_job(job, announcement, now)
            disposition = 'no_list' if parsed.no_list and not parsed.rows else 'active'
            self.conn.execute("""UPDATE global_tier_pass SET disposition=?
                WHERE sweep_id=? AND tier=? AND scope=? AND ?='no_list'
                  AND NOT EXISTS (SELECT 1 FROM global_work WHERE kind='list' AND scope=? AND tier=?
                  AND json_extract(cursor,'$.root')=1 AND job_key<>?)""",
                (disposition, queue.SWEEP, job['tier'], scope['scope'], disposition, scope['scope'], job['tier'], job['job_key']))
            errors, refresh = parsed.errors, self._refresh(job, now)
        elif mode == 'wiki':
            parsed, article = page
            self._observations(job, scope, parsed, 'wikipedia', urljoin(url, article), fid, now, stamp)
            errors, refresh = parsed.errors, self._refresh(job, now)
        elif mode == 'press-search' or job['kind'] == 'discover':
            self._news(job, page, url, now, announcements=mode == 'press-search')
            refresh = self._refresh(job, now, short=mode == 'press-search')
        elif mode == 'press':
            parsed, source, year = page
            if source:
                primary = source in ('michelin_announcement', 'press_original')
                announcement = parsed.rows and primary and year >= (scope['announcement_year'] or 0)
                if announcement and self.conn.execute('INSERT OR IGNORE INTO global_announcement VALUES (?,?,?)',
                                                      (scope['scope'], fid, year)).rowcount:
                    self.conn.execute('UPDATE global_scope SET announcement_year=? WHERE scope=?', (year, scope['scope']))
                    self.conn.execute("""UPDATE global_work SET available_at=MIN(available_at,?)
                        WHERE sweep_id=? AND scope=? AND kind='list' AND state IN ('pending','deferred')""",
                        (now, queue.SWEEP, scope['scope']))
                    scope['announcement_year'] = year
                parsed.rows = [row for row in parsed.rows if row.stars == job['tier']]
                self._observations(job, scope, parsed, source, url, fid, now, stamp)
                for link in parsed.links:
                    if (scope['url'] and urlsplit(link).path.startswith(urlsplit(scope['url']).path.rstrip('/'))
                            and _tier_link(link) in (None, job['tier'])):
                        _list_job(self.conn, scope['scope'], link, job['tier'], now)
            errors = parsed.errors
        elif job['kind'] == 'venue':
            self._venue(job, page, url, fid, now, stamp)
            refresh = self._refresh(job, now)
        elif job['kind'] == 'article':
            self._extract_seed(job, fid, now)
            self._article_links(job, page, url, now)
        if errors:
            self._review(job, 'guide_parse_review', errors, stamp)
            return {'state': 'deferred' if refresh else 'review', 'available_at': refresh, 'reason': 'parse_review'}
        return {'state': 'pending' if refresh else 'done', 'available_at': refresh}

    def _article_links(self, job, soup, url, now):
        main = soup.select_one('[itemprop=articleBody], article, main') or soup
        for anchor in main.select('a[href]'):
            target = _linked_url(url, anchor['href'])
            if not target or target == url:
                continue
            label = norm(anchor.get_text(' ', strip=True))
            if re.search(r'\b(?:chef|biography|interview)\b|셰프|料理長|シェフ|主廚', label, re.I):
                self._related(job, 'article', target, now, priority=28)

    def _extract(self, job, extractor):
        from .targets import Gazetteer

        fid = json.loads(job['cursor'])['fetch_id']
        fetch = self.conn.execute('SELECT * FROM fetch_record WHERE fetch_id=?', (fid,)).fetchone()
        if not fetch or not fetch['text_ref']:
            return {'state': 'review', 'reason': 'missing_article_text', 'fetch_id': fid}
        if extractor is None:
            return {'state': 'deferred', 'available_at': self.clock() + 3600, 'reason': 'extractor_unavailable', 'fetch_id': fid}
        try:
            focus = [origin['branch_id'] for origin in _focus(self.conn, job['job_key'])]
            complete = extractor(fetch, Gazetteer(self.conn), focus)
        except (FileNotFoundError, UnicodeError):
            return {'state': 'review', 'reason': 'missing_article_text', 'fetch_id': fid}
        if not complete:
            return {'state': 'deferred', 'available_at': self.clock() + 3600, 'reason': 'extractor_deferred', 'fetch_id': fid}
        return {'fetch_id': fid, 'extracted': True}

    def _failure(self, job, response, now):
        if response.error == 'block_end':
            return {'state': 'pending', 'available_at': max(now + 1, self.net.deadline or 0), 'reason': 'block_end', 'attempted': False}
        if response.blocked:
            retry = self.conn.execute('SELECT retry_after_at FROM web_host_guard WHERE host=?',
                                      (host_of(response.final_url),)).fetchone()
            run = self.conn.execute('SELECT expires_at FROM web_access_run WHERE run_id=?', (self.net.run_id,)).fetchone()
            return {'state': 'deferred', 'available_at': max(now + 300, run[0] if run else 0, (retry[0] or 0) if retry else 0),
                    'reason': 'fail_login_page' if response.error == 'login_page' else 'fail_blocked'}
        reason = response.error or f'http_{response.status}'
        interval = 86400 if (response.status in (404, 410) or response.error in ('robots', 'banned', 'unsafe_destination')) else 3600
        return {'state': 'deferred', 'available_at': now + interval, 'reason': reason}

    def _request(self, job):
        cached = self.conn.execute('''SELECT p.*, f.content_hash,f.raw_ref,f.content_type FROM global_page p
            JOIN fetch_record f USING(fetch_id) WHERE p.url=?''', (job['target'],)).fetchone()
        params = {'Accept-Language': 'en', 'Accept': 'text/html,application/json,application/rss+xml,application/xml,*/*;q=0.5'}
        response = self.net.blocked_response(job['target'])
        if response:
            return cached, response, None, False
        cached_body = None
        if cached:
            try:
                cached_body = self.raw.read(cached['raw_ref'])
            except OSError:
                cached = None
        if cached and cached['etag']:
            params['If-None-Match'] = cached['etag']
        if cached and cached['last_modified']:
            params['If-Modified-Since'] = cached['last_modified']
        if cached and job['fetch_id'] is None and cached['checked_at'] >= self.clock() - 3600:
            response = Response(job['target'], 304, {}, b'', final_url=cached['final_url'])
            return cached, response, cached_body, True
        fixed_host = HOST if job['host'] == HOST and job['kind'] in ('list', 'venue') else None
        response = self.net.get(job['target'], params, restrict_host=fixed_host,
                                before_send=lambda: queue.owns(self.conn, job, self.clock()))
        if response.status == 304 and cached:
            return cached, response, cached_body, False
        if response.status == 200 and not response.error and not response.blocked:
            return cached, response, response.body, False
        return cached, response, None, False

    def work(self, job, *, extractor=None):
        """Write a checkpoint and its children in one transaction, then acknowledge separately."""
        if not queue.owns(self.conn, job, self.clock()):
            return 'lease_lost'
        extract = job['kind'] == 'extract'
        if extract:
            result = self._extract(job, extractor)
        else:
            cached, response, body, reuse = self._request(job)
            if body is not None:
                url = cached['final_url'] if response.status == 304 else response.final_url
                unchanged = cached and url == cached['final_url'] and (
                    response.status == 304 or cached['content_hash'] == self.raw.sha(body))
                mode = json.loads(job['cursor']).get('mode')
                already_parsed = (unchanged and cached['fetch_id'] == job['fetch_id']
                                  and job['reason'] not in ('parse_review', 'block_end'))
                page, doc = None, {}
                if not already_parsed:
                    snapshot = Response(url, 200, {'Content-Type': cached['content_type']}, body) if response.status == 304 else response
                    payload = snapshot.text()
                    page = self._parse_page(job, mode, payload, url)
                    if not unchanged and job['kind'] in ('article', 'venue'):
                        doc = parse_html(payload, url)
        with queue.write(self.conn):
            now = self.clock()
            stamp = iso_stamp(now)
            if not queue.owns(self.conn, job, now):
                return 'lease_lost'
            if extract:
                if result.pop('extracted', False):
                    self.conn.execute('UPDATE fetch_record SET extracted_at=?,extractor_version=? WHERE fetch_id=?',
                                      (stamp, EXTRACTOR_VERSION, result['fetch_id']))
            elif body is None:
                result = self._failure(job, response, now)
            else:
                fid = cached['fetch_id'] if reuse else self._page(job, body, url, response.headers, now, stamp,
                                                                unchanged=unchanged, cached=cached, doc=doc)
                if already_parsed:
                    result = {'state': 'pending', 'available_at': self._refresh(
                        job, now, short=mode == 'press-search')}
                else:
                    result = self._apply(job, mode, page, url, fid, now, stamp)
                if mode == 'selection' and not result.get('reason'):
                    self.conn.execute("""UPDATE global_tier_pass SET last_success_at=?,
                      disposition=CASE WHEN disposition='no_list' THEN disposition ELSE 'complete' END
                        WHERE sweep_id=? AND tier=? AND scope=?
                        AND NOT EXISTS (SELECT 1 FROM global_work w WHERE w.sweep_id=? AND w.tier=?
                        AND w.scope=? AND w.kind='list' AND w.job_key<>?
                        AND (w.fetch_id IS NULL OR w.reason IS NOT NULL))""",
                        (now, queue.SWEEP, job['tier'], job['scope'], queue.SWEEP, job['tier'], job['scope'], job['job_key']))
                result['fetch_id'] = fid
            queue.checkpoint(self.conn, job, now, **result)
        queue.acknowledge(self.conn, job, self.clock())
        return result.get('reason') or ('not_modified' if not extract and response.status == 304 else 'ok')


def record_exception(conn, job, clock, exc):
    now = clock()
    with queue.write(conn):
        if not queue.owns(conn, job, now):
            return
        reason = type(exc).__name__
        queue_review(conn, 'guide_worker_error', 'global_work', job['job_key'], {'error': reason, 'url': job['target']},
                     dedupe_key=f'{job["job_key"]}:{reason}', now=iso_stamp(now))
        queue.checkpoint(conn, job, now, state='deferred', available_at=now + 3600, reason=reason)
    queue.acknowledge(conn, job, clock())
