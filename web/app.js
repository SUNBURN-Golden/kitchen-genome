// 판정(과대평가/저평가)은 데이터가 모일 때까지 비공개. true로 바꾸면 카드·배지·필터·#/gap이 다시 보여요.
const SHOW_VERDICTS=false;
const SHOW_NEWSLETTER=false;
const {kitchens,restaurants,sources}=window.SEED;const K=Object.fromEntries(kitchens.map(k=>[k.id,k]));
const $=s=>document.querySelector(s);const esc=s=>(s??'').toString().replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const alumni=restaurants.filter(r=>r.type==='alumni');const ver=r=>r.lineage_status==='verified';
// 점수 로직은 초안(site/app.js)과 같아요
function stars(m){m=m||'';if(/3스타/.test(m))return 3;if(/2스타/.test(m))return 2;if(/1스타/.test(m))return 1;return 0}
function expertPts(m){m=m||'';const s=stars(m);if(s)return s+2; if(/빕/.test(m))return 2; if(/선정|등재/.test(m))return 1;return 0}
function rising(r){const k=K[r.kitchen];let ped=stars(k?.michelin)*10+5; if(/헤드|수석|수셰프|창립/.test(r.role))ped+=10; if(/실습/.test(r.role))ped-=10;
 const y=parseInt(r.opened);const rec=isNaN(y)?5:(y>=2022?20:y>=2017?8:2);const noStar=stars(r.michelin)?0:(/선정|등재/.test(r.michelin)?10:20);const price=0;
 let t=ped+rec+noStar+price; if(!ver(r))t*=0.5; return {t:Math.round(t),ped,rec,noStar,price}}
