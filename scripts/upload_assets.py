"""Upload web/ assets with a Pages upload-token JWT (from the connector) read from a file. Never prints the token."""
import os,re,sys,json,base64,urllib.request
from blake3 import blake3
jwt=re.search(r'eyJ[A-Za-z0-9._-]+',open(sys.argv[1]).read()).group(0)
root=os.path.join(os.path.dirname(os.path.abspath(__file__)),'..','web')
mt={'html':'text/html','css':'text/css','js':'application/javascript','svg':'image/svg+xml','png':'image/png','txt':'text/plain','xml':'application/xml'}
files=[]
for f in sorted(os.listdir(root)):
    if f.startswith('_'):continue
    b64=base64.b64encode(open(os.path.join(root,f),'rb').read()).decode();ext=f.rsplit('.',1)[-1]
    files.append({'key':blake3((b64+ext).encode()).hexdigest()[:32],'value':b64,'metadata':{'contentType':mt.get(ext,'application/octet-stream')},'base64':True})
def call(p,body):
    r=urllib.request.Request('https://api.cloudflare.com/client/v4'+p,json.dumps(body).encode(),{'Authorization':'Bearer '+jwt,'Content-Type':'application/json'})
    try: x=urllib.request.urlopen(r);return x.status,json.load(x)
    except urllib.error.HTTPError as e: return e.code,e.read()[:200]
h=[f['key'] for f in files]
s,j=call('/pages/assets/check-missing',{'hashes':h});print('check-missing',s,j.get('result') if s==200 else j)
miss=set(j['result']) if s==200 else set(h)
up=[f for f in files if f['key'] in miss]
if up: s,j=call('/pages/assets/upload',up);print('upload',s,str(j)[:150])
s,j=call('/pages/assets/upsert-hashes',{'hashes':h});print('upsert',s,str(j)[:150])
