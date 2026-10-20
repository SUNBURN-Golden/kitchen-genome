"""Build dist/deploy_kitchengenome.js from web/ (needs: pip install blake3)."""
import os, base64, json
from blake3 import blake3
here=os.path.dirname(os.path.abspath(__file__)); root=os.path.join(here,'..','web')
mt={'html':'text/html','css':'text/css','js':'application/javascript','svg':'image/svg+xml','png':'image/png','txt':'text/plain'}
files=[];manifest={}
for d,_,fs in os.walk(root):
    for f in sorted(fs):
        if f.startswith('_'): continue
        p=os.path.join(d,f); rel='/'+os.path.relpath(p,root)
        b64=base64.b64encode(open(p,'rb').read()).decode(); ext=f.rsplit('.',1)[-1]
        h=blake3((b64+ext).encode()).hexdigest()[:32]; manifest[rel]=h
        files.append({'key':h,'value':b64,'metadata':{'contentType':mt.get(ext,'application/octet-stream')},'base64':True})
tpl=open(os.path.join(here,'deploy_kitchengenome.js')).read()
js=tpl.replace('__MANIFEST__',json.dumps(manifest))
os.makedirs(os.path.join(here,'..','dist'),exist_ok=True)
out=os.path.join(here,'..','dist','deploy_kitchengenome.js'); open(out,'w').write(js)
print(out, len(js), 'bytes;', manifest)