// 판정 공식 v2: 전문가(미쉐린 공식 등급)와 화제성(계보 출처 기사 제외)을 0–1로 맞춰 비교. 한쪽이라도 없으면 null → 판정 대기
const EXP={'3 star':1,'2 star':0.8,'1 star':0.6,'bib':0.4,'plate':0.2};const BMAX=5,TH=0.15;
function expertN(r){const m=r.michelin_official;if(m&&EXP[m]!=null)return EXP[m];if(!r.official_source_url&&!('michelin_official' in r)){const s=stars(r.michelin);if(s)return EXP[s+' star'];}return null}
function buzzN(r){const n=[...new Set([...(r.reddit_threads||[]),...(r.press||[])])].filter(u=>u&&u!==r.lineage_src).length;return n?Math.min(n,BMAX)/BMAX:null}
const r2=x=>Math.round(x*100)/100;
function gap(r){const e=expertN(r),b=buzzN(r);if(e==null||b==null)return {e,b,g:null};return {e:r2(e),b:r2(b),g:r2(e-b)}}
const vk=g=>g>TH?'under':g<-TH?'over':'even';const VT={under:'저평가',over:'과대평가',even:'균형'};
const badge=r=>ver(r)?'<span class="tag v">출처 확인</span>':r.lineage_status==='unverified'?'<span class="tag">미검증</span>':r.lineage_status==='conflict'?'<span class="tag">출처 상충</span>':r.lineage_status==='related'?'<span class="tag">관련 이력</span>':'';
const link=(u,t)=>u?`<a href="${u}" target="_blank" rel="noopener">${t||'출처'}</a>`:'<span>출처 없음</span>';
const sponsor=t=>`<aside class="sponsor" aria-label="Sponsored"><b>Sponsored</b>${t||'광고 자리예요. 순위와 점수에는 영향을 주지 않아요.'}</aside>`;
const news=()=>!SHOW_NEWSLETTER?"":`<section class="card" id="news"><h2 style="margin-top:0">뉴스레터</h2><p class="muted">새로 문 연 계보 식당과 셰프 이동 소식을 한 주에 한 번 보내 드려요. 지금은 준비 중이에요.</p><div class="news"><input type="email" placeholder="이메일 주소" aria-label="이메일 주소" disabled><button class="btn p" disabled>구독하기</button></div></section>`;
const cut=(t,n)=>{t=(t??'').toString();return t.length>n?t.slice(0,n-1)+'…':t};
const legend=`<div class="legend"><span><i></i>출처 확인</span><span><i class="d"></i>미검증</span></div>`;
// 판정: 기존 gap() 점수만 써요. 서울 밖이거나 신호가 하나도 없으면 '판정 대기'
function vd(r){const g=gap(r);if(r.city!=='서울'||g.g==null)return {k:'wait',t:'판정 대기',g};const k=vk(g.g);return {k,t:VT[k],g}}
let VF='';const shown=a=>!VF||vd(a).k===VF;
function vsum(L){if(!SHOW_VERDICTS)return L.length?'후손 '+L.length+'곳':'후손 없음';const c={};L.forEach(a=>{const t=vd(a).t;c[t]=(c[t]||0)+1});return ['저평가','과대평가','균형','판정 대기'].filter(t=>c[t]).map(t=>t+' '+c[t]).join(' · ')||'후손 없음'}
const VST={under:'fill:var(--accent);stroke:var(--accent)',over:'fill:var(--surface);stroke:var(--ink)',even:'fill:var(--surface);stroke:var(--ink-2)',wait:'fill:none;stroke:var(--dash)'};
const VTX={under:'fill:var(--on-accent)',over:'fill:var(--ink)',even:'fill:var(--ink-2)',wait:'fill:var(--ink-2)'};
function treeSVG(kid){const k=K[kid];const all=alumni.filter(a=>a.kitchen===kid);const kids=all.filter(shown);const W=1040,row=72,top=40,H=Math.max(1,kids.length)*row+top+16;
 const ky=top+(H-top-16)/2;let s=`<svg viewBox="0 0 ${W} ${H}" width="100%" role="group" aria-label="${esc(k.name)} 계보도">`;
 s+=`<text x="0" y="16" class="mono" font-size="11" style="fill:var(--ink-2)">ORIGIN</text><text x="392" y="16" class="mono" font-size="11" style="fill:var(--ink-2)">CHEF · LOCUS</text><text x="680" y="16" class="mono" font-size="11" style="fill:var(--ink-2)">INDEPENDENT · 판정</text>`;
 s+=`<line x1="0" y1="26" x2="${W}" y2="26" stroke-width="1" style="stroke:var(--line)"/>`;
 s+=`<rect x="0" y="${ky-44}" width="220" height="88" rx="12" style="fill:var(--ink)"/><text x="20" y="${ky-14}" font-size="18" font-weight="700" style="fill:var(--bg)">${esc(k.name)}</text><text x="20" y="${ky+8}" font-size="13" style="fill:var(--bg)">${esc(k.michelin)}</text><text x="20" y="${ky+30}" font-size="12" style="fill:var(--bg)">${esc(vsum(all))}</text>`;
 if(!kids.length)s+=`<text x="260" y="${ky+5}" font-size="14" style="fill:var(--ink-2)">${all.length?'이 판정에 해당하는 식당이 없어요.':'아직 확인된 독립 셰프가 없어요. 제보를 받고 있어요.'}</text>`;
 if(kids.length)s+=`<line x1="372" y1="${top+row/2-12}" x2="372" y2="${top+(kids.length-1)*row+row/2+12}" stroke-width="1" style="stroke:var(--line)"/>`;
 kids.forEach((a,i)=>{const y=top+i*row+row/2;const st=ver(a)?'stroke:var(--accent)':'stroke:var(--dash)';const d=ver(a)?'':'stroke-dasharray="6 4"';const id=String(i+1).padStart(2,'0');const v=vd(a);
  s+=`<path d="M220,${ky} C300,${ky} 300,${y} 366,${y}" fill="none" stroke-width="1.5" style="${st}" ${d}/>`;
  s+=`<rect x="366" y="${y-6}" width="12" height="12" rx="2" stroke-width="1.5" style="${ver(a)?'fill:var(--accent)':'fill:var(--surface)'};${st}"/>`;
  s+=`<text x="392" y="${y-12}" class="mono" font-size="11" style="fill:var(--ink-2)">L${id}</text><text x="392" y="${y+6}" font-size="15" font-weight="600" style="fill:var(--ink)"><title>${esc(a.chef)}</title>${esc(cut(a.chef,13))}</text><text x="392" y="${y+24}" font-size="13" style="fill:var(--ink-2)">${esc(cut(a.role,16))}</text>`;
  s+=`<path d="M600,${y} L680,${y}" stroke-width="1.5" style="${st}" ${d}/>`;
  s+=`<g class="tn" role="button" tabindex="0" data-id="${a.id}" aria-haspopup="dialog" aria-label="${esc(a.name)}${SHOW_VERDICTS?', '+v.t:''}. 근거 열기"><rect class="hit" x="680" y="${y-28}" width="360" height="56" rx="12" stroke-width="1.5" style="fill:var(--surface);${st}" ${d}/><text x="696" y="${y-4}" font-size="15" font-weight="700" style="fill:var(--ink)">${esc(cut(a.name,16))}</text><text x="696" y="${y+16}" font-size="13" style="fill:var(--ink-2)">${esc(cut(a.area+" · "+a.cuisine+" · "+a.opened+" 오픈",22))}</text>${SHOW_VERDICTS?`
  <rect x="944" y="${y-12}" width="84" height="24" rx="12" stroke-width="1.2" style="${VST[v.k]}" ${v.k==='wait'?'stroke-dasharray="3 3"':''}/><text x="986" y="${y+4}" font-size="12" font-weight="600" text-anchor="middle" style="${VTX[v.k]}">${v.t}${v.k!=='wait'?' '+(v.g.g>0?'+':'')+v.g.g:''}</text>`:''}</g>`});
 return s+'</svg>'}
