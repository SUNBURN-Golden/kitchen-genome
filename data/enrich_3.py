# enrich_3: owner-verification fields. Run after enrich_2.py.
import json,os
d=json.load(open("data/seed.json"))
# manual updates: id -> dict(verification_status=..., contacted_at=..., confirmed_at=..., verification_note=...)
STATUS={"legume":dict(verification_status="contacted",contacted_at="2026-10-21",verification_note="email batch1_v2 via Resend 01a15b94-d707-71bd-bc8d-6cf023ece6c0"),"bornyon":dict(verification_status="contacted",contacted_at="2026-10-21",verification_note="email batch1_v2 via Resend 01a15b94-e26e-7d70-ad18-fb30b36cf543"),"trid":dict(verification_status="contacted",contacted_at="2026-10-21",verification_note="email batch1_v2 via Resend 01a15b94-e30f-7213-90d4-b136bafc20e3"),"vinho":dict(verification_status="contacted",contacted_at="2026-10-21",verification_note="email batch1_v2 via Resend 01a15b94-e53a-75bb-a8b3-ea2a520ab422")}
OK={"unverified","contacted","confirmed","correction_requested","declined","no_reply"}
for r in d["restaurants"]:
    for k,v in dict(verification_status="unverified",contacted_at=None,confirmed_at=None,verification_note="").items():
        r.setdefault(k,v)
    r.update(STATUS.get(r["id"],{}))
    assert r["verification_status"] in OK, r["id"]
json.dump(d,open("data/seed.json","w"),ensure_ascii=False,indent=1)
js="window.SEED="+json.dumps(dict(sources=d["sources"],kitchens=d["kitchens"],restaurants=d["restaurants"]),ensure_ascii=False)+";"
for p in ("site/data.js","concept-v1/data.js"):
    if os.path.exists(p): open(p,"w").write(js)
print("verification fields set on",len(d["restaurants"]))
