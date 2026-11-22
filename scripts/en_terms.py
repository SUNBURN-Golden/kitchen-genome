"""Korean -> English terms for the /en/ pages. Areas follow Revised Romanization of official place names."""
AREA={'강남':'Gangnam-gu, Seoul','금호동':'Geumho-dong, Seoul','논현':'Nonhyeon-dong, Seoul','뉴욕':'New York','명동 (르메르디앙)':'Myeong-dong, Seoul (Le Méridien)',
 '명동 (호텔)':'Myeong-dong, Seoul (hotel)','북촌':'Bukchon, Seoul','서초':'Seocho-gu, Seoul','성수':'Seongsu-dong, Seoul','성수(왕십리로)':'Seongsu-dong, Seoul (Wangsimni-ro)',
 '신사':'Sinsa-dong, Seoul','역삼':'Yeoksam-dong, Seoul','울산':'Ulsan','잠실·삼성·판교':'Jamsil, Samseong (Seoul) and Pangyo','정동':'Jeong-dong, Seoul','창성동':'Changseong-dong, Seoul',
 '청담':'Cheongdam-dong, Seoul','청담(선릉로)':'Cheongdam-dong, Seoul (Seolleung-ro)','한남':'Hannam-dong, Seoul','한남/이태원':'Hannam-dong / Itaewon, Seoul'}
CITY={'서울':'Seoul','뉴욕':'New York','울산':'Ulsan','광주':'Gwangju','두바이':'Dubai',
      '부산':'Busan','파리':'Paris','도쿄':'Tokyo','런던':'London','샌프란시스코':'San Francisco'}
CUISINE={'곰탕 다이닝 코스':'gomtang (beef bone soup) tasting course','곰탕·한식':'gomtang and Korean','디저트':'desserts','디저트 코스':'dessert tasting course','모던 한식':'modern Korean',
 '베이커리·샤퀴테리':'bakery and charcuterie','베트남':'Vietnamese','비건 파인다이닝':'vegan fine dining','스시 오마카세':'sushi omakase','와인 다이닝':'wine-focused dining',
 '와인 레스토랑':'wine restaurant','이탈리안':'Italian','이탈리안 생면 파스타':'Italian fresh pasta','이탈리안·한식':'Italian-Korean','캐주얼 중식':'casual Chinese',
 '컨템퍼러리':'contemporary','클래식 프렌치':'classic French','프렌치 코스':'French tasting course','플레이팅 디저트':'plated desserts','한식':'Korean','한식 디저트':'Korean desserts',
 '한식 반상':'Korean set meals (bansang)','한식 반찬·주점':'Korean banchan and drinks','한식 타파스 바':'Korean tapas bar','한식 파인다이닝':'Korean fine dining',
 '해산물·스파클링 바':'seafood and sparkling wine bar','현대 한식':'contemporary Korean','화덕·발효 우드파이어 한식':'wood-fired, fermentation-led Korean'}
ROLE={'총괄 셰프':'executive chef','책임주방장':'head chef','오너 셰프':'chef-owner','연수생':'trainee',
 '2019년 합류 (디저트)':'joined 2019 (desserts)','2022년 합류 셰프':'chef, joined 2022','수석 셰프':'chef de cuisine','수셰프':'sous chef','실습 (CIA 재학 중)':'stage (internship) while at the CIA',
 '창립 멤버':'founding member','출신 (10여 년 한식)':'about 10 years in Korean kitchens',
 '파티시에':'pastry chef','파티시에 (소나 경력도)':'pastry chef','헤드 셰프':'head chef','헤드 셰프 (10년+)':'head chef (10+ years)','헤드 셰프 (6년)':'head chef (6 years)','2020년 근무 (레귬·요드니에·렁클룸 경력도)':'worked in 2020; also Légume 2023, Jordnær 2022, L’Enclume 2024'}
def michelin(s):
    if not s or '미확인' in s and '스타' not in s: return None
    if '없음' in s: return 'Not in the current MICHELIN Guide Seoul (checked 2026-10-20)'
    import re
    m=re.match(r'(\d)스타( \+ 그린스타)?',s)
    if m: return f"{m[1]} MICHELIN Star{'s' if m[1]!='1' else ''}{' + Green Star' if m[2] else ''}"+(' (2026)' if '2026' in s else '')
    if '플레이트' in s or '선정' in s: return 'MICHELIN Guide selected'
    if '블루리본' in s: return 'Blue Ribbon Survey, 3 ribbons (2027)'
    return None
VSTAT={'confirmed':'Confirmed by the restaurant','contacted':'Asked the restaurant to confirm (awaiting reply)'}