const vbadge=a=>{if(!SHOW_VERDICTS)return '';const v=vd(a);return `<span class="vb ${v.k}">${v.t}${v.k!=='wait'?' '+(v.g.g>0?'+':'')+v.g.g:''}</span>`};
function treeList(){const counts=kitchens.map(k=>[k,alumni.filter(a=>a.kitchen===k.id)]).sort((a,b)=>b[1].length-a[1].length);
 return counts.map(([k,all],i)=>{const L=all.filter(shown);return `<details${i===0||VF?' open':''}><summary>${esc(k.name)} <span class="muted">${esc(vsum(all))}</span></summary>${L.length?L.map(a=>`<button type="button" class="node tn ${ver(a)?'':'u'}" data-id="${a.id}" aria-haspopup="dialog">${esc(a.chef)} → <b>${esc(a.name)}</b> ${vbadge(a)}<span>${esc(a.role)} · ${esc(a.area)} · ${esc(a.opened)} 오픈${ver(a)?'':' · 미검증'}</span></button>`).join(''):`<p class="muted">${all.length?'이 판정에 해당하는 식당이 없어요.':'아직 확인된 독립 셰프가 없어요.'}</p>`}</details>`}).join('')}
function vfilter(){if(!SHOW_VERDICTS)return '';return `<div class="chips" role="group" aria-label="판정 필터">${[['','전체'],['under','저평가만 보기'],['over','과대평가만 보기'],['even','균형만 보기']].map(([k,t])=>`<button type="button" class="chip ${VF===k?'on':''}" aria-pressed="${VF===k}" data-vf="${k}">${t}</button>`).join('')}</div>`}
function panel(id){const a=restaurants.find(x=>x.id===id);const v=vd(a);const dl=$('#panel');
 dl.innerHTML=`<form method="dialog" class="pclose"><button class="btn g" aria-label="닫기">닫기</button></form><p class="mono" style="margin:0">근거 패널</p><h2 id="ptitle" style="margin:var(--s1) 0">${esc(a.name)} ${vbadge(a)}</h2>
 <p class="muted why">${!SHOW_VERDICTS?'':v.k==='wait'?'판정 대기 · 비교할 신호가 아직 없거나 서울 밖이에요':'전문가 '+v.g.e+' − 화제성 '+v.g.b+' = '+(v.g.g>0?'+':'')+v.g.g}  · ${esc(a.michelin)} · ${link(a.michelin_src,'미쉐린 출처')}</p>
 <p class="why">출신 주방: <a href="#/k/${a.kitchen}">${esc(K[a.kitchen].name)}</a> · ${esc(a.role)} · ${link(a.lineage_src,'계보 출처')}</p>
 ${SHOW_VERDICTS?'<div class="note muted ph">[빈 자리] 준태님 한 줄 평 (작성 전)</div>':''}<p><a class="btn g" href="#/r/${a.id}">식당 페이지로</a></p>`;
 dl.setAttribute('aria-labelledby','ptitle');dl.showModal();dl.querySelector('.pclose button').focus()}
