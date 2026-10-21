# enrich_2: official Michelin URLs (verified 200 + title match on 2026-10-20), current distinctions, extra public Reddit threads.
# Blue Ribbon per-restaurant pages: not reachable (anti-crawl token) -> official bluer url left null.
import json,csv
from collections import Counter
d=json.load(open("data/seed.json"));D="2026-10-20"
KR="https://guide.michelin.com/kr/ko/seoul-capital-area/kr-seoul/restaurant/"
NY="https://guide.michelin.com/us/en/new-york-state/new-york/restaurant/"
LBL={"3 star":"3스타","2 star":"2스타","1 star":"1스타","plate":"미쉐린 선정 (플레이트)"}
# id: (url, distinction from page dLayer, extra note)
M={"soseoul":(KR+"soseoul-hannam","1 star",""),"trid":(KR+"trid","plate",""),"orzo":(KR+"osteria-orzo","plate",""),
"legume":(KR+"legume","1 star",""),"on":(KR+"on565623","plate","공식 페이지 '온'(강남·프렌치) — 동일 업장으로 판단, 주소 대조 권장"),
"vinho":(KR+"vinho","1 star",""),"origin_numbers":(KR+"original-numbers","plate","공식 페이지 제목은 '칸티크', 본문은 '오리지널 넘버스' → 상호 변경 가능성"),
"atomix":(NY+"atomix","2 star",""),"jua":(NY+"jua","1 star",""),"atoboy":(NY+"atoboy","plate",""),"moono":(NY+"moono","plate",""),
"mingles_r":(KR+"mingles","3 star",""),"mosu_r":(KR+"mosu","2 star",""),"san":(KR+"san","1 star",""),"jungsik_r":(KR+"jungsik511965","2 star",""),
"soigne_r":(KR+"soigne","2 star",""),"evett_r":(KR+"evett","2 star",""),"kwon_r":(KR+"kwonsooksoo","2 star",""),"allen_r":(KR+"restaurant-allen","2 star",""),
"sosuheon":(KR+"sosuheon","2 star",""),"layeon_r":(KR+"la-yeon","2 star",""),"mitou_r":(KR+"mitou","2 star","그린스타"),"allaprima_r":(KR+"alla-prima","2 star",""),
"jueun":(KR+"restaurant-jueun","1 star",""),"lamant":(KR+"l-amant-secret","1 star",""),"eatanic":(KR+"eatanic-garden","1 star",""),
"yeast":(KR+"y-east","1 star","공식 상호 '이스트'"),"onjium_r":(KR+"onjium","1 star","")}
KM={"mingles":"mingles_r","jungsik":"jungsik_r","mosu":"mosu_r","soigne":"soigne_r","onjium":"onjium_r","kwonsooksoo":"kwon_r","layeon":"layeon_r","evett":"evett_r","mitou":"mitou_r","allen":"allen_r","allaprima":"allaprima_r"}
RD={"vinho":["https://www.reddit.com/r/finedining/comments/1s92wyy/"],"legume":["https://www.reddit.com/r/finedining/comments/1rjq2ed/"],
"trid":["https://www.reddit.com/r/finedining/comments/1r3d33a/"],
"soseoul":["https://www.reddit.com/r/finedining/comments/1t2k2um/","https://www.reddit.com/r/finedining/comments/15va8lk/","https://www.reddit.com/r/seoul/comments/1fvspbi/"]}
R={r["id"]:r for r in d["restaurants"]}
for id,r in R.items():
    if id in M:
        u,dist,n=M[id]; lab=LBL[dist]+(" + 그린스타" if n=="그린스타" else "")
        r["michelin"]=f"{lab} (미쉐린 공식 페이지, {D} 확인)"; r["michelin_src"]=u
        r["official_source_url"]=u; r["official_source_type"]="michelin"; r["michelin_official"]=dist
        if n and n!="그린스타": r["note"]=((r.get("note") or "")+" · "+n).strip(" ·")
    else:
        r["official_source_url"]=None; r["official_source_type"]=None; r["michelin_official"]=None
        if r["city"]=="서울" and not (r.get("michelin") or "").startswith("미쉐린 1"):
            old=r.get("michelin") or ""
            r["michelin"]=f"미쉐린 서울 공식 목록에 없음 ({D} 확인)"+(f" · 이전 기재: {old}" if old and not old.startswith("미확인") else "")
    r["bluer_official_url"]=None
    has_sec=bool(r.get("press") or r.get("lineage_src") or r.get("bluer_src"))
    r["source_tier"]="official" if r["official_source_url"] else ("secondary_only" if has_sec else "none")
    if id in RD: r["reddit_threads"]=list(dict.fromkeys((r.get("reddit_threads") or [])+RD[id]))
    r["checked"]=D
for k in d["kitchens"]:
    e=KM.get(k["id"])
    if e: k["michelin"]=R[e]["michelin"].split(" (")[0]+" (2026)"; k["michelin_src"]=R[e]["official_source_url"]; k["official_source_url"]=R[e]["official_source_url"]
    else: k["official_source_url"]=None
    if k["id"]=="gaon": k["michelin"]=f"현재 미쉐린 서울 목록에 없음 ({D} 확인)"
    k["checked"]=D
# 폐업 기록 (레코드는 지우지 않고 표시만)
CLOSED={"soseoul":("2026-07-19","https://namdi.co.kr")}
for r in d["restaurants"]:
    c=CLOSED.get(r["id"])
    if c: r["closed_at"],r["closed_src"]=c
d["generated"]=D
json.dump(d,open("data/seed.json","w"),ensure_ascii=False,indent=1)
cols=["id","type","name","chef","kitchen","role","opened","area","cuisine","city","price","michelin","official_source_url","source_tier","bluer","lineage_status","lineage_src","note","checked"]
with open("data/seed_restaurants.csv","w",newline="") as f:
    w=csv.writer(f);w.writerow(cols)
    for r in d["restaurants"]: w.writerow([r.get(c) or "" for c in cols])
js="window.SEED="+json.dumps(dict(sources=d["sources"],kitchens=d["kitchens"],restaurants=d["restaurants"]),ensure_ascii=False)+";"
for p in ("site/data.js","concept-v1/data.js"): open(p,"w").write(js)
print(Counter(r["source_tier"] for r in d["restaurants"]))
