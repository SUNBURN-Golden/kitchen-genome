"""Generate static SEO pages from data/seed.json into web/r/<id>/, web/k/<id>/, web/directory/.
Public facts only: never verdicts (과대평가/저평가), gap scores, newsletter, or outreach internals."""
import os, re, json, shutil
from html import escape as e
BASE='https://kitchengenome.soulbound.co.kr'
here=os.path.dirname(os.path.abspath(__file__)); root=os.path.join(here,'..'); web=os.path.join(root,'web')
d=json.load(open(os.path.join(root,'data','seed.json'),encoding='utf-8'))
K={k['id']:k for k in d['kitchens']}; R=d['restaurants']
bad=lambda v:v is None or v=='' or bool(re.search('미확인|미기재',str(v)))
VS={'confirmed':'식당 확인 완료','contacted':'식당에 확인 요청함 (답변 대기)'}
def vstat(r): return VS.get(r.get('verification_status'),'공개 자료 기준 (식당 확인 전)')
def kname(r): k=K.get(r.get('kitchen')); return k['name'] if k else None
def chef(r): return None if bad(r.get('chef')) else r['chef']
def role(r): v=r.get('role'); return None if bad(v) or v=='출신' else v
LOGO='<svg class="mark" viewBox="0 0 24 32" width="18" height="24" aria-hidden="true"><path d="M6 2C6 10 18 10 18 16S6 22 6 30" /><path d="M18 2C18 10 6 10 6 16S18 22 18 30" /><path class="r" d="M8 6H16M8 26H16" /></svg>'
def page(path,title,desc,body,ld):
    url=BASE+path; en=BASE+'/en'+(path if path!='/' else '/')
    return f'''<!doctype html><html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="color-scheme" content="light dark">
<title>{e(title)}</title><meta name="description" content="{e(desc)}"><link rel="canonical" href="{url}"><link rel="alternate" hreflang="ko" href="{url}"><link rel="alternate" hreflang="en" href="{en}"><link rel="alternate" hreflang="x-default" href="{url}">
<meta property="og:title" content="{e(title)}"><meta property="og:description" content="{e(desc)}"><meta property="og:url" content="{url}"><meta property="og:type" content="website"><meta property="og:site_name" content="키친 게놈"><meta property="og:locale" content="ko_KR">
<meta name="theme-color" content="#FAF8F3" media="(prefers-color-scheme: light)"><meta name="theme-color" content="#161513" media="(prefers-color-scheme: dark)">
<link rel="alternate" type="application/rss+xml" title="키친 게놈" href="/rss.xml"><link rel="stylesheet" href="/style.css">
<script type="application/ld+json">{json.dumps(ld,ensure_ascii=False).replace("</","<\\/")}</script></head><body>
<header class="top"><div class="wrap bar"><a class="logo" href="/" aria-label="키친 게놈 홈">{LOGO}<span class="wm">키친 게놈<small>KITCHEN GENOME</small></span></a>
<nav id="nav" aria-label="주요 메뉴"><a href="/">계보</a><a href="/directory/">디렉토리</a><a href="/#/rising">다음에 뜰 집</a><a href="/#/submit">제보</a><a href="/en{path}" hreflang="en" lang="en">EN</a></nav></div></header>
<main class="wrap">{body}</main>
<footer class="wrap foot">키친 게놈 · 계보마다 출처와 확인일을 붙였어요. 틀린 곳이 있으면 <a href="/#/submit">제보</a>해 주세요.</footer></body></html>
'''
def link(u,t): return f'<a href="{e(u)}" rel="nofollow noopener" target="_blank">{e(t)}</a>'
def srcs(r):
    L=[]
    for lab,k in (('계보 출처','lineage_src'),('미쉐린 가이드','michelin_src'),('공식 출처','official_source_url'),('블루리본','bluer_official_url'),('가격 출처','price_src'),('폐업 출처','closed_src'),('공식 채널','url')):
        if r.get(k) and r[k] not in [u for _,u in L]: L.append((lab,r[k]))
    for i,u in enumerate(r.get('press') or []):
        if u not in [x for _,x in L]: L.append((f'보도 {i+1}',u))
    return L