function bindTree(){document.querySelectorAll('.tn').forEach(n=>{n.onclick=()=>panel(n.dataset.id);if(n.tagName==='g')n.onkeydown=e=>{if(e.key==='Enter'||e.key===' '){e.preventDefault();panel(n.dataset.id)}}});
 document.querySelectorAll('[data-vf]').forEach(b=>b.onclick=()=>{VF=b.dataset.vf;route.keep=1;route();route.keep=0;const f=document.querySelector(`[data-vf="${VF}"]`);f&&f.focus()})}
function verdict(){if(!SHOW_VERDICTS)return news();const L=alumni.filter(a=>a.city==='서울'&&ver(a)&&a.official_source_url).map(r=>({r,g:gap(r)})).filter(x=>x.g.g!=null).sort((a,b)=>Math.abs(b.g.g)-Math.abs(a.g.g));if(!L.length)return news();const {r,g}=L[0];const v=VT[vk(g.g)];
 return `<section class="card verdict" aria-label="이번 주 판정"><p class="mono" style="margin:0">이번 주 판정 · 초안</p>
 <h2 style="margin:var(--s1) 0"><a href="#/r/${r.id}" style="color:var(--ink);text-decoration:none">${esc(r.name)}</a> <span class="vd">${v} ${g.g>0?'+':''}${g.g}</span></h2>
 <p class="why muted" style="margin:0">전문가 ${g.e} − 화제성 ${g.b} · ${esc(r.michelin)} · ${link(r.michelin_src,'미쉐린 출처')}</p>
 <a class="chip lb" href="#/k/${r.kitchen}">${esc(K[r.kitchen].name)} 출신 · ${esc(cut(r.role,14))} → 계보 보기</a>
 <div class="note muted ph">[빈 자리] 준태님 한 줄 평 (작성 전)</div>
 <a class="muted" href="#/gap" style="display:inline-flex;min-height:var(--tap);align-items:center">과대평가 vs 저평가 전체 보기</a></section>${news()}`}
function home(sel){const counts=kitchens.map(k=>[k,alumni.filter(a=>a.kitchen===k.id).length]).sort((a,b)=>b[1]-a[1]);sel=sel||counts[0][0].id;
 return `${verdict()}<h1>그 셰프, 어느 주방에서 왔을까요?</h1><p class="lead">서울 최고 주방을 거친 셰프들이 어디서 자기 가게를 열었는지 출처와 함께 정리했어요.</p>
 <div class="cta"><a class="btn p" href="#lineage">계보 보기</a></div>
 <section id="lineage"><h2>계보도 <span class="mono">LINEAGE MAP</span></h2>${legend}${vfilter()}
 <div class="tree-d"><div class="chips" role="tablist">${counts.map(([k,n])=>`<button class="chip ${k.id===sel?'on':''}" onclick="location.hash='#/k/${k.id}'">${esc(k.name)} ${n}</button>`).join('')}</div><div class="card">${treeSVG(sel)}</div></div>
 <div class="tree-m">${treeList()}</div></section>
 <p class="muted">원조 주방 ${kitchens.length}곳 · 계보 식당 ${alumni.length}곳(출처 확인 ${alumni.filter(ver).length}, 미검증 ${alumni.filter(a=>!ver(a)).length}) · 비교용 레스토랑 ${restaurants.length-alumni.length}곳 · 확인일 2026-10-20</p>
 ${sponsor()}`}
