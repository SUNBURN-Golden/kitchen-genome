// Social-login-only auth (Supabase Auth via CDN). No own email/password form.
(function(){
const C=window.KG_AUTH||{};const EN=location.pathname.startsWith('/en/');
const T=EN?{login:'Log in',logout:'Log out'}:{login:'로그인',logout:'로그아웃'};
const ready=!!(C.SUPABASE_URL&&C.SUPABASE_ANON_KEY);
const CONSENT_KEY='kg_newsletter_consent';
let sbP=null;
function sb(){if(!ready)return Promise.resolve(null);if(sbP)return sbP;sbP=new Promise((res,rej)=>{const s=document.createElement('script');s.src='https://cdn.jsdelivr.net/npm/@supabase/supabase-js@2';s.onload=()=>res(window.supabase.createClient(C.SUPABASE_URL,C.SUPABASE_ANON_KEY,{auth:{flowType:'pkce',detectSessionInUrl:true,persistSession:true}}));s.onerror=rej;document.head.appendChild(s)});return sbP}
async function header(){const nav=document.getElementById('nav');if(!nav)return;let a=nav.querySelector('[data-auth]');if(!a){a=document.createElement('a');a.dataset.auth='1';a.className='auth';nav.appendChild(a)}
a.textContent=T.login;a.href=EN?'/en/login/':'/login/';const c=await sb().catch(()=>null);if(!c)return;const {data}=await c.auth.getSession();
if(data.session){a.textContent=T.logout;a.href='#';a.onclick=async e=>{e.preventDefault();await c.auth.signOut();location.reload()}}}
async function signIn(provider){const c=await sb();if(!c)return false;const box=document.getElementById('kg-news');
localStorage.setItem(CONSENT_KEY,JSON.stringify({v:!!(box&&box.checked),at:new Date().toISOString(),ver:C.CONSENT_VERSION,lang:EN?'en':'ko'}));
const next=new URLSearchParams(location.search).get('next')||(EN?'/en/':'/');
const opt={redirectTo:location.origin+'/auth/callback/?next='+encodeURIComponent(next)};
if(provider==='kakao')opt.scopes='account_email';
await c.auth.signInWithOAuth({provider,options:opt});return true}
async function callback(){const msg=document.getElementById('kg-cb');const c=await sb().catch(()=>null);
if(!c){msg&&(msg.textContent='로그인 설정이 아직 준비되지 않았어요. / Sign-in is not configured yet.');return}
const q=new URLSearchParams(location.search);const code=q.get('code');let s=(await c.auth.getSession()).data.session;
if(!s&&code){const r=await c.auth.exchangeCodeForSession(code);s=r.data&&r.data.session}
if(!s){msg&&(msg.textContent='로그인에 실패했어요. 다시 시도해 주세요. / Sign-in failed, please try again.');return}
let cons=null;try{cons=JSON.parse(localStorage.getItem(CONSENT_KEY)||'null')}catch(e){}
const u=s.user;const {data:ex}=await c.from('profiles').select('id').eq('id',u.id).maybeSingle();
if(!ex){await c.from('profiles').insert({id:u.id,email:u.email,provider:(u.app_metadata||{}).provider||null,newsletter_opt_in:!!(cons&&cons.v),newsletter_consent_at:cons&&cons.v?cons.at:null,consent_version:cons?cons.ver:null,signup_lang:cons?cons.lang:null})}
localStorage.removeItem(CONSENT_KEY);const n=q.get('next')||'/';location.replace(n.startsWith('/')&&!n.startsWith('//')?n:'/')}
function loginPage(){const f=document.getElementById('kg-login');if(!f)return;const box=document.getElementById('kg-news');if(box)box.checked=C.NEWSLETTER_DEFAULT_CHECKED!==false;
const note=document.getElementById('kg-auth-note');if(!ready&&note)note.hidden=false;
f.querySelectorAll('[data-provider]').forEach(b=>{if(!(C.PROVIDERS||[]).includes(b.dataset.provider)){b.remove();return}if(!ready)b.disabled=true;b.addEventListener('click',()=>signIn(b.dataset.provider))})}
window.KGAuth={sb,signIn};
document.addEventListener('DOMContentLoaded',()=>{header();loginPage();if(document.getElementById('kg-cb'))callback()});
})();
