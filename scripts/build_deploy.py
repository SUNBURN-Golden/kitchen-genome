"""Build dist/deploy_kitchengenome.js from web/ + functions/ (needs: pip install blake3, npx wrangler)."""
import os, base64, json, subprocess, sys
from blake3 import blake3
here=os.path.dirname(os.path.abspath(__file__))
repo=os.path.join(here,'..')
for _s in ('build_pages.py','build_pages_en.py','build_feeds.py'):
    subprocess.run([sys.executable, os.path.join(here,_s)], check=True)
root=os.path.join(repo,'web')
mt={'html':'text/html','css':'text/css','js':'application/javascript','svg':'image/svg+xml','png':'image/png','txt':'text/plain','xml':'application/xml'}
files=[];manifest={}
for d,_,fs in os.walk(root):
    for f in sorted(fs):
        if f.startswith('_'): continue
        p=os.path.join(d,f); rel='/'+os.path.relpath(p,root)
        b64=base64.b64encode(open(p,'rb').read()).decode(); ext=f.rsplit('.',1)[-1]
        h=blake3((b64+ext).encode()).hexdigest()[:32]; manifest[rel]=h
        files.append({'key':h,'value':b64,'metadata':{'contentType':mt.get(ext,'application/octet-stream')},'base64':True})
dist_dir=os.path.join(repo,'dist')
os.makedirs(dist_dir,exist_ok=True)
worker_bundle=''
routes_json=''
if os.path.isdir(os.path.join(repo,'functions')):
    os.makedirs(os.path.join(repo,'node_modules','.cache','wrangler'),exist_ok=True)
    wb_path=os.path.join(dist_dir,'_worker.bundle')
    rj_path=os.path.join(dist_dir,'_routes.json')
    subprocess.run(['npx','wrangler','pages','functions','build','--outfile',wb_path,'--output-routes-path',rj_path], cwd=repo, check=True)
    worker_bundle=open(wb_path,'r',encoding='utf-8').read()
    routes_json=open(rj_path,'r',encoding='utf-8').read()
tpl=open(os.path.join(here,'deploy_kitchengenome.js')).read()
js=(tpl.replace('__MANIFEST__',json.dumps(manifest))
       .replace('__WORKER_BUNDLE__',json.dumps(worker_bundle))
       .replace('__ROUTES_JSON__',json.dumps(routes_json)))
out=os.path.join(dist_dir,'deploy_kitchengenome.js'); open(out,'w').write(js)
print(out, len(js), 'bytes; files:', len(manifest), 'worker:', len(worker_bundle))