function dir(){const ks=[...new Set(alumni.map(a=>a.kitchen))];return `<h1>디렉토리</h1><div class="filters"><select id="fk" aria-label="원조 주방"><option value="">원조 주방 전체</option>${ks.map(k=>`<option value="${k}">${K[k].name}</option>`).join('')}</select>
 <select id="fc" aria-label="도시"><option value="">도시 전체</option><option>서울</option><option>뉴욕</option></select><select id="ft" aria-label="유형"><option value="">유형 전체</option><option value="alumni">계보 식당</option><option value="established">비교용 레스토랑</option></select><select id="fv" aria-label="검증"><option value="">검증 전체</option><option value="verified">출처 확인</option><option value="unverified">미검증</option></select></div><div id="list"></div>`}
function renderList(){const f={k:$('#fk').value,c:$('#fc').value,t:$('#ft').value,v:$('#fv').value};
 const L=restaurants.filter(r=>(!f.k||r.kitchen===f.k)&&(!f.c||r.city===f.c)&&(!f.t||r.type===f.t)&&(!f.v||r.lineage_status===f.v));
 $('#list').innerHTML=`<p class="muted">${L.length}곳</p><ul class="rows">`+L.map(r=>`<li><span class="n"></span><a href="#/r/${r.id}">${esc(r.name)}</a><span>${badge(r)}</span><span class="why">${esc(r.chef)}${r.kitchen?' · '+esc(K[r.kitchen].name)+' '+esc(r.role):''}${r.area?' · '+esc(r.area):''} · ${esc(r.michelin)}</span></li>`).join('')+'</ul>'}
function risingPage(){const L=alumni.filter(a=>a.city==='서울').map(a=>({a,s:rising(a)})).sort((x,y)=>y.s.t-x.s.t);
 return `<p class="draft" role="note">비공개 초안이에요. 데이터를 모으는 중이라 순위가 바뀔 수 있어요.</p><h1>다음에 뜰 집</h1><p class="lead">출신 주방의 별 수와 직책, 오픈 연도, 아직 별이 없는지를 더한 초안 점수예요. 가격은 수집 전이라 0점이고, 미검증 계보는 절반만 반영해요. 서울만 넣었어요.</p>
 <ol class="rows">${L.map((x,i)=>`<li><span class="n">${i+1}</span><a href="#/r/${x.a.id}">${esc(x.a.name)}</a><span class="sc">${x.s.t}점</span><span class="why">출신 ${x.s.ped} + 최근성 ${x.s.rec} + 별 없음 ${x.s.noStar} + 가격 ${x.s.price}${ver(x.a)?'':' × 0.5(미검증)'} · ${esc(K[x.a.kitchen].name)} ${esc(x.a.role)} · ${link(x.a.lineage_src,'계보 출처')}</span></li>`).join('')}</ol>${SHOW_VERDICTS?'<p><a class="btn g" href="#/gap">과대평가 vs 저평가 보기</a></p>':''}${sponsor()}`}
function gapPage(){const L=restaurants.filter(r=>r.city==='서울').map(r=>({r,g:gap(r)})).filter(x=>x.g.g!=null).sort((a,b)=>b.g.g-a.g.g);
 return `<p class="draft" role="note">비공개 초안이에요. 데이터를 모으는 중이라 순위가 바뀔 수 있어요.</p><h1>과대평가 vs 저평가</h1><p class="lead">미쉐린 등급 점수에서 화제성(Reddit 스레드와 기사 수, 직접 모은 표본)을 뺐어요. 플러스면 저평가(평가에 비해 덜 알려짐), 마이너스면 과대평가(평가보다 화제가 큼)로 봐요. 표본이 작아서 참고로만 봐 주세요.</p>
 <div class="note muted">준태님 코멘트 자리 · 이번 달 요약(작성 예정)</div>
 <ol class="rows">${L.map(({r,g},i)=>`<li><span class="n">${i+1}</span><a href="#/r/${r.id}">${esc(r.name)}</a><span class="sc">${g.g>0?'+':''}${g.g}</span><span class="why">${VT[vk(g.g)]} · 전문가 ${g.e} − 화제성 ${g.b} · ${esc(r.michelin)} ${link(r.michelin_src,'미쉐린 출처')}</span></li>`).join('')}</ol>`}
