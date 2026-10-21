import json,csv
d=json.load(open("data/seed.json"));S=d["sources"];D="2026-10-20"
S.update({
"matthew_blog":"https://m.blog.naver.com/seongsudonggorilla/224146581965",
"metizen":"https://metizen.co.kr/%EB%84%88%EC%9D%98-%EC%B2%AB-%ED%8C%8C%EC%9D%B8%EB%8B%A4%EC%9D%B4%EB%8B%9D-%EC%97%AC%EA%B8%B0%EB%A1%9C-%EB%81%9D-%EB%A7%A4%ED%8A%9C-2/",
"bluer304":"http://www.bluer.co.kr/magazine/304","bluer288":"http://www.bluer.co.kr/magazine/288",
"esq_rising":"https://www.esquirekorea.co.kr/article/1879916","mk_mowa":"https://www.mk.co.kr/news/business/11251921",
"foodnews":"https://www.foodnews.news/news/article_print.html?no=199288","polle":"https://polle.com/posts/tag/%EC%B2%AD%EB%8B%B4%EB%8F%99%EB%A7%9B%EC%A7%91",
"ktrip_goum":"https://www.ktriptips.com/kor/food/4068175","sumuk_blog":"https://zoomtrend.com/posts/seoul-hansik-fine-dining-summukdang-review",
"iter":"https://m.blog.naver.com/dks900409/223517649576","heypop":"https://heypop.kr/n/65239/",
"winein_odd":"https://winein.co.kr/%eb%b4%84%eb%b0%94%eb%9e%8c-%eb%94%b0%eb%9d%bc-%ed%95%9c-%ec%9e%94-%ed%85%8c%eb%9d%bc%ec%8a%a4%ea%b0%80-%ed%8a%b9%eb%b3%84%ed%95%9c-%ec%99%80%ec%9d%b8%eb%b0%94-4/",
"joongang_gaon":"https://www.joongang.co.kr/article/25440058","economist_gaon":"https://economist.co.kr/article/view/ecn202608050001",
"esq_italian":"https://www.esquirekorea.co.kr/article/1872774","vegannews":"https://www.vegannews.co.kr/news/article.html?no=15353",
"herald2025":"https://biz.heraldcorp.com/article/10430286","cnc_psb":"https://cooknchefnews.com/news/view/1065617650444380",
"newsroad_mamalee":"http://www.newsroad.co.kr/news/articleView.html?idxno=35617","bluer2027":"https://www.bluer.co.kr/magazine/675",
"bluer2025":"https://www.bluer.co.kr/magazine/475","search_only":"(검색 요약만 확인, 원문 미확인)"})
R={r["id"]:r for r in d["restaurants"]}
def add(id,name,chef,k,role,opened,area,cuisine,price,mich,lsrc,status,note,city="서울",press=None,msrc=None):
    R[id]=dict(id=id,name=name,chef=chef,kitchen=k,role=role,opened=opened,area=area,cuisine=cuisine,price=price,michelin=mich,michelin_src=S.get(msrc) if msrc else None,
      lineage_src=S[lsrc],lineage_status=status,note=note,url=None,city=city,checked=D,type="alumni",reddit_threads=[],press=[S[x] for x in (press or [lsrc])],bluer="2027 3리본 목록에 없음 (1·2리본 여부 미확인)")
