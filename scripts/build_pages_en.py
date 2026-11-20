"""English static pages under web/en/ from data/seed.json (run after enrich_en.py data exists).
Public facts only: never verdicts (과대평가/저평가), gap scores, newsletter, or outreach internals."""
import os, re, json, shutil, sys
from html import escape as e
sys.path.insert(0,os.path.dirname(os.path.abspath(__file__)))
from en_terms import AREA, CITY, CUISINE, ROLE, michelin, VSTAT
from lineage_evidence import evidence_html
CHK='<span class="tag">Checking</span>'
def chk(r,f): return (' '+CHK) if f in (r.get('career_checking') or []) else ''
def lineage(r):
    return evidence_html(r, 'Lineage evidence', {'lineage':'Origin kitchen', 'affiliation':'Opening or affiliation', 'location':'Location'}, 'Source')
BASE='https://kitchengenome.soulbound.co.kr'
here=os.path.dirname(os.path.abspath(__file__)); root=os.path.join(here,'..'); web=os.path.join(root,'web')
d=json.load(open(os.path.join(root,'data','seed.json'),encoding='utf-8'))
K={k['id']:k for k in d['kitchens']}; R=d['restaurants']
kn=lambda k:k.get('name_en') or k.get('en') or k['name']
def kof(r): return K.get(r.get('kitchen'))
def area(r):
    a=AREA.get(r.get('area')); return a or CITY.get(r.get('city')) or r.get('city')
def cuisine(r): return CUISINE.get(r.get('cuisine'))
def role(r): return ROLE.get(r.get('role'))
def opened(r):
    m=re.match(r'\d{4}(-\d{2})?(-\d{2})?',str(r.get('opened') or '')); return m and m.group(0)
LOGO='<svg class="mark" viewBox="0 0 24 32" width="18" height="24" aria-hidden="true"><path d="M6 2C6 10 18 10 18 16S6 22 6 30" /><path d="M18 2C18 10 6 10 6 16S18 22 18 30" /><path class="r" d="M8 6H16M8 26H16" /></svg>'
def page(kopath,title,desc,body,ld):
    en=BASE+'/en'+kopath; ko=BASE+kopath
    return f'''<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="color-scheme" content="light dark">
<title>{e(title)}</title><meta name="description" content="{e(desc)}"><link rel="canonical" href="{en}">
<link rel="alternate" hreflang="ko" href="{ko}"><link rel="alternate" hreflang="en" href="{en}"><link rel="alternate" hreflang="x-default" href="{ko}">
<meta property="og:title" content="{e(title)}"><meta property="og:description" content="{e(desc)}"><meta property="og:url" content="{en}"><meta property="og:type" content="website"><meta property="og:site_name" content="Kitchen Genome"><meta property="og:locale" content="en_US"><meta property="og:locale:alternate" content="ko_KR">
<meta name="theme-color" content="#FAF8F3" media="(prefers-color-scheme: light)"><meta name="theme-color" content="#161513" media="(prefers-color-scheme: dark)">
<link rel="alternate" type="application/rss+xml" title="Kitchen Genome" href="/en/rss.xml"><link rel="stylesheet" href="/style.css">
<script type="application/ld+json">{json.dumps(ld,ensure_ascii=False).replace("</","<\\/")}</script></head><body>
<header class="top"><div class="wrap bar"><a class="logo" href="/en/" aria-label="Kitchen Genome home">{LOGO}<span class="wm">Kitchen Genome<small>SEOUL CHEF LINEAGE</small></span></a>
<nav id="nav" aria-label="Main"><a href="/en/">Home</a><a href="/en/directory/">Directory</a><a href="/#/">Lineage map</a><a href="{kopath}" hreflang="ko" lang="ko">한국어</a></nav></div></header>
<main class="wrap">{body}</main>
<footer class="wrap foot">Kitchen Genome · Every link between a kitchen and a chef cites a public source and a check date. Spotted an error? <a href="/#/submit">Tell us</a> (form in Korean).</footer><script src="/auth.js"></script></body></html>
'''
def link(u,t): return f'<a href="{e(u)}" rel="nofollow noopener" target="_blank">{e(t)}</a>'
def srcs(r):
    L=[]
    for lab,k in (('Lineage source','lineage_src'),('MICHELIN Guide','michelin_src'),('Official source','official_source_url'),('Blue Ribbon','bluer_official_url'),('Price source','price_src'),('Closure source','closed_src'),('Official channel','url')):
        if r.get(k) and r[k] not in [u for _,u in L]: L.append((lab,r[k]))
    for i,u in enumerate(r.get('press') or []):
        if u not in [x for _,x in L]: L.append((f'Press {i+1}',u))
    return L