function rpage(id){const r=restaurants.find(x=>x.id===id);if(!r)return '<h1>찾는 식당이 없어요</h1>';const g=gap(r);
 return `<h1>${esc(r.name)} ${badge(r)}</h1><p class="lead">${esc(r.chef)}${r.area?' · '+esc(r.area):''}${r.cuisine?' · '+esc(r.cuisine):''}${r.city?' · '+esc(r.city):''}</p>
 <div class="note muted">준태님 한 줄 평 자리(작성 예정)</div>
 ${r.type==='alumni'?`<section class="card"><h2 style="margin-top:0">계보</h2><p>${esc(K[r.kitchen].name)} ${esc(r.role)} → ${esc(r.name)} (${esc(r.opened)})</p><p class="muted">${link(r.lineage_src,'계보 출처')} · 확인일 ${r.checked}${r.note?' · '+esc(r.note):''}</p></section>`:''}
 <section class="card"><h2 style="margin-top:0">평판</h2><dl><dt>미쉐린</dt><dd>${esc(r.michelin)} · ${link(r.michelin_src)}</dd><dt>블루리본</dt><dd>${esc(r.bluer)}</dd>
 <dt>Reddit</dt><dd>${(r.reddit_threads||[]).length?r.reddit_threads.map((u,i)=>link(u,'스레드 '+(i+1))).join(' · '):'없음'}</dd>
 <dt>언론</dt><dd>${(r.press||[]).length?r.press.map((u,i)=>link(u,'기사 '+(i+1))).join(' · '):'없음'}</dd>
 <dt>가격</dt><dd>${esc(r.price||'미수집')} ${r.price_src?link(r.price_src):''}</dd>${SHOW_VERDICTS?`<dt>차이 점수</dt><dd>${g.g==null?'판정 대기':(g.g>0?'+':'')+g.g} (초안)</dd>`:''}</dl></section>
 ${r.url?`<p><a class="btn g" href="${r.url}" target="_blank" rel="noopener">공식 채널</a></p>`:''}${sponsor()}`}
function submit(){return `<h1>제보하기</h1><p class="lead">어느 주방에서 어떤 일을 하셨는지 알려 주세요. 출처를 확인한 뒤 반영해요. 지금은 저장되지 않아요.</p><div class="card">
 <input placeholder="가게 이름" disabled><input placeholder="셰프 이름" disabled><input placeholder="거쳐 간 주방과 직책 (예: 밍글스 수셰프 2019–2022)" disabled><input placeholder="확인할 수 있는 링크" disabled><textarea rows="3" placeholder="남길 말" disabled></textarea><button class="btn p" disabled>보내기</button></div>
 <p class="muted">유료 소개는 순위·점수와 따로 운영하고 Sponsored로 표시해요.</p>`}
function route(){const h=location.hash||'#/';if(h==='#/gap'&&!SHOW_VERDICTS){location.replace('#/');return}if(h==='#lineage'||h==='#news')return;let m;
 document.querySelectorAll('nav a').forEach(a=>a.classList.toggle('on',a.getAttribute('href')===h||(h.startsWith('#/k/')&&a.getAttribute('href')==='#/')));
 const app=$('#app');if(m=h.match(/^#\/k\/(.+)/)){app.innerHTML=home(m[1]);bindTree()}else if(h==='#/dir'){app.innerHTML=dir();['#fk','#fc','#ft','#fv'].forEach(s=>$(s).onchange=renderList);renderList()}
 else if(h==='#/rising')app.innerHTML=risingPage();else if(h==='#/gap'){app.innerHTML=gapPage();document.querySelectorAll('nav a').forEach(a=>a.classList.toggle('on',a.getAttribute('href')==='#/rising'))}else if(m=h.match(/^#\/r\/(.+)/))app.innerHTML=rpage(m[1]);else if(h==='#/submit')app.innerHTML=submit();else{app.innerHTML=home();bindTree()}if(!route.keep)scrollTo(0,0)}
if(!SHOW_VERDICTS)document.querySelectorAll('[data-verdict]').forEach(e=>e.remove());
addEventListener('hashchange',route);route();