# recheck weak links
R["legume"].update(opened="2023-04",michelin="1스타 (2025 승급)",michelin_src=S["herald2025"],lineage_src=S["vegannews"],lineage_status="verified",note="비건뉴스(2023)·헤럴드경제(2025) 두 매체로 교차 확인",press=[S["vegannews"],S["herald2025"]])
R["layered"].update(lineage_status="conflict",note="충돌: 해외 리뷰 사이트만 '밍글스 출신'이라 함. 국내 매체(쿡앤셰프 2025.8)에선 박성배 셰프가 온지음 헤드셰프로 재직 중 → 동명이인이거나 오류 가능. 계보에서 제외",press=[S["rfk"],S["cnc_psb"]])
R["mamalee"].update(note="대만 매체: 밍글스 창립 멤버. 국내 기사: 밍글스·마마리마켓 공동 업무협약, 강민구 셰프 투자 → '독립'보다 파트너십에 가까움",press=[S["mamalee"],S["newsroad_mamalee"]])
R["on"].update(note="약한 연결: CIA 재학 중 온지음 '실습'. 본인 커리어는 서승호 셰프 사사. 2017 오픈, 2026.3 확장 이전",opened="2017")
R["mao"].update(opened="2025-04 (무신사 성수 입점, 검색 요약 기준)")
# new alumni
add("matthew","매튜","이원석","mingles","헤드 셰프 (6년)","2022-06","성수","프렌치 코스","런치 7만 / 디너 12만 (2026.1 블로그)","미확인","metizen","verified","메티즌(2022)·블로그(2026) 교차",press=["metizen","matthew_blog"])
add("matteo","마테오견문록","신왕열","mingles","수셰프","2023 (블루리본 2023 새맛집)","금호동","이탈리안 생면 파스타","파스타코스 4.8만 / 테이스팅 6.5만 (2023)","미확인","bluer304","verified","고메트리 오너와 공동 운영")
add("vinho","빈호","전성빈 (+김진호 소믈리에, 장하나 디저트)","mingles","출신(직책 미기재)","2022-07","논현","와인 다이닝","디너코스 13만 (2023)","미확인 (에스콰이어: 미쉐린이 '새로운 별'이라 칭함)","bluer288","verified","세 명 모두 밍글스 출신",press=["bluer288","bluer304","esq_rising"])
add("finz","핀즈","김범주 (파티시에)","mingles","파티시에 (소나 경력도)","미확인 (압구정→성수 이전)","성수","플레이팅 디저트",None,"미확인","bluer288","verified","")
add("mowa","모와","문원기","mingles","출신 → 사브서울 수셰프","2022-11 (헤드셰프 부임)","명동 (르메르디앙)","와인 레스토랑",None,"미확인","mk_mowa","verified","오너 아님: 와인수입사 아영FBC 업장의 고용 헤드셰프")
add("midolim","미도림","박준희","jungsik","출신(직책 미기재)","미확인","성수(왕십리로)","한식 타파스 바",None,"미확인","foodnews","verified","운영사 카린지 프리젠트 (셰프 오너 아님)")
add("nooyster","노 오이스터 (No Oyster)","김기재","jungsik","출신 (도멘청담 경력도)","미확인 (2025 추정)","청담","해산물·스파클링 바",None,"미확인","polle","verified","출처가 이용자 리뷰 1건뿐 → 약함")
add("goum","고움","정식당 출신 셰프 (이름 미기재)","jungsik","출신 (10여 년 한식)","2026-04 (가오픈)","역삼","곰탕 다이닝 코스",None,"미확인","ktrip_goum","verified","전 '수묵당' 자리. 블로그상 수묵당은 정식당 수셰프 송승훈 셰프였음 → 동일인 여부 미확인",press=["ktrip_goum","sumuk_blog"])
add("iter","이테르 라클라스","노지민","kwonsooksoo","출신(직책 미기재)","미확인","명동 (호텔)","이탈리안·한식",None,"미확인","iter","verified","출처가 블로그 1건 → 약함")
add("oct19","10월 19일","박지현·윤송이 (파티시에)","kwonsooksoo","파티시에","미확인","서초","디저트 코스",None,"미확인","heypop","verified","2023 여름 이후 휴식 중이라는 기사 → 현재 운영 재확인 필요")
add("oddhouse","오드하우스","권숙수 출신 헤드셰프 (이름 미기재)","kwonsooksoo","출신","미확인","정동","와인 다이닝",None,"미확인","winein_odd","verified","")
add("gaongaok","가온가옥","박상진","gaon","2022년 합류 셰프","2026-06-26","서초","곰탕·한식",None,"미확인","joongang_gaon","related","독립 아님: 가온 운영사(가온소사이어티) 신규 브랜드",press=["joongang_gaon","economist_gaon","bluer2027"])
add("arari","아라리","이지우","gaon","2019년 합류 (디저트)","2025","북촌","한식 디저트",None,"미확인","economist_gaon","related","독립 아님: 가온소사이어티 브랜드")
add("italianclub","더 이탈리안 클럽","김호윤","soigne","수석 셰프","2024 (매장 3곳)","잠실·삼성·판교","이탈리안",None,"미확인","esq_italian","verified","오스테리아 오르조와 같은 셰프")
add("origin_numbers","오리지널 넘버스","이찬양","soigne","출신 (검색 요약)","미확인","미확인","미확인",None,"미확인","search_only","unverified","원문 미확인")
add("goni","스시 고니","조병곤","sushicho","출신 (검색 요약)","미확인","신사","스시 오마카세",None,"미확인","search_only","unverified","식신 페이지 접근 차단 → 원문 미확인")
add("odre","오드레","강창기","onjium","출신 (검색 요약)","미확인","뉴욕","한식 반상",None,"미확인","search_only","unverified","원문 미확인","뉴욕")
add("wanchun","완춘","김태완","onjium","출신 (검색 요약)","미확인","울산","캐주얼 중식",None,"미확인","search_only","unverified","원문 미확인","울산")
# new kitchen
d["kitchens"].append(dict(id="sushicho",name="스시조",en="Sushi Cho",michelin="블루리본 3리본 (2027)",michelin_src=S["bluer2027"],checked=D))
# established: bluer + prices
three=["권숙수","라연","모수","미토우","밍글스","소수헌","스와니예","알라 프리마","이타닉 가든","정식당","라망 시크레"]
P={"kwon_r":"점심 21만 / 저녁 34만 (블루리본 2024.10)","soigne_r":"런치 21만 / 디너 32만 (블루리본 2024.10)","allaprima_r":"런치 19만 / 디너 31만 (블루리본 2024.10)",
   "jungsik_r":"점심 19.5만 / 저녁 29만 (블루리본 2024.10)","eatanic":"런치 19만 / 디너 32만 (블루리본 2024.10)"}