def rtitle(r):
    c=chef(r); k=kname(r)
    return f"{r['name']}"+(f" {c} 셰프" if c else '')+(f" — {k} 출신" if k else '')+" | 키친 게놈"
def rdesc(r):
    c=chef(r); k=kname(r); bits=[f"{r['name']}"+(f"({', '.join(v for v in (r.get('area'),r.get('cuisine')) if not bad(v))})" if any(not bad(r.get(x)) for x in('area','cuisine')) else '')]
    if c: bits.append(f"{c} 셰프"+(f"는 {k}" if k else '')+(f" {role(r)}" if role(r) and k else '')+(' 출신' if k else ''))
    if not bad(r.get('opened')) and re.match(r'\d{4}',r['opened']): bits.append(f"{r['opened'][:7]} 오픈")
    if r.get('closed_at'): bits.append(f"{r['closed_at']} 폐업")
    return '. '.join(bits)+'. 출처와 확인 상태를 함께 정리했어요.'
def rpage(r):
    path=f"/r/{r['id']}/"; k=K.get(r.get('kitchen')); c=chef(r)
    rows=[('셰프',c or '미확인'),('출신 주방',f'<a href="/k/{k["id"]}/">{e(k["name"])}</a>' if k else '미확인'),('직책·기간',e(role(r) or '미기재')),
          ('지역',e(' · '.join(v for v in (r.get('city'),r.get('area')) if not bad(v)) or '미확인')),('요리',e(r.get('cuisine') or '미확인')),
          ('오픈',e(r.get('opened') or '미확인')),]
    if r.get('closed_at'): rows.append(('폐업',e(r['closed_at'])))
    if not bad(r.get('michelin')): rows.append(('미쉐린',e(r['michelin'])))
    rows+= [('확인 상태',e(vstat(r))),('자료 확인일',e(r.get('checked') or d['generated']))]
    rows=[(a,e(b) if a=='셰프' else b) for a,b in rows]
    sib=[x for x in R if k and x['kitchen']==k['id'] and x['id']!=r['id']]
    sibs=('<section class="card"><h2>같은 주방 출신</h2><ul>'+''.join(f'<li><a href="/r/{x["id"]}/">{e(x["name"])}</a> · {e(chef(x) or "셰프 미확인")}</li>' for x in sib)+'</ul></section>') if sib else ''
    body=f'''<article class="card"><p class="muted">{('<a href="/k/'+k['id']+'/">'+e(k['name'])+' 계보</a> · ') if k else ''}식당</p>
<h1>{e(r['name'])}{' <span class="tag">폐업</span>' if r.get('closed_at') else ''}</h1>
<p class="lead">{e(rdesc(r))}</p>
<dl>{''.join(f'<dt>{a}</dt><dd>{b}</dd>' for a,b in rows)}</dl>
{f'<p class="note muted">{e(r["note"])}</p>' if r.get('note') else ''}
<h2>출처</h2><ul>{''.join(f'<li>{link(u,lab)}</li>' for lab,u in srcs(r)) or '<li>공개 출처 수집 중</li>'}</ul>
<p class="cta"><a class="btn p" href="/#/r/{e(r['id'])}">인터랙티브 계보에서 보기</a>{f' <a class="btn g" href="/k/{k["id"]}/">{e(k["name"])} 출신 전체</a>' if k else ''}</p></article>
{sibs}'''
    rest={'@type':'Restaurant','@id':BASE+path+'#restaurant','name':r['name'],'url':BASE+path}
    if not bad(r.get('cuisine')): rest['servesCuisine']=r['cuisine']
    loc=[v for v in (r.get('area'),r.get('city')) if not bad(v)]
    if loc: rest['address']={'@type':'PostalAddress','addressLocality':' '.join(loc),'addressCountry':'KR' if r.get('city')=='서울' else None}
    if rest.get('address') and rest['address']['addressCountry'] is None: del rest['address']['addressCountry']
    if r.get('url'): rest['sameAs']=[r['url']]
    g=[rest]
    if c:
        p={'@type':'Person','name':c,'jobTitle':'셰프','worksFor':{'@id':rest['@id']}}
        if k: p['alumniOf']={'@type':'Organization','name':k['name'],'url':f'{BASE}/k/{k["id"]}/'}
        g.append(p)
    return path,page(path,rtitle(r),rdesc(r),body,{'@context':'https://schema.org','@graph':g})
