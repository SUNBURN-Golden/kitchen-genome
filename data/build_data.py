import json, csv
D="2026-10-20"
S={
"dafanew":"https://dafanew.com/interview/?bmode=view&idx=123802384",
"imaeil":"https://www.imaeil.com/page/view/2024082522241532110",
"nycb":"https://www.nyculturebeat.com/index.php?document_srl=4186400&mid=Lounge2",
"orzo":"https://cooknchefnews.com/news/view/1065602225638121",
"maison":"https://www.maisonkorea.com/food/2026/08/beyond-french-cuisine/",
"rfk":"https://restaurantsforkings.com/city/seoul/layered",
"mamalee":"https://alive.businessweekly.com.tw/single/Index/ARTL006004161",
"legume":"https://guide.michelin.com/kr/ko/article/people/legume-asia-s-first-ever-michelin-starred-vegan-restaurant",
"mich2026":"https://www.michelin.co.kr/categories/michelin-news-and-information/michelin-guide-seoul-busan-2026",
"mich2star":"https://guide.michelin.com/kr/ko/selection/south-korea/restaurants/3-stars-michelin/2-stars-michelin",
"mk2026":"https://www.mk.co.kr/news/culture/11981084",
"mosu":"https://mosuseoul.com/",
"mingles":"https://www.restaurant-mingles.com/team",
"r_seoul":"https://www.reddit.com/r/seoul/comments/1sjwlrt/",
"r_solo":"https://www.reddit.com/r/finedining/comments/1um4t6t/",
"r_mosu":"https://www.reddit.com/r/finedining/comments/1sgdff6/",
"r_san":"https://www.reddit.com/r/finedining/comments/1ulk26t/",
"r_yeast":"https://www.reddit.com/r/finedining/comments/1rym0iy/",
"newsis":"https://www.newsis.com/view/NISX20260202_0003498684",
"daum":"https://v.daum.net/v/20260218171523074",
"hk":"https://www.hankyung.com/article/202607230961P",
}
# source kitchens
K=[
("mingles","밍글스","Mingles","3스타 (2026)","mich2026"),
("jungsik","정식당","Jungsik","2스타 (2026)","mich2star"),
("mosu","모수","Mosu","2스타 (2026)","mich2026"),
("soigne","스와니예","Soigné","2스타 (2026)","mich2star"),
("onjium","온지음","Onjium","미쉐린 등재 (등급 재확인 필요)",None),
("kwonsooksoo","권숙수","Kwonsooksoo","2스타 (2026)","mich2star"),
("layeon","라연","La Yeon","2스타 (2026)","mich2star"),
("gaon","가온","Gaon","2026 목록 미확인",None),
("evett","에빗","Evett","2스타 (2026)","mich2star"),
("mitou","미토우","Mitou","2스타 + 그린스타 (2026)","mich2026"),
("allen","레스토랑 알렌","Restaurant Allen","2스타 (2026)","mich2star"),
("allaprima","알라 프리마","Alla Prima","2스타 (2026)","mich2star"),
]
kitchens=[dict(id=a,name=b,en=c,michelin=d,michelin_src=S.get(e) if e else None,checked=D) for a,b,c,d,e in K]
# alumni restaurants: id,name,chef,kitchen,role,opened,area,cuisine,price,michelin,michelin_src,lineage_src,verified,note,url,city
A=[
("soseoul","소설한남","엄태철","mosu","수셰프","미확인","한남","현대 한식",None,"1스타 (2022–2024, 2026 재확인 필요)","dafanew","dafanew","verified","2차 출처(매체 정리글)","https://www.instagram.com/soseoul_hannam","서울"),
("bornyon","본연","배경준","mosu","출신(직책 미기재)","2024-05","논현","화덕·발효 우드파이어 한식",None,"미확인",None,"dafanew","verified","흑백요리사 출연 셰프","https://www.instagram.com/bornyon_seoul","서울"),
("trid","트리드","강승원","mosu","출신(직책 미기재)","2020-08","청담(선릉로)","컨템퍼러리",None,"미쉐린 등재 (2024 기사 기준)","dafanew","dafanew","verified","흑백요리사 출연 셰프","https://www.instagram.com/tridseoul","서울"),
("mils","밀스","정영훈","mosu","출신(직책 미기재)","미확인","성수","베이커리·샤퀴테리",None,"미확인",None,"dafanew","verified","","https://www.instagram.com/milsishappy","서울"),
("mao","마오","정영훈","mosu","출신(직책 미기재)","미확인","성수","베트남",None,"미확인",None,"dafanew","verified","","https://www.instagram.com/maophoga","서울"),
("orzo","오스테리아 오르조","김호윤","soigne","수석 셰프","미확인","한남","이탈리안",None,"미쉐린 선정 (2020–2025)","orzo","orzo","verified","Reddit r/seoul '가성비' 추천에 언급","https://www.catchtable.co.kr","서울"),
("legume","레귬","성시우","soigne","헤드 셰프 (10년+)","2023 (미검증)","신사","비건 파인다이닝",None,"스타 보유설 (미검증)",None,"legume","unverified","미쉐린 기사 페이지 접근 차단 → 검색 요약만 확인",None,"서울"),
("on","레스토랑 온","김준형","onjium","실습 (CIA 재학 중)","2017","강남","클래식 프렌치",None,"2023 미쉐린 영 셰프 어워드","maison","maison","verified","'출신'보다 약한 연결(실습). 이후 서승호 셰프에게 사사",None,"서울"),
("layered","레이어드","박성배","mingles","출신(직책 미기재)","2022","한남/이태원","모던 한식",None,"1스타 (2차 출처, 2026 재확인 필요)","rfk","rfk","verified","해외 리뷰 사이트 1곳만 확인 → 교차검증 필요",None,"서울"),
("mamalee","마마리","송하슬람 (宋夏瑟蘭)","mingles","창립 멤버","미확인","성수","한식 반찬·주점",None,"미확인",None,"mamalee","verified","대만 매체 출처. 한글 이름 표기 재확인 필요",None,"서울"),
("atomix","아토믹스","박정현","jungsik","헤드 셰프","2018","뉴욕","한식 파인다이닝",None,"2스타 (2020–2023)","imaeil","imaeil","verified","해외(뉴욕)",None,"뉴욕"),
("atoboy","아토보이","박정현","jungsik","헤드 셰프","미확인","뉴욕","한식",None,"미확인",None,"nycb","verified","해외(뉴욕)",None,"뉴욕"),
("jua","주아","김호영","jungsik","출신(직책 미기재)","미확인","뉴욕","한식",None,"1스타 (기사 기준)","imaeil","imaeil","verified","해외(뉴욕)",None,"뉴욕"),
("moono","무노","김호영","jungsik","출신(직책 미기재)","미확인","뉴욕","한식",None,"미확인",None,"nycb","verified","해외(뉴욕)",None,"뉴욕"),
("lysee","리제","이은지","jungsik","출신(직책 미기재)","미확인","뉴욕","디저트",None,"미확인",None,"nycb","verified","해외(뉴욕)",None,"뉴욕"),
]
alumni=[]
for r in A:
    alumni.append(dict(id=r[0],name=r[1],chef=r[2],kitchen=r[3],role=r[4],opened=r[5],area=r[6],cuisine=r[7],price=r[8],
      michelin=r[9],michelin_src=S.get(r[10]) if r[10] else None,lineage_src=S[r[11]],lineage_status=r[12],note=r[13],url=r[14],city=r[15],checked=D,type="alumni"))
