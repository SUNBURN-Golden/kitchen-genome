// Social-login-only auth via Cloudflare Pages Functions (/auth/*, /api/me). No passwords, no third-party JS.
(function(){
const EN=location.pathname.startsWith('/en/');
const T=EN?{login:'Log in',logout:'Log out',soon:'Coming soon'}:{login:'로그인',logout:'로그아웃',soon:'준비 중'};
let meP=null;
function me(){if(!meP)meP=fetch('/api/me',{credentials:'same-origin'}).then(r=>r.ok?r.json():null).catch(()=>null);return meP}
async function header(){const nav=document.getElementById('nav');if(!nav)return;let a=nav.querySelector('[data-auth]');if(!a){a=document.createElement('a');a.dataset.auth='1';a.className='auth';nav.appendChild(a)}
a.textContent=T.login;a.href=(EN?'/en/login/':'/login/')+'?next='+encodeURIComponent(location.pathname);const m=await me();
if(m&&m.user){a.textContent=T.logout;a.href='/auth/logout?next='+encodeURIComponent(location.pathname);a.onclick=async e=>{e.preventDefault();await fetch('/auth/logout',{method:'POST',credentials:'same-origin'}).catch(()=>{});location.reload()}}}
async function loginPage(){const f=document.getElementById('kg-login');if(!f)return;const box=document.getElementById('kg-news');
const next=new URLSearchParams(location.search).get('next')||(EN?'/en/':'/');const m=await me();const cfg=(m&&m.config)||{};
let anyOff=false;
f.querySelectorAll('[data-provider]').forEach(b=>{const p=b.dataset.provider;
 if(!cfg[p]){anyOff=true;b.disabled=true;b.setAttribute('aria-disabled','true');b.textContent+=' · '+T.soon;return}
 b.addEventListener('click',()=>{location.href='/auth/'+p+'/start?news='+(box&&box.checked?'1':'0')+'&next='+encodeURIComponent(next)})});
const note=document.getElementById('kg-auth-note');if(note&&anyOff&&!cfg.google)note.hidden=false;
if(m&&m.user&&note){note.hidden=false;note.textContent=(EN?'Signed in as ':'로그인됨: ')+m.user.email}}
window.KGAuth={me};
document.addEventListener('DOMContentLoaded',()=>{header();loginPage()});
})();