for r in R.values():
    if r["type"]=="established":
        r["bluer"]="3리본 (2027)" if r["name"] in three else "2027 3리본 목록에 없음 (1·2리본 여부 미확인)"
        if r["name"]=="라연": r["bluer"]="3리본 (2027 신규)"
        r["bluer_src"]=S["bluer2027"]
        if r["id"] in P and not r.get("price"): r["price"]=P[r["id"]]; r["price_src"]=S["bluer2025"]
        if r["name"] in three: r["press"]=list(dict.fromkeys(r["press"]+[S["bluer2027"]]))
R["evett_r"]["press"]=[S["herald2025"]]
# onjium established entry
R["onjium_r"]=dict(id="onjium_r",name="온지음",chef="조은희 (+박성배 헤드셰프)",kitchen=None,role=None,opened=None,area="창성동",cuisine="모던 한식",price="점심 17만 / 저녁 25만 (블루리본 2024.10)",price_src=S["bluer2025"],
  michelin="미쉐린 1스타 (쿡앤셰프 2025.8 기준)",michelin_src=S["cnc_psb"],reddit_threads=[],press=[S["cnc_psb"],S["bluer2027"]],city="서울",checked=D,type="established",lineage_status="n/a",bluer="3리본 (2027)",bluer_src=S["bluer2027"])
for k in d["kitchens"]:
    if k["id"]=="onjium": k["michelin"]="1스타 (2025 기사 기준)"; k["michelin_src"]=S["cnc_psb"]
d["restaurants"]=list(R.values())
json.dump(d,open("data/seed.json","w"),ensure_ascii=False,indent=1)
cols=["id","type","name","chef","kitchen","role","opened","area","cuisine","city","price","michelin","bluer","lineage_status","lineage_src","note","checked"]
with open("data/seed_restaurants.csv","w",newline="") as f:
    w=csv.writer(f);w.writerow(cols)
    for r in d["restaurants"]: w.writerow([r.get(c) or "" for c in cols])
open("site/data.js","w").write("window.SEED="+json.dumps(dict(sources=S,kitchens=d["kitchens"],restaurants=d["restaurants"]),ensure_ascii=False)+";")
A=[r for r in d["restaurants"] if r["type"]=="alumni"]
from collections import Counter
print("kitchens",len(d["kitchens"]),"alumni",len(A),"seoul",sum(r["city"]=="서울" for r in A),Counter(r["lineage_status"] for r in A),"seoul_verified",sum(r["city"]=="서울" and r["lineage_status"]=="verified" for r in A),"established",len(d["restaurants"])-len(A))
print(Counter(r["kitchen"] for r in A if r["city"]=="서울" and r["lineage_status"]=="verified"))
