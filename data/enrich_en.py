"""English names for restaurants, chefs, kitchens. Run after enrich_3.py.
Official forms verified 2026-10-21 on the MICHELIN Guide English pages (h1 = restaurant name,
'Chef <name>' in the guide text) or the restaurant's own Instagram handle. Everything else is
automatic Revised Romanization and flagged *_en_official=False."""
import json, re, os
from rr import person, place
here=os.path.dirname(os.path.abspath(__file__)); P=os.path.join(here,'seed.json')
d=json.load(open(P,encoding='utf-8'))
M=lambda r:(r.get('michelin_src') or '').replace('/kr/ko/','/kr/en/')
# restaurant names as printed on MICHELIN Guide English pages (h1)
MN={'trid':'trid','orzo':'Osteria Orzo','legume':'Légume','on':'ON','atomix':'Atomix','atoboy':'Atoboy','jua':'Jua','moono':'Moono',
 'mingles_r':'Mingles','mosu_r':'Mosu','san':'SAN','jungsik_r':'Jungsik','soigne_r':'Soigné','evett_r':'Evett','kwon_r':'Kwonsooksoo',
 'allen_r':'Restaurant Allen','sosuheon':'Sosuheon','layeon_r':'La Yeon','mitou_r':'Mitou','allaprima_r':'alla prima','jueun':'JUEUN',
 'lamant':"L'Amant Secret",'eatanic':'Eatanic Garden','yeast':"y'east",'vinho':'VINHO','onjium_r':'Onjium'}
# names from the restaurant's own Instagram handle (lowercase handle -> display form)
IG={'soseoul':('Soseoul Hannam','https://www.instagram.com/soseoul_hannam'),'bornyon':('Bornyon','https://www.instagram.com/bornyon_seoul'),
 'mils':('Mils','https://www.instagram.com/milsishappy'),'mao':('Mao','https://www.instagram.com/maophoga')}
# verified 2026-10-21 on the restaurant's own channels
OWN={'matthew':('Matthew','https://www.instagram.com/matthew.seoul/ (Instagram; Blue Ribbon magazine/280 lists \'매튜 Matthew\')'),
 'italianclub':('The Italian Club','https://the-italian-club.lovable.app/ (restaurant site)'),
 'iter':('ITER LACLASS','https://sotetsu-hotels.com/splaisir/myeong-dong/news/182/ (hotel site)')}
# translated, not official: numeric name
TR={'oct19':('October 19','translation of 10월 19일 (no official English form found)')}
# English written in the Korean name itself, e.g. '노 오이스터 (No Oyster)'
# chef names as written on MICHELIN Guide English pages: (name, url-source restaurant id)
MC={'legume':'Si-woo Sung','on':'Kim Jun-hyung','atomix':'Junghyun Park','atoboy':'Junghyun Park','jua':'Hoyoung Kim','moono':('Hoyoung Kim','jua'),
 'mingles_r':'Mingoo Kang','san':'Seong-hyun Jo','jungsik_r':'Yim Jung-sik','soigne_r':'Jun Lee','evett_r':'Joseph Lidgerwood','kwon_r':'Kwon Woo-joong',
 'sosuheon':'Kyung-jae Park','lamant':'Son Jong-won','eatanic':('Son Jong-won','lamant'),'vinho':'Sung-bin Jeon'}
byid={r['id']:r for r in d['restaurants']}
off=rom=coff=crom=0
for r in d['restaurants']:
    i=r['id']; nm=r['name']
    if i in MN: r.update(name_en=MN[i],name_en_src=M(r),name_en_official=True)
    elif i in IG: r.update(name_en=IG[i][0],name_en_src=IG[i][1]+' (Instagram handle)',name_en_official=True)
    elif i in OWN: r.update(name_en=OWN[i][0],name_en_src=OWN[i][1],name_en_official=True)
    elif i in TR: r.update(name_en=TR[i][0],name_en_src=TR[i][1],name_en_official=False)
    elif re.search(r'\(([A-Za-z][^)]*)\)',nm): r.update(name_en=re.search(r'\(([A-Za-z][^)]*)\)',nm).group(1),name_en_src='English form in source name',name_en_official=True)
    else: r.update(name_en=place(re.sub(r'\s*\(.*\)','',nm)),name_en_src='Revised Romanization (automatic)',name_en_official=False)
    off+=r['name_en_official']; rom+=not r['name_en_official']
    c=r.get('chef') or ''
    if i in MC:
        v=MC[i]; n,src=(v if isinstance(v,tuple) else (v,i))
        r.update(chef_en=n,chef_en_src=M(byid[src]),chef_en_official=True); coff+=1
    else:
        base=re.sub(r'\s*\(.*?\)','',c).strip()
        names=[x for x in re.split(r'[·,]',base) if re.fullmatch(r'[가-힣]{2,4}',x.strip())]
        if names and not re.search('미기재|출신|팀',c):
            r.update(chef_en=' & '.join(person(x.strip()) for x in names),chef_en_src='Revised Romanization (automatic)',chef_en_official=False); crom+=1
        else: r.update(chef_en=None,chef_en_src=None,chef_en_official=None)
for k in d['kitchens']:
    r=byid.get(k['id']+'_r') or next((x for x in d['restaurants'] if x.get('michelin_src') and x['michelin_src']==k.get('michelin_src')),None)
    k['name_en']=k.get('en'); k['name_en_src']=(k.get('michelin_src') or '').replace('/kr/ko/','/kr/en/') if 'michelin' in (k.get('michelin_src') or '') else 'existing data (not verified on an official page)'
    k['name_en_official']='michelin' in (k.get('michelin_src') or '')
json.dump(d,open(P,'w',encoding='utf-8'),ensure_ascii=False,indent=1)
for p in ('../web/data.js',):
    pass
print(f'restaurant names official {off} romanized {rom}; chef names official {coff} romanized {crom}')