# established (reputation comparison). michelin_pts: 3=5,2=4,1=3,bib=2,selected=1
E=[
("mingles_r","밍글스","강민구",None,"3스타 (2026)","mich2026","런치 32만 / 디너 42만 (2026.3~)","daum",["r_seoul","r_solo"],["daum","hk"]),
("mosu_r","모수","안성재",None,"2스타 (2026)","mich2026","디너 42만","daum",["r_mosu","r_seoul"],["daum","hk","mk2026"]),
("san","레스토랑 산","조승현",None,"1스타 (2026 신규)","mk2026","디너 38만 (2026.7 인상)","r_san",["r_san","r_seoul","r_solo"],["mk2026"]),
("jungsik_r","정식당","임정식",None,"2스타 (2026)","mich2star",None,None,["r_solo"],[]),
("soigne_r","스와니예","이준",None,"2스타 (2026)","mich2star",None,None,["r_solo"],[]),
("evett_r","에빗","조셉 리저우드",None,"2스타 (2026)","mich2star",None,None,["r_solo","r_yeast"],[]),
("kwon_r","권숙수","권우중",None,"2스타 (2026)","mich2star",None,None,["r_seoul","r_solo"],[]),
("allen_r","레스토랑 알렌","(미기재)",None,"2스타 (2026)","mich2star",None,None,["r_solo"],[]),
("sosuheon","소수헌","박경재",None,"2스타 (2026 승급)","mk2026",None,None,["r_seoul"],["mk2026"]),
("layeon_r","라연","(호텔 팀)",None,"2스타 (2026)","mich2star",None,None,[],[]),
("mitou_r","미토우","(미기재)",None,"2스타 + 그린스타 (2026)","mk2026",None,None,[],["mk2026"]),
("allaprima_r","알라 프리마","(미기재)",None,"2스타 (2026)","mich2star",None,None,["r_solo"],[]),
("jueun","레스토랑 주은","(미기재)",None,"1스타 (2026 승급)","mk2026",None,None,["r_solo"],["mk2026"]),
("lamant","라망 시크레","손종원",None,"1스타 (2026 유지)","mk2026","런치 19만 / 디너 29만 (2026.3~)","newsis",[],["newsis","mk2026"]),
("eatanic","이타닉 가든","손종원",None,"1스타 (2026 유지)","mk2026",None,None,["r_seoul"],["mk2026"]),
("yeast","와이스트 (Y'east)","(미기재)",None,"1스타 (Reddit 글 제목 기준, 공식 재확인 필요)","r_yeast","디너 24만 (2026.3)","r_yeast",["r_yeast","r_solo"],[]),
]
est=[]
for r in E:
    est.append(dict(id=r[0],name=r[1],chef=r[2],kitchen=None,role=None,opened=None,area=None,cuisine=None,price=r[6],price_src=S.get(r[7]) if r[7] else None,
      michelin=r[4],michelin_src=S.get(r[5]),reddit_threads=[S[x] for x in r[8]],press=[S[x] for x in r[9]],city="서울",checked=D,type="established",lineage_status="n/a",
      bluer="미확인 (블루리본 사이트에서 수동 확인 예정)"))
for a in alumni:
    a["reddit_threads"]=[S["r_seoul"]] if a["id"]=="orzo" else []
    a["press"]=[a["lineage_src"]]
    a["bluer"]="미확인 (블루리본 사이트에서 수동 확인 예정)"
json.dump(dict(generated=D,sources=S,kitchens=kitchens,restaurants=alumni+est),open("data/seed.json","w"),ensure_ascii=False,indent=1)
with open("data/seed_restaurants.csv","w",newline="") as f:
    w=csv.writer(f); cols=["id","type","name","chef","kitchen","role","opened","area","cuisine","city","price","michelin","michelin_src","lineage_status","lineage_src","note","checked"]
    w.writerow(cols)
    for r in alumni+est: w.writerow([r.get(c) or "" for c in cols])
open("site/data.js","w").write("window.SEED="+json.dumps(dict(sources=S,kitchens=kitchens,restaurants=alumni+est),ensure_ascii=False)+";")
print(len(kitchens),len(alumni),len(est),sum(a["lineage_status"]=="verified" for a in alumni))
