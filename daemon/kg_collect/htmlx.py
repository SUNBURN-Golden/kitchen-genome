"""HTML -> structured document: main text, JSON-LD, tables, links, pagination, publish date. Rules only; no model."""
import json
import re
import urllib.parse

from bs4 import BeautifulSoup

NOISE = re.compile(r'(comment|sidebar|related|banner|advert|\bad[-_]|share|sns|gnb|lnb|footer|popup|cookie|breadcrumb|recommend|ranking|login)', re.I)
MAIN_SELECTORS = ['[itemprop=articleBody]', '#article-view-content', '.article-body', '#articleBody', '.view_con', '.news_view', '.article_view', 'article', '#content', 'main']
DATE_PATTERNS = [
    re.compile(r'(?:입력|등록|기사입력|작성|발행|게재)?\s*(20\d{2})[.\-/년 ]\s*(\d{1,2})[.\-/월 ]\s*(\d{1,2})'),
]


def _iso(y, m, d):
    try:
        return f'{int(y):04d}-{int(m):02d}-{int(d):02d}'
    except ValueError:
        return None


def parse_date_text(s: str | None):
    if not s:
        return None
    m = re.match(r'\s*(20\d{2})-(\d{2})-(\d{2})', s)
    if m:
        return _iso(*m.groups())
    for p in DATE_PATTERNS:
        m = p.search(s)
        if m:
            return _iso(*m.groups())
    return None


def jsonld_blocks(soup) -> list:
    out = []
    for s in soup.find_all('script', attrs={'type': re.compile('ld\\+json', re.I)}):
        try:
            data = json.loads(s.string or s.get_text() or '')
        except Exception:
            continue
        stack = data if isinstance(data, list) else [data]
        while stack:
            d = stack.pop()
            if isinstance(d, dict):
                out.append(d)
                if '@graph' in d and isinstance(d['@graph'], list):
                    stack.extend(d['@graph'])
    return out


def _table_text(t) -> str:
    rows = []
    for tr in t.find_all('tr'):
        cells = [re.sub(r'\s+', ' ', c.get_text(' ', strip=True)) for c in tr.find_all(['th', 'td'])]
        cells = [c for c in cells if c]
        if cells:
            rows.append(' | '.join(cells))
    return '\n'.join(rows)


def parse_html(html: str, url: str) -> dict:
    soup = BeautifulSoup(html, 'lxml')
    ld = jsonld_blocks(soup)
    title = None
    og = soup.find('meta', property='og:title')
    if og and og.get('content'):
        title = og['content'].strip()
    if not title and soup.title:
        title = soup.title.get_text(strip=True)
    published = None
    for d in ld:
        if d.get('datePublished'):
            published = parse_date_text(str(d['datePublished']))
            break
    if not published:
        for key in ('article:published_time', 'og:article:published_time', 'datePublished', 'date'):
            m = soup.find('meta', attrs={'property': key}) or soup.find('meta', attrs={'name': key}) or soup.find('meta', attrs={'itemprop': key})
            if m and m.get('content'):
                published = parse_date_text(m['content'])
                if published:
                    break
    if not published:
        t = soup.find('time', attrs={'datetime': True})
        if t:
            published = parse_date_text(t['datetime'])
    links = []
    for a in soup.find_all('a', href=True):
        href = urllib.parse.urljoin(url, a['href'].strip())
        if href.startswith(('http://', 'https://')):
            links.append((href.split('#')[0], re.sub(r'\s+', ' ', a.get_text(' ', strip=True))[:120], a.get('rel') or []))
    next_url = None
    ln = soup.find('link', rel='next')
    if ln and ln.get('href'):
        next_url = urllib.parse.urljoin(url, ln['href'])
    else:
        for u, txt, rel in links:
            if 'next' in rel or txt.strip() in ('다음', '다음 페이지', '다음페이지', 'Next', 'next', '>', '›', '»'):
                next_url = u
                break
    images = []
    for im in soup.find_all('img', src=True):
        src = urllib.parse.urljoin(url, im['src'])
        if src.startswith('http') and re.search(r'\.(jpe?g|png|webp)(\?|$)', src, re.I):
            images.append((src, (im.get('alt') or '')[:100]))
    for tag in soup(['script', 'style', 'noscript', 'nav', 'header', 'footer', 'aside', 'form', 'iframe', 'svg']):
        tag.decompose()
    for tag in soup.find_all(True, attrs={'class': NOISE}) + soup.find_all(True, attrs={'id': NOISE}):
        if tag.name not in ('html', 'body', 'article', 'main'):
            tag.decompose()
    main = None
    for sel in MAIN_SELECTORS:
        found = soup.select(sel)
        if found:
            cand = max(found, key=lambda e: len(e.get_text(strip=True)))
            if len(cand.get_text(strip=True)) > 150:
                main = cand
                break
    main = main or soup.body or soup
    parts = []
    for el in main.find_all(['h1', 'h2', 'h3', 'p', 'li', 'table', 'blockquote', 'div', 'td'], recursive=True):
        if el.name == 'table':
            parts.append(_table_text(el))
            for d in el.find_all(True):
                d.extract()
            continue
        if el.name in ('div', 'td') and el.find(['p', 'li', 'div', 'table', 'h1', 'h2', 'h3']):
            continue
        txt = re.sub(r'\s+', ' ', el.get_text(' ', strip=True))
        if txt:
            parts.append(txt)
    text = '\n'.join(dict.fromkeys(parts)) if parts else re.sub(r'\s+', ' ', main.get_text(' ', strip=True))
    ld_text = []
    for d in ld:
        for k in ('headline', 'name', 'description', 'articleBody'):
            if isinstance(d.get(k), str) and len(d[k]) > 8:
                ld_text.append(d[k])
    if len(text) < 150 and ld_text:      # JSON-LD fallback for thin pages
        text = (text + '\n' + '\n'.join(dict.fromkeys(ld_text))).strip()
    return {'title': title, 'text': text.strip(), 'published_at': published, 'links': links, 'next_url': next_url,
            'jsonld_types': sorted({str(d.get('@type')) for d in ld if d.get('@type')}), 'jsonld': ld, 'images': images}


def is_js_shell(html: str, text_chars: int) -> bool:
    if text_chars >= 200:
        return False
    low = html.lower()
    return len(html) > 2000 and (low.count('<script') >= 3 or 'id="root"' in low or 'id="__next"' in low or '__next_data__' in low or 'id="app"' in low)


def feed_links(html: str, base: str) -> list[str]:
    """<link rel="alternate" type="application/rss+xml|atom+xml" href=...>: how a NEW domain advertises its feed (discovery path 3)."""
    import urllib.parse
    out = []
    for m in re.finditer(r'<link\b[^>]*>', html[:200000], re.I):
        tag = m.group(0)
        if re.search(r'rel=["\']?alternate', tag, re.I) and re.search(r'type=["\']?application/(?:rss|atom)\+xml', tag, re.I):
            h = re.search(r'href=["\']([^"\']+)', tag, re.I)
            if h:
                out.append(urllib.parse.urljoin(base, h.group(1)))
    return out[:3]
