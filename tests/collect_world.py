"""A SYNTHETIC web for the collection-bot acceptance tests. Every domain ends in .test and every name is fictional: this proves the machinery
(discovery, evaluation, fetch of html/pdf/image, gaps, change detection, extractor alerts, resume, idempotency), NOT accuracy on real press."""
import datetime as dt
import glob
import io
import json
import os
import re
import urllib.parse

import pytest

from kg_collect.net import PoliteNet, Response

TODAY = dt.date(2026, 10, 27)
RFC = 'Mon, 12 Oct 2026 09:00:00 +0900'
SEED = {
    'generated': '2026-10-27', 'sources': {}, 'kitchens': [],
    'restaurants': [
        {'id': 'r_dal', 'name': '달빛정원', 'name_en': 'Dalbit Garden', 'chef': '한도윤', 'chef_en': 'Han Doyun', 'kitchen': None, 'role': '총괄셰프', 'opened': None, 'area': '성수', 'cuisine': '한식',
         'type': 'established', 'lineage_status': 'n/a', 'url': 'https://dalbit.test/', 'checked': '2026-10-01', 'city': '서울'},
        {'id': 'r_mor', 'name': '모래시계', 'name_en': 'Sandglass', 'chef': '서하람', 'chef_en': 'Seo Haram', 'kitchen': None, 'role': '셰프', 'opened': '2023-03', 'area': '한남', 'cuisine': '프렌치',
         'type': 'established', 'lineage_status': 'n/a', 'url': 'https://sandglass.test/', 'checked': '2026-10-01', 'city': '서울'},
        {'id': 'r_chu', 'name': '청우헌', 'name_en': 'Cheongwooheon', 'chef': '이름 미기재', 'kitchen': None, 'role': None, 'opened': '2018-01', 'area': '성수', 'cuisine': '한식',
         'type': 'established', 'lineage_status': 'n/a', 'checked': '2026-10-01', 'city': '서울'},
    ],
}


def font_path():
    for pat in ('/usr/share/fonts/**/GothicA1-Regular.ttf', '/usr/share/fonts/**/Nanum*.ttf', '/usr/share/fonts/**/*CJK*.tt?'):
        g = glob.glob(pat, recursive=True)
        if g:
            return g[0]
    return None


def menu_png(lines):
    from PIL import Image, ImageDraw, ImageFont
    fp = font_path()
    if not fp:
        pytest.skip('no Korean font on this machine')
    f = ImageFont.truetype(fp, 44)
    im = Image.new('RGB', (1500, 120 + 90 * len(lines)), 'white')
    d = ImageDraw.Draw(im)
    for i, t in enumerate(lines):
        d.text((60, 60 + 90 * i), t, font=f, fill='black')
    b = io.BytesIO()
    im.save(b, 'PNG')
    return b.getvalue()


def scanned_pdf(lines):
    from PIL import Image
    im = Image.open(io.BytesIO(menu_png(lines))).convert('RGB')
    b = io.BytesIO()
    im.save(b, 'PDF', resolution=150)
    return b.getvalue()


def html(title, body, head='', pub='2026-10-20'):
    return (f'<html><head><meta charset="utf-8"><title>{title}</title>{head}<meta property="article:published_time" content="{pub}T09:00:00+09:00"></head>'
            f'<body><article><h1>{title}</h1>{body}</article></body></html>').encode()


def rss(items):
    x = ''.join(f'<item><title>{t}</title><link>{u}</link><description>{d}</description><pubDate>{RFC}</pubDate></item>' for t, u, d in items)
    return f'<?xml version="1.0" encoding="utf-8"?><rss version="2.0"><channel><title>t</title>{x}</channel></rss>'.encode()


