// Kitchen Genome manual deploy for the Cloudflare connector's `execute` tool (user-Cloudflare).
// Do not run as-is: `python3 scripts/build_deploy.py` fills __FILES__/__MANIFEST__ from web/
// and writes dist/deploy_kitchengenome.js; pass that file's content as the execute `code`.
// Project: kitchengenome (account aa6bf084982ee841c3ea39b45895a15e) -> https://kitchengenome.soulbound.co.kr
async () => {
const PROJECT='kitchengenome';
const files=__FILES__;
const manifest=__MANIFEST__;
// Site-wide noindex while rankings/verdicts are draft. Keep this.
const headersTxt="/*\n  X-Robots-Tag: noindex, nofollow\n  X-Content-Type-Options: nosniff\n  Referrer-Policy: strict-origin-when-cross-origin\n";
const tk=await cloudflare.request({method:'GET',path:`/accounts/${accountId}/pages/projects/${PROJECT}/upload-token`});
const jwt=tk.result.jwt;const out={};
const call=async(path,body)=>{const r=await fetch('https://api.cloudflare.com/client/v4'+path,{method:'POST',headers:{'Authorization':'Bearer '+jwt,'Content-Type':'application/json'},body:JSON.stringify(body)});return {s:r.status,j:await r.json()};};
const hashes=files.map(f=>f.key);
const cm=await call('/pages/assets/check-missing',{hashes});out.cmStatus=cm.s;
const miss=new Set(cm.j.result||[]);const toUp=files.filter(f=>miss.has(f.key));out.uploaded=toUp.length;
if(toUp.length){const up=await call('/pages/assets/upload',toUp);out.upload={s:up.s,ok:up.j.success,e:up.j.errors};}
const uh=await call('/pages/assets/upsert-hashes',{hashes});out.upsert={s:uh.s,ok:uh.j.success};
const b='----F'+Date.now();
const parts=[['manifest',JSON.stringify(manifest)],['branch','main'],['_headers',headersTxt]];
const body=parts.map(([n,v])=>n==='_headers'?`--${b}\r\nContent-Disposition: form-data; name="_headers"; filename="_headers"\r\nContent-Type: application/octet-stream\r\n\r\n${v}\r\n`:`--${b}\r\nContent-Disposition: form-data; name="${n}"\r\n\r\n${v}\r\n`).join('')+`--${b}--\r\n`;
const d=await cloudflare.request({method:'POST',path:`/accounts/${accountId}/pages/projects/${PROJECT}/deployments`,body,contentType:'multipart/form-data; boundary='+b,rawBody:true});
out.deploy={s:d.status,ok:d.success,e:d.errors,id:d.result&&d.result.id,url:d.result&&d.result.url,env:d.result&&d.result.environment};
return out;
}