def vstat(r): return VSTAT.get(r.get('verification_status'),'Based on public sources (not yet confirmed by the restaurant)')
def rtitle(r):
    k=kof(r); c=r.get('chef_en')
    t=r['name_en']+(f" by Chef {c}" if c else '')+(f" — {kn(k)} Alumni Restaurant" if k else '')
    return t+(f", {AREA.get(r.get('area'),'').split(',')[0]}" if AREA.get(r.get('area')) else '')+' | Kitchen Genome'
def rdesc(r):
    k=kof(r); c=r.get('chef_en'); a=area(r); cu=cuisine(r)
    s=f"{r['name_en']}"+(f" is a {cu} restaurant" if cu else ' is a restaurant')+(f" in {a}" if a else '')+'.'
    if c and k: s+=f" Chef {c} trained at {kn(k)}"+(f" ({role(r)})" if role(r) else '')+'.'
    elif k: s+=f" Its chef came from {kn(k)}."
    if opened(r): s+=f" Opened {opened(r)}."
    if r.get('closed_at'): s+=f" Closed {r['closed_at']}."
    return s+' Sources and verification status included.'
def rpage(r):
    kp=f"/r/{r['id']}/"; k=kof(r); c=r.get('chef_en')
    LBL={'romanized':' <small class="muted">(romanized)</small>','transliterated':' <small class="muted">(transliterated)</small>'}
    nm_note=LBL.get(r.get('name_en_type'),'')
    rows=[('Chef',e(c)+LBL.get(r.get('chef_en_type'),'') if c else 'Not confirmed'),
          ('Trained at',(f'<a href="/en/k/{k["id"]}/">{e(kn(k))}</a>' if k else 'Not confirmed')+chk(r,'kitchen')),('Role there',e(role(r) or 'Not stated in sources')+chk(r,'role')),
          ('Location',e(area(r) or 'Not confirmed')),('Cuisine',e(cuisine(r) or 'Not confirmed')),('Opened',e(opened(r) or 'Not confirmed'))]
    if r.get('closed_at'): rows.append(('Closed',e(r['closed_at'])))
    if michelin(r.get('michelin')): rows.append(('MICHELIN',e(michelin(r['michelin']))))
    rows+=[('Korean name',f'<span lang="ko">{e(r["name"])}</span>'),('Verification',e(vstat(r))),('Data checked',e(r.get('checked') or d['generated']))]
    sib=[x for x in R if k and x.get('kitchen')==k['id'] and x['id']!=r['id']]
    sibs=('<section class="card"><h2>Other '+e(kn(k))+' alumni</h2><ul>'+''.join(f'<li><a href="/en/r/{x["id"]}/">{e(x["name_en"])}</a>'+(f' · Chef {e(x["chef_en"])}' if x.get('chef_en') else '')+'</li>' for x in sib)+'</ul></section>') if sib else ''
    body=f'''<article class="card"><p class="muted">{('<a href="/en/k/'+k['id']+'/">'+e(kn(k))+' lineage</a> · ') if k else ''}Restaurant</p>
<h1>{e(r['name_en'])}{nm_note}{(' <span class="tag v">Source verified</span>' if r.get('lineage_status')=='verified' else ' '+CHK) if r.get('lineage_evidence') else ''}{' <span class="tag">Closed</span>' if r.get('closed_at') else ''}</h1>
<p class="lead">{e(rdesc(r))}</p>
<dl>{''.join(f'<dt>{a}</dt><dd>{b}</dd>' for a,b in rows)}</dl>
{('<h2>Chef employment history</h2><ul>'+''.join(f'<li>{e(c["date"])} · {e(c["text_en"])} ({link(c["src"],"source" if True else "")})</li>' for c in r.get('chef_career',[]))+'</ul>') if r.get('chef_career') else ''}{('<h2>Mentorship</h2><ul>'+''.join(f'<li>{e(c.get("text_en",""))} ({link(c["src"],"source")})</li>' for c in r.get('chef_mentorship',[]))+'</ul>') if r.get('chef_mentorship') else ''}{lineage(r)}{aisec(r)}<h2>Sources</h2><ul>{''.join(f'<li>{link(u,lab)}</li>' for lab,u in srcs(r)) or '<li>Collecting public sources</li>'}</ul>
<p class="cta"><a class="btn p" href="/#/r/{e(r['id'])}">Open in the interactive map (Korean)</a>{f' <a class="btn g" href="/en/k/{k["id"]}/">All {e(kn(k))} alumni</a>' if k else ''}</p></article>{sibs}'''
    rest={'@type':'Restaurant','@id':BASE+'/en'+kp+'#restaurant','name':r['name_en'],'alternateName':r['name'],'url':BASE+'/en'+kp,'inLanguage':'en'}
    if cuisine(r): rest['servesCuisine']=cuisine(r)
    if area(r): rest['address']={'@type':'PostalAddress','addressLocality':area(r)}|({'addressCountry':'KR'} if r.get('city') in ('서울','울산') else {})
    if r.get('url'): rest['sameAs']=[r['url']]
    g=[rest]
    if c:
        p={'@type':'Person','name':c,'jobTitle':'Chef'}
        if 'kitchen' not in (r.get('career_checking') or []): p['worksFor']={'@id':rest['@id']}
        if not bad_chef(r): p['alternateName']=re.sub(r'\s*\(.*?\)','',r['chef']).strip()
        if k and 'kitchen' not in (r.get('career_checking') or []): p['alumniOf']={'@type':'Organization','name':kn(k),'url':f'{BASE}/en/k/{k["id"]}/'}
        g.append(p)
    return '/en'+kp,page(kp,rtitle(r),rdesc(r),body,{'@context':'https://schema.org','@graph':g})