def kpage(k):
    path=f"/k/{k['id']}/"; al=[r for r in R if r.get('kitchen')==k['id']]
    title=f"{k['name']} 출신 셰프와 식당 {len(al)}곳 | 키친 게놈"
    desc=f"{k['name']}({k.get('en','')}) 주방을 거친 셰프들이 연 식당 {len(al)}곳을 출처와 함께 정리했어요."+(f" {k['michelin']}." if not bad(k.get('michelin')) else '')
    lis=''.join(f'<li><a href="/r/{r["id"]}/">{e(r["name"])}</a>{" <span class=tag>폐업</span>" if r.get("closed_at") else ""} · {e(chef(r) or "셰프 미확인")}{(" · "+e(role(r))) if role(r) else ""} · {e(" ".join(v for v in (r.get("area"),r.get("cuisine")) if not bad(v)))}</li>' for r in al)
    body=f'''<article class="card"><p class="muted">출신 주방</p><h1>{e(k['name'])} <small class="muted">{e(k.get('en',''))}</small></h1>
<p class="lead">{e(desc)}</p><dl><dt>미쉐린</dt><dd>{e(k.get('michelin') or '미확인')}{(' · '+link(k['michelin_src'],'출처')) if k.get('michelin_src') else ''}</dd><dt>자료 확인일</dt><dd>{e(k.get('checked') or d['generated'])}</dd></dl>
<h2>출신 셰프가 연 식당</h2><ul>{lis or '<li>아직 수집된 기록이 없어요</li>'}</ul>
<p class="cta"><a class="btn p" href="/#/k/{e(k['id'])}">인터랙티브 계보도 보기</a> <a class="btn g" href="/directory/">전체 디렉토리</a></p></article>'''
    ld={'@context':'https://schema.org','@type':'Restaurant','@id':BASE+path+'#kitchen','name':k['name'],'alternateName':k.get('en'),'url':BASE+path,
        'subjectOf':{'@type':'ItemList','numberOfItems':len(al),'itemListElement':[{'@type':'ListItem','position':i+1,'url':f'{BASE}/r/{r["id"]}/','name':r['name']} for i,r in enumerate(al)]}}
    return path,page(path,title,desc,body,ld)
def dirpage():
    path='/directory/'; desc=f"서울 최고 주방 {len(K)}곳 출신 셰프들이 연 식당 {len(R)}곳 전체 목록. 출신 주방별로 정리했어요."
    sec=''.join(f'<section class="card"><h2><a href="/k/{k["id"]}/">{e(k["name"])}</a> <small class="muted">{sum(r.get("kitchen")==k["id"] for r in R)}곳</small></h2><ul>'+''.join(f'<li><a href="/r/{r["id"]}/">{e(r["name"])}</a> · {e(chef(r) or "셰프 미확인")}</li>' for r in R if r.get('kitchen')==k['id'])+'</ul></section>' for k in d['kitchens'])
    other=[r for r in R if r.get('kitchen') not in K]
    if other: sec+='<section class="card"><h2>출신 주방 미확인</h2><ul>'+''.join(f'<li><a href="/r/{r["id"]}/">{e(r["name"])}</a></li>' for r in other)+'</ul></section>'
    body=f'<h1>디렉토리</h1><p class="lead">{e(desc)}</p>{sec}<p class="cta"><a class="btn p" href="/#/dir">인터랙티브 디렉토리</a></p>'
    ld={'@context':'https://schema.org','@type':'CollectionPage','name':'키친 게놈 디렉토리','url':BASE+path}
    return path,page(path,'셰프 계보 식당 디렉토리 | 키친 게놈',desc,body,ld)
for sub in ('r','k','directory'): shutil.rmtree(os.path.join(web,sub),ignore_errors=True)
out=[rpage(r) for r in R]+[kpage(k) for k in d['kitchens']]+[dirpage()]
for path,html in out:
    p=os.path.join(web,path.strip('/'),'index.html'); os.makedirs(os.path.dirname(p),exist_ok=True); open(p,'w',encoding='utf-8').write(html)
print('pages:',len(out))
