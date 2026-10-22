// Kitchen Genome manual deploy for the Cloudflare connector's `execute` tool (user-Cloudflare).
// Step 3 of 3 (creates the deployment). Asset upload from inside the connector returns 403,
// so assets are uploaded from the box first (scripts/upload_assets.py). See README.
// `python3 scripts/build_deploy.py` fills __MANIFEST__, __WORKER_BUNDLE__, __ROUTES_JSON__ and writes dist/deploy_kitchengenome.js.
// Project: kitchengenome (account aa6bf084982ee841c3ea39b45895a15e) -> https://kitchengenome.soulbound.co.kr
async () => {
const PROJECT='kitchengenome';
const manifest=__MANIFEST__;
const workerBundle=__WORKER_BUNDLE__;
const routesJson=__ROUTES_JSON__;
const headersTxt="/*\n  X-Content-Type-Options: nosniff\n  Referrer-Policy: strict-origin-when-cross-origin\n";
const out={};
const b='----F'+Date.now();
const fileFields={'_headers':['text/plain',headersTxt]};
if (workerBundle) fileFields['_worker.bundle']=['application/octet-stream',workerBundle];
if (routesJson) fileFields['_routes.json']=['application/json',routesJson];
const parts=[['manifest',JSON.stringify(manifest)],['branch','main']];
let body=parts.map(([n,v])=>`--${b}\r\nContent-Disposition: form-data; name="${n}"\r\n\r\n${v}\r\n`).join('');
for (const [fn,[ct,val]] of Object.entries(fileFields)) {
  body+=`--${b}\r\nContent-Disposition: form-data; name="${fn}"; filename="${fn}"\r\nContent-Type: ${ct}\r\n\r\n${val}\r\n`;
}
body+=`--${b}--\r\n`;
const d=await cloudflare.request({method:'POST',path:`/accounts/${accountId}/pages/projects/${PROJECT}/deployments`,body,contentType:'multipart/form-data; boundary='+b,rawBody:true});
out.deploy={s:d.status,ok:d.success,e:d.errors,id:d.result&&d.result.id,url:d.result&&d.result.url,env:d.result&&d.result.environment};
return out;
}