class FakeWeb:
    """transport(url, headers, timeout) -> Response. `pages` can be mutated between runs (v1 -> v2)."""

    def __init__(self):
        self.hits = []
        self.pages = {}
        self.server_down = set()
        self._build()

    def _build(self):
        P = self.pages
        pad = ' 이 문서는 수집 봇 시험을 위한 가상의 안내문이며 실제 식당과 무관하다. 모든 이름과 주소는 지어낸 것이다. 문서의 길이를 채우기 위한 문장이 이어진다. 시험용 문서는 여기서 끝난다.'
        # official site of 달빛정원 (official chain: home -> about -> menu pdf/image; SNS + booking are leads only)
        P['https://dalbit.test/'] = ('text/html', html('달빛정원', '<p>달빛정원 공식 홈페이지입니다. 계절의 한식 코스를 선보입니다.' + pad + '</p>'
                                     '<a href="/about">about 소개</a> <a href="/files/menu.pdf">메뉴 PDF</a> <a href="https://instagram.com/dalbit_test">instagram</a> '
                                     '<a href="https://catchtable.co.kr/dalbit">예약</a> <a href="/members">로그인</a> <img src="/img/menu.png" alt="가을 메뉴">'))
        P['https://dalbit.test/about'] = ('text/html', html('달빛정원 소개', '<p>달빛정원은 2021년 5월 성수동에 문을 열었다. 한도윤 셰프는 청우헌 출신이다. 한도윤 셰프는 달빛정원의 총괄 셰프로 있다.' + pad + '</p>'))
        P['https://dalbit.test/members'] = ('text/html', '<html><body><form><h1>로그인</h1><input type="password" name="pw"><p>회원가입 아이디 비밀번호를 입력하세요. 이 페이지는 로그인이 필요합니다. 회원 전용 서비스입니다 로그인 하십시오.</p></form></body></html>'.encode())
        P['https://dalbit.test/blank'] = ('text/html', b'')
        P['https://dalbit.test/files/menu.pdf'] = ('application/pdf', None)          # filled lazily (needs a Korean font)
        P['https://dalbit.test/img/menu.png'] = ('image/png', None)
        # a reference directory with two pages (pagination) pointing to press articles
        P['https://dir.test/list?p=1'] = ('text/html', html('성수 레스토랑 디렉터리 1쪽', '<p>성수 지역 한식 레스토랑 디렉터리 첫 번째 쪽. 달빛정원 청우헌 등이 있다.' + pad + '</p>'
                                         '<a href="https://press1.test/a/1">기사1</a> <a rel="next" href="/list?p=2">다음</a>'))
        P['https://dir.test/list?p=2'] = ('text/html', html('성수 레스토랑 디렉터리 2쪽', '<p>성수 지역 한식 레스토랑 디렉터리 두 번째 쪽. 모래시계와 달빛정원도 소개되어 있다.' + pad + '</p>'
                                         '<a href="https://press2.test/n/9">기사9</a>'))
        # press articles on hosts NOT in any initial list
        for i, txt in enumerate([
            '서하람 셰프는 가온재 출신이다. 서하람 셰프는 모래시계의 헤드셰프로 있다. 모래시계는 한남동에 있는 프렌치 레스토랑이다.',
            '가온재 출신 오세준 셰프가 합류해 새 메뉴를 준비한다. 한도윤 셰프는 청우헌 출신이다.',
            '달빛정원은 2021년 5월 성수동에 문을 열었다. 한도윤 셰프는 달빛정원의 총괄 셰프로 있다.']):
            P[f'https://press1.test/a/{i + 1}'] = ('text/html', html(f'셰프 소식 {i + 1}', f'<p>{txt}{pad}</p>', head='<link rel="alternate" type="application/rss+xml" href="/feed.xml">'))
        P['https://press1.test/feed.xml'] = ('application/rss+xml', rss([(f'셰프 소식 {i}', f'https://press1.test/a/{i}', '서하람 셰프 한도윤 셰프 소식') for i in (1, 2, 3)]))
        P['https://press2.test/n/9'] = ('text/html', html('성수 신규 오픈', '<p>한도윤 셰프가 이끄는 달빛정원이 가을 메뉴를 공개했다. 서하람 셰프는 모래시계의 헤드셰프로 있다.' + pad + '</p>'))
        # search endpoint: ANY query returns the same three leads (the bot dedups by url/job id)
        self.search_items = [('서하람 셰프 모래시계 헤드셰프', 'https://press1.test/a/1', '서하람 셰프 가온재 출신 모래시계'),
                             ('가온재 출신 오세준 셰프 합류', 'https://press1.test/a/2', '오세준 셰프 가온재 출신'),
                             ('달빛정원 한도윤 셰프 근황', 'https://press1.test/a/3', '달빛정원 한도윤 셰프')]

    def fill_binary(self):
        self.pages['https://dalbit.test/files/menu.pdf'] = ('application/pdf', scanned_pdf(['달빛정원 가을 코스 메뉴', '한우 육회 전복 구이', '제주 흑돼지 정식', '송이버섯 솥밥 된장찌개', '배숙 후식 차 한잔']))
        self.pages['https://dalbit.test/img/menu.png'] = ('image/png', menu_png(['달빛정원 가을 메뉴 안내', '송이 솥밥 한우 갈비찜 후식']))

    def __call__(self, url, headers=None, timeout=20):
        self.hits.append(url)
        u = urllib.parse.urlparse(url)
        host = u.netloc
        if host in self.server_down:
            return Response(url, 503, {}, b'busy')
        if u.path == '/robots.txt':
            return Response(url, 404, {}, b'')
        if host == 'search.test':
            return Response(url, 200, {'content-type': 'application/rss+xml'}, rss(self.search_items))
        key = url
        if key not in self.pages:
            key = url.rstrip('/') if url.rstrip('/') in self.pages else url
        if key in self.pages:
            ct, body = self.pages[key]
            if body is None:
                self.fill_binary()
                ct, body = self.pages[key]
            return Response(url, 200, {'content-type': ct + ('; charset=utf-8' if ct.startswith('text') else '')}, body)
        return Response(url, 404, {}, b'not found')

    def net(self):
        return PoliteNet(transport=self, sleep=lambda s: None, clock=lambda: 0.0, min_delay=0)


def seeds_for_world():
    from kg_collect import discover
    s = discover.load_seeds()
    s = json.loads(json.dumps(s))
    s['keyword_search']['endpoint'] = 'https://search.test/rss?q={q}'
    s['directories'] = [{'id': 'dir_test', 'url': 'https://dir.test/list?p=1', 'kind': 'reference'}]
    s['feeds'], s['sitemaps'] = [], []
    return s
