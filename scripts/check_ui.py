"""Offline browser checks; run python3 scripts/check_ui.py (requires Playwright)."""
import mimetypes
import sys
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
    # Inspect actual line boxes, including wrapped Korean labels, at the breakpoint edges.
    page.emulate_media(reduced_motion='reduce')
    geometry = """() => {
      const bad=[];
      if(document.documentElement.scrollWidth > innerWidth) bad.push('page overflow');
      if(innerWidth>=961) {
        // Every row must join the same root, including the final partial row.
        let previous=null;
        for(const row of document.querySelectorAll('.trow')) {
          const r=row.getBoundingClientRect(), trunk=getComputedStyle(row,'::before');
          const bar=getComputedStyle(row,'::after'), nodes=[...row.children];
          const x=r.left+parseFloat(trunk.left), top=r.top+parseFloat(trunk.top);
          const bottom=top+parseFloat(trunk.height), y=r.top+parseFloat(bar.top);
          if(trunk.content==='none' || trunk.borderLeftStyle!=='solid' || parseFloat(trunk.borderLeftWidth)<2 || top>y || bottom<y) bad.push('missing root trunk');
          if(previous && (Math.abs(previous.x-x)>.5 || Math.abs(previous.bottom-top)>.5)) bad.push('disconnected rows');
          if(Math.abs(r.left+parseFloat(bar.left)-x)>.5 || bar.borderTopStyle!=='solid' || parseFloat(bar.borderTopWidth)<2) bad.push('missing row bar');
          let end=x+parseFloat(bar.width);
          for(const node of nodes) {
            const n=node.getBoundingClientRect(), stem=getComputedStyle(node,'::after'), line=getComputedStyle(node,'::before');
            const sx=n.left+parseFloat(stem.left), sy=n.top+parseFloat(stem.top);
            if(Math.abs(end-sx)>.5 || Math.abs(sy-y)>.5 || line.borderTopStyle!=='solid' || parseFloat(line.borderTopWidth)<2) bad.push('disconnected chef stem');
            end=n.left+parseFloat(line.left)+parseFloat(line.width);
          }
          previous={x,bottom};
        }
      }
      for(const col of document.querySelectorAll('.tnode')) {
        const cr=col.getBoundingClientRect(), rects=[];
        if(innerWidth>=961 && cr.width<120) bad.push('narrow chef column');
        const walker=document.createTreeWalker(col,NodeFilter.SHOW_TEXT);
        while(walker.nextNode()) {
          const node=walker.currentNode;if(!node.textContent.trim()) continue;
          const range=document.createRange();range.selectNodeContents(node);
          for(const r of range.getClientRects()) {
            if(r.width && r.height) {
              if(r.left < cr.left-.5 || r.right > cr.right+.5) bad.push('text outside column: '+node.textContent);
              rects.push(r);
            }
          }
        }
        for(let i=0;i<rects.length;i++) for(let j=i+1;j<rects.length;j++) {
          const a=rects[i],b=rects[j];
          if(Math.min(a.right,b.right)-Math.max(a.left,b.left)>.5 && Math.min(a.bottom,b.bottom)-Math.max(a.top,b.top)>.5) bad.push('text overlap');
        }
      }
      for(const el of document.querySelectorAll('.hero button,.hero select,nav a')) {
        const r=el.getBoundingClientRect();if(r.width<44 || r.height<44) bad.push('small target');
      }
      return bad;
    }"""
    assert page.locator('#kname a').count() == 0
    kitchen_ids = page.locator('#k option').evaluate_all('els => els.map(el => el.value)')
    for scheme in ['light', 'dark']:
        page.emulate_media(color_scheme=scheme)
        for kid in kitchen_ids:
            page.goto('http://kg.test/#/k/' + kid)
            page.wait_for_function('(kid) => document.querySelector("#k")?.value === kid', arg=kid)
            for width in ([1440,1280,1100,1024,961,960,900,860,768,390,320] if kid in ['jungsik','mingles'] else [961,390]):
                page.set_viewport_size({'width': width, 'height': 900})
                page.evaluate('document.fonts.ready')
                assert page.evaluate(geometry) == [], (kid, scheme, width, page.evaluate(geometry))
    # Exercise eight chef groups without modifying the source dataset.
    for scheme in ['light', 'dark']:
        page.emulate_media(color_scheme=scheme)
        page.goto('http://kg.test/#/k/mingles')
        page.reload()  # Reset the browser-only extra group between colour schemes.
        expect(page.locator('.tnode')).to_have_count(7)
        for count in [7,8]:
            if count==8:
                page.locator('.trow').last.evaluate('el => el.append(document.querySelector(".tnode").cloneNode(true))')
            for width in [1440,1280,1100,1024,961,960,390,320]:
                page.set_viewport_size({'width':width,'height':900})
                page.evaluate('document.fonts.ready')
                assert page.evaluate(geometry) == [], (count,'groups',scheme,width,page.evaluate(geometry))
                if width>=961:
                    assert page.locator('.trow').first.evaluate('el => getComputedStyle(el).gridTemplateColumns.split(" ").length') == 6
                    assert page.locator('.tnode').evaluate_all('els => els[6].getBoundingClientRect().top > els[0].getBoundingClientRect().top')
    page.goto('http://kg.test/#/k/jungsik')
    expect(page.locator('.tn')).to_have_count(8)
    page.set_viewport_size({'width': 1440, 'height': 900})
    if '--screenshots' in sys.argv:
        shots = Path(sys.argv[sys.argv.index('--screenshot-dir')+1]) if '--screenshot-dir' in sys.argv else WEB.parent / 'shots'
        shots.mkdir(parents=True,exist_ok=True)
        for kid,tag in [('jungsik','home'),('mingles','mingles')]:
            page.goto('http://kg.test/#/k/'+kid)
            page.wait_for_function('(kid) => document.querySelector("#k")?.value === kid', arg=kid)
            for scheme in ['light','dark']:
                page.emulate_media(color_scheme=scheme)
                for width in [1440,390]:
                    page.set_viewport_size({'width':width,'height':900 if width==1440 else 844})
                    page.evaluate('document.fonts.ready')
                    page.screenshot(path=str(shots / f'{tag}-{width}-{scheme}.png'),full_page=True)
        page.goto('http://kg.test/#/k/jungsik')
        expect(page.locator('.tn')).to_have_count(8)
        page.set_viewport_size({'width':1440,'height':900})
    first = page.locator('.tn').first
    first.click()
    assert page.locator('#panel').evaluate('(el) => el.open')
    page.keyboard.press('Escape')
    assert first.evaluate('(el) => el === document.activeElement')
    page.goto('http://kg.test/#/dir')
    page.locator('#fk').select_option('jungsik')
    assert page.locator('#list .rows > li').count() == 8
    for path in ['/#/dir','/#/rising','/#/r/atomix','/#/submit','/k/jungsik/', '/r/atomix/', '/directory/', '/en/', '/en/directory/', '/en/r/atomix/', '/login/', '/en/login/']:
        page.goto('http://kg.test' + path)
        marker={'/#/dir':'#fk','/#/rising':'.draft','/#/r/atomix':'.lead.parts','/#/submit':'textarea'}.get(path)
        if marker: page.locator(marker).wait_for()
        page.evaluate('document.fonts.ready')
        assert page.locator('h1').count() == 1
        for scheme in ['light','dark']:
            page.emulate_media(color_scheme=scheme)
            for width in [1440,768,390,320]:
                page.set_viewport_size({'width':width,'height':900})
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), (path,scheme,width,'overflow')
                small = page.locator('a,button,input,select,textarea').evaluate_all("""els => els.filter(el => {
                  const r=el.getBoundingClientRect();return r.width && r.height && (r.width<44 || r.height<44)
                }).map(el=>el.outerHTML)""")
                assert not small, (path,scheme,width,small)
    page.emulate_media(color_scheme='dark', reduced_motion='reduce')
    assert page.evaluate('getComputedStyle(document.body).backgroundColor') == 'rgb(27, 25, 21)'
    assert not errors, errors
    browser.close()
print('UI checks passed: fonts, tree dialog/focus, directory filter, KO/EN pages, dark mode')
