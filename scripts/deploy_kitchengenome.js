// Kitchen Genome manual deploy for the Cloudflare connector's `execute` tool (user-Cloudflare).
// Step 3 of 3 (creates the deployment). Asset upload from inside the connector returns 403,
// so assets are uploaded from the box first (scripts/upload_assets.py). See README.
// `python3 scripts/build_deploy.py` fills __MANIFEST__ and writes dist/deploy_kitchengenome.js.
// Project: kitchengenome (account aa6bf084982ee841c3ea39b45895a15e) -> https://kitchengenome.soulbound.co.kr
async () => {
const PROJECT='kitchengenome';
const manifest=__MANIFEST__;
// Site-wide noindex while rankings/verdicts are draft. Keep this.
const headersTxt="/*\n  X-Robots-Tag: noindex, nofollow\n  X-Content-Type-Options: nosniff\n  Referrer-Policy: strict-origin-when-cross-origin\n";
const out={};
const b='----F'+Date.now();
const parts=[['manifest',JSON.stringify(manifest)],['branch','main'],['_headers',headersTxt]];
const body=parts.map(([n,v])=>n==='_headers'?`--${b}\r\nContent-Disposition: form-data; name="_headers"; filename="_headers"\r\nContent-Type: application/octet-stream\r\n\r\n${v}\r\n`:`--${b}\r\nContent-Disposition: form-data; name="${n}"\r\n\r\n${v}\r\n`).join('')+`--${b}--\r\n`;
const d=await cloudflare.request({method:'POST',path:`/accounts/${accountId}/pages/projects/${PROJECT}/deployments`,body,contentType:'multipart/form-data; boundary='+b,rawBody:true});
out.deploy={s:d.status,ok:d.success,e:d.errors,id:d.result&&d.result.id,url:d.result&&d.result.url,env:d.result&&d.result.environment};
return out;
}