def bad_chef(r): return bool(re.search('미기재|출신|팀',r.get('chef') or ''))
def kpage(k):
    kp=f"/k/{k['id']}/"; al=[r for r in R if r.get('kitchen')==k['id']]
    title=f"{kn(k)} Alumni Restaurants in Seoul — {len(al)} Places by {kn(k)}-Trained Chefs | Kitchen Genome"
    mi=michelin(k.get('michelin'))
    desc=f"Where did chefs from {kn(k)} go? {len(al)} restaurants opened or led by {kn(k)} alumni, with sources."+(f" {kn(k)}: {mi}." if mi else '')
    lis=''.join(f'<li><a href="/en/r/{r["id"]}/">{e(r["name_en"])}</a>{" <span class=tag>Closed</span>" if r.get("closed_at") else ""}'+(f' · Chef {e(r["chef_en"])}' if r.get('chef_en') else '')+(f' · {e(role(r))}' if role(r) else '')+(f' · {e(area(r))}' if area(r) else '')+((' '+CHK) if r.get('career_checking') else '')+'</li>' for r in al)
    body=f'''<article class="card"><p class="muted">Origin kitchen</p><h1>{e(kn(k))} <small class="muted" lang="ko">{e(k['name'])}</small></h1>
<p class="lead">{e(desc)}</p><dl><dt>MICHELIN</dt><dd>{e(mi or 'Not confirmed')}{(' · '+link(k['michelin_src'].replace('/kr/ko/','/kr/en/'),'source')) if k.get('michelin_src') else ''}</dd><dt>Data checked</dt><dd>{e(k.get('checked') or d['generated'])}</dd></dl>
<h2>Restaurants by {e(kn(k))} alumni</h2><ul>{lis or '<li>No records yet</li>'}</ul>{aisec(k)}
<p class="cta"><a class="btn p" href="/#/k/{e(k['id'])}">See the lineage tree (Korean)</a> <a class="btn g" href="/en/directory/">Full directory</a></p></article>'''
    ld={'@context':'https://schema.org','@type':'Restaurant','@id':BASE+'/en'+kp+'#kitchen','name':kn(k),'alternateName':k['name'],'url':BASE+'/en'+kp,
        'subjectOf':{'@type':'ItemList','name':f'{kn(k)} alumni restaurants','numberOfItems':len(al),'itemListElement':[{'@type':'ListItem','position':i+1,'url':f'{BASE}/en/r/{r["id"]}/','name':r['name_en']} for i,r in enumerate(al)]}}
    return '/en'+kp,page(kp,title,desc,body,ld)
AI_LABEL_EN={'career': 'Career', 'employment_claim': 'Employment', 'opening': 'Opening', 'closure': 'Closure', 'relocation': 'Relocation', 'rename': 'Rename', 'menu': 'Menu', 'collab': 'Collaboration', 'mentorship_claim': 'Mentorship'}
def aisec(x):
    """Findings published by the DeepSeek review stage: verbatim (Korean) source quote + source link, always badged."""
    items=x.get('ai_reviewed') or []
    if not items: return ''
    return ('<h2>AI-reviewed updates</h2><ul class="ai-reviewed">'+''.join(f'<li><span class="tag ai">AI-reviewed</span> {e(AI_LABEL_EN.get(i["label"],i["label"]))} · “{e(i["quote"])}”'+(f' · {e(i["date"])}' if i.get('date') else '')+f' ({link(i["src"],"source")})</li>' for i in items)
            +'</ul><p class="muted">Published automatically after an AI review of the original quote; not yet checked by a person. Quotes are shown in the original Korean.</p>')
