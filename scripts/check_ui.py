"""Offline browser checks; run python3 scripts/check_ui.py (requires Playwright)."""
import mimetypes
from pathlib import Path
from urllib.parse import unquote, urlparse
from playwright.sync_api import sync_playwright, expect

WEB = Path(__file__).resolve().parents[1] / 'web'


def serve(route):
    path = unquote(urlparse(route.request.url).path)
    if path == '/api/me':
        route.fulfill(json={'config': {'google': False, 'kakao': False}, 'user': None})
        return
    file = WEB / path.lstrip('/')
    if file.is_dir():
        file /= 'index.html'
    if file.is_file() and file.resolve().is_relative_to(WEB):
        route.fulfill(body=file.read_bytes(), content_type=mimetypes.guess_type(file)[0] or 'application/octet-stream')
    else:
        route.fulfill(status=404, body='Not found')


with sync_playwright() as p:
    browser = p.chromium.launch()
    page = browser.new_page(viewport={'width': 1440, 'height': 900})
    page.route('**/*', serve)
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.goto('http://kg.test/')
    page.evaluate('document.fonts.ready')
    assert page.evaluate('document.fonts.check(\'400 32px "KG Head"\', "정식당")')
    assert page.evaluate('document.fonts.check(\'600 16px "KG Body"\', "출처 확인")')
    assert page.locator('.wm small').count() == 0
    for width in [1440, 1024, 768, 390, 379, 340, 320]:
        page.set_viewport_size({'width': width, 'height': 900})
        assert page.locator('nav a').evaluate_all('els => els.every(el => {const r=el.getBoundingClientRect(); return r.width >= 44 && r.height >= 44})')
        assert page.locator('nav a').evaluate_all('els => els.every(el => {const r=document.createRange();r.selectNodeContents(el);return r.getBoundingClientRect().width <= el.getBoundingClientRect().width})')
    page.set_viewport_size({'width': 1440, 'height': 900})
    assert page.locator('#k option').count() == 13
    assert page.locator('.hero button').count() == 9  # Eight restaurant dialogs and one submit.
    page.locator('#k').select_option('mingles')
    page.locator('#pick button').click()
    page.wait_for_url('**/#/k/mingles')
    expect(page.locator('.tn')).to_have_count(7)
    page.locator('#k').select_option('jungsik')
    page.locator('#pick button').click()
    page.wait_for_url('**/#/k/jungsik')
    expect(page.locator('.tn')).to_have_count(8)
    assert page.locator('.totals dd').all_text_contents() == ['13곳', '35곳', '26', '9', '17곳', '2026-10-20']
    first = page.locator('.tn').first
    first.click()
    assert page.locator('#panel').evaluate('(el) => el.open')
    page.keyboard.press('Escape')
    assert first.evaluate('(el) => el === document.activeElement')
    page.goto('http://kg.test/#/dir')
    page.locator('#fk').select_option('jungsik')
    assert page.locator('#list .rows > li').count() == 8
    for path in ['/k/jungsik/', '/r/atomix/', '/en/', '/en/r/atomix/', '/login/', '/en/login/']:
        page.goto('http://kg.test' + path)
        page.evaluate('document.fonts.ready')
        assert page.locator('h1').count() == 1
    page.emulate_media(color_scheme='dark', reduced_motion='reduce')
    assert page.evaluate('getComputedStyle(document.body).backgroundColor') == 'rgb(27, 25, 21)'
    assert not errors, errors
    browser.close()
print('UI checks passed: fonts, tree dialog/focus, directory filter, KO/EN pages, dark mode')