def dirpage():
    kp='/directory/'; desc=f"All {len(R)} restaurants opened or led by alumni of {len(K)} top Seoul kitchens, grouped by where the chef trained."
    sec=''.join(f'<section class="card"><h2><a href="/en/k/{k["id"]}/">{e(kn(k))}</a> <small class="muted">{sum(r.get("kitchen")==k["id"] for r in R)}</small></h2><ul>'+''.join(f'<li><a href="/en/r/{r["id"]}/">{e(r["name_en"])}</a>'+(f' · Chef {e(r["chef_en"])}' if r.get('chef_en') else '')+((' '+CHK) if r.get('career_checking') else '')+'</li>' for r in R if r.get('kitchen')==k['id'])+'</ul></section>' for k in d['kitchens'])
    body=f'<h1>Directory</h1><p class="lead">{e(desc)}</p>{sec}'
    return '/en'+kp,page(kp,'Seoul Chef Lineage Directory — Restaurants by Alumni of Top Kitchens | Kitchen Genome',desc,body,{'@context':'https://schema.org','@type':'CollectionPage','name':'Kitchen Genome directory','url':BASE+'/en'+kp,'inLanguage':'en'})
def home():
    sr=[r for r in R if r.get('city')=='서울']
    desc=f"Who trained where, and which restaurants they opened or lead: {len(R)} restaurants by alumni of {len(K)} acclaimed kitchens such as Mingles, Jungsik and Mosu, each with sources."
    ks=''.join(f'<li><a href="/en/k/{k["id"]}/">{e(kn(k))}</a> · {sum(r.get("kitchen")==k["id"] for r in R)} alumni restaurants</li>' for k in d['kitchens'])
    body=f'''<section class="card"><h1>Kitchen Genome: Seoul’s chef family tree</h1>
<p class="lead">Great restaurants train great cooks, and many of them go on to open places of their own. Kitchen Genome follows those paths: which chefs worked at Seoul’s most acclaimed kitchens, and where they cook now.</p>
<p>If you loved a tasting menu at a MICHELIN-starred restaurant, the chef who ran its kitchen line may now serve a smaller, more affordable menu across town. We track {len(R)} restaurants ({len(sr)} in Seoul) tied to {len(K)} origin kitchens. Every link cites a public source, such as the MICHELIN Guide, press interviews or the restaurant’s own channels, and shows when we last checked it. Where a restaurant has not yet confirmed a detail, we say so.</p>
<p class="cta"><a class="btn p" href="/en/directory/">Browse all restaurants</a> <a class="btn g" href="/#/">Open the interactive lineage map (Korean)</a></p></section>
<section class="card"><h2>Start from a kitchen you know</h2><ul>{ks}</ul></section>
<section class="card"><h2>How to read it</h2><p>Names use the restaurant’s or chef’s official English form where we found one (MICHELIN Guide, the restaurant’s site or Instagram). Some follow the spelling used in English-language press or guides. Otherwise Korean words use Revised Romanization, marked “romanized”, and loanwords get a plain English spelling, marked “transliterated”. Roles and dates come from the cited sources; “not confirmed” means we could not find them yet.</p></section>'''
    ld={'@context':'https://schema.org','@type':'WebSite','name':'Kitchen Genome','alternateName':'키친 게놈','url':BASE+'/en/','inLanguage':'en','description':desc}
    return '/en/',page('/','Kitchen Genome — Seoul Chef Lineage: Where Top Kitchens’ Alumni Cook Now',desc,body,ld)
shutil.rmtree(os.path.join(web,'en'),ignore_errors=True)
os.makedirs(os.path.join(web,'en','login'),exist_ok=True); shutil.copyfile(os.path.join(os.path.dirname(os.path.abspath(__file__)),'templates','en_login.html'),os.path.join(web,'en','login','index.html'))
out=[home()]+[rpage(r) for r in R]+[kpage(k) for k in d['kitchens']]+[dirpage()]
for path,html in out:
    p=os.path.join(web,path.strip('/'),'index.html'); os.makedirs(os.path.dirname(p),exist_ok=True); open(p,'w',encoding='utf-8').write(html)
print('en pages:',len(out))
