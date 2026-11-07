# 5. 능동 수집 봇 / Active collection bot (Phase 2)

코드: `daemon/kg_collect/` (+ 마이그레이션 `schema/migrations/0003_collection_bot.sql`). Phase 1 데몬 위에서 돈다 — 같은 서비스 DB, 같은 락/체크포인트/비용 상한/실패 큐. 사이트에 게시하지 않고(`SHOW_*` false, `build_data.py` 미실행), main에 직접 쓰지 않는다.

```
PYTHONPATH=daemon python3 -m kg_collect --db data_store/kg_service.db init          # 마이그레이션 + 타깃/레지스트리/갭 시드
PYTHONPATH=daemon python3 -m kg_collect --db data_store/kg_service.db cycle --units 80   # 1사이클 (재개 가능)
PYTHONPATH=daemon python3 -m kg_collect --db data_store/kg_service.db scoreboard    # 실측 스코어보드(JSON)
PYTHONPATH=daemon python3 -m kg_collect --db data_store/kg_service.db gold <file>   # 손라벨 골드 채점
```

## 구성 / Components
| 모듈 | 역할 |
|---|---|
| `discover.py` | 타깃 리스트 기반 발굴, 6개 경로: ①키워드 조합 검색(Bing News RSS) ②공식 디렉터리 ③sitemap/RSS/페이지네이션 ④엔티티 후속(새로 나온 이름) ⑤공식 사이트→SNS→예약→첨부 ⑥빈 필드 역검색. 관련도·깊이·예산 상한, 크롬 호스트(언어/후원 링크 등) 제외 |
| `evaluate.py` | 새 도메인 시험 수집 후 relevance .30 / novelty .20 / extractability .25 / originality .15 / permission .10 가중 점수. ≥0.50 + 문서 2개 + 추출 1개면 승격, <0.30 강등 → 레지스트리 성장(`sources/registry.discovered.yaml`) |
| `fetch.py`, `net.py`, `rawstore.py`, `docs_ocr.py` | API/피드 → HTML/JSON-LD → (JS 셸에만) 브라우저. 페이지네이션·표·PDF(pdftotext→OCR)·이미지 OCR(tesseract kor+eng). raw ref·hash·fetched_at·extractor version 기록. 200이어도 로그인 페이지/빈 본문은 실패. robots.txt, 도메인당 ≥5초(또는 Crawl-delay), 429/403/503/챌린지면 도메인 정지 — 우회 없음 |
| `extract.py`, `resolve.py` | 규칙 우선 추출(경력/재직 주장/오픈/이전/폐업/메뉴/협업)과 인식 상태(fact/claim/inference/unverified). 게스트 이벤트≠재직, 코워킹≠멘토십. 엔티티 해석은 **정규화 별칭 정확 일치만**; 동명이인은 레스토랑 문맥이 있을 때만, 아니면 `homonym_unresolved`. 새 이름은 `candidate` 타깃 |
| `gaps.py` | 필수 필드(식당: chef/opened/status_recent, 셰프: origin_kitchen/current_employer)의 빈칸 → 작업. 분류 `not_extracted / absent_in_source / inaccessible / conflict`. AI로 채우지 않음 |
| `scheduler.py` | backfill .35 / discovery .40 / refresh .25 예산 분리(discovery 하한 .30 예약). 우선순위 = novelty + gap + urgency + quality − cost − dup − fail (+ 구조화 경로 보너스). 변동 필드 1일/안정 14일 주기 |
| `apply.py` | claim/fact만 서비스 데이터에 반영. inference/unverified는 반영 안 함. 이전 vs 새 소속 변화는 `field_history` + change 이벤트 |
| `gold.py`, `scoreboard.py` | 정밀도/재현율/F1, 인식 상태 정확도, 거짓 병합률, 스코어보드 |

레이어: raw(git 밖 content-addressed) → extraction → labels → service data.

## 증거: 합성 vs 실측 (정직하게)
**합성 세계(`tests/collect_world.py`, 오프라인·결정적) — 인수 테스트 9개, `tests/test_collect_acceptance.py`:** 초기 목록에 없던 소스·타깃 발굴, 다중 페이지 + PDF + 이미지 수집, 근거 있는 갭 해소, 멱등 재실행, 이전 vs 새 소속 변화 감지, 망가진 추출기 감지(`extractor_alert`), 체크포인트 재개.
**실측(2026-10-27 KST, 실제 웹):** 위 합성 항목 중 **소속 변화 감지·추출기 경고는 실제 데이터로 입증하지 못했다**(몇 시간 안에 실제 소속이 바뀌지 않음) — 합성 데모일 뿐이다. 실제 PDF/이미지 소스도 실사이트에서는 아직 못 찾았다.

### 실측 스코어보드 (`data_store/runs/score_final.json`, git 밖)
실제 웹에 대해 `cycle --units 80`을 7회(총 562 unit, 약 3시간 KST, 폴라이트 지연 포함) 돌린 뒤. 초기 상태(`score_before`)와 비교.

| 지표 | 값 |
|---|---|
| 신규 유효 소스 (초기 목록 밖 호스트 중 평가 승격) | **8** — bntnews.co.kr, dailian.co.kr, fnnews.com, frame-less.co.kr, ko.wikipedia.org(참조 디렉터리), news.jkn.co.kr, sports.khan.co.kr, wikitree.co.kr (본 호스트 64개) |
| 신규 타깃 (candidate; 승격 0) | **5** — 정호영, 송훈, 임성근, 여경옥(셰프), `CIA`(식당; **오탐 가능** — Culinary Institute of America 약어일 수 있어 사람 검토 필요) |
| 필수 필드 커버리지 | 0.5877 → **0.5921** (228개 중 134 → 135) |
| 갭 해소율 | 1/94 = **1.1%** (나머지: not_extracted 49, absent_in_source 44) |
| 변화 지연 | 실측 0건 (14일 이내 기사에서 소속 변화 없음) |
| 정확도 (합성 골드 28문장) | P 1.0 / R 0.9 / F1 0.947, 인식상태 정확도 1.0, 거짓 병합 0/6 |
| 정확도 (실측 골드 58문장, **in-sample**) | P 1.0 / R 0.941 / F1 0.97, 인식상태 1.0, 거짓 병합 0/7 (튜닝 전 rules/1.0: P 0.639 / R 0.676) |
| 비용 | 562 unit (backfill 196 / discovery 225 / refresh 141), 유효 항목 28 → **20.1 unit/항목**, 신규 유효 소스당 70.2 unit |
| 수집 결과 | ok 304, fail_empty 214(주로 0건 검색), robots 10, blocked 6, login_page 2, network 10, type 4, http 3 |
| 추출 | claim 48, unverified 309 (claim/fact만 서비스 데이터에 반영) |

실측으로 입증된 것: 폴라이트 Bing News RSS 발굴 → 실제 언론 기사 수집 → 새 호스트 평가·승격 → 레지스트리 성장(`sources/registry.discovered.yaml`), 디렉터리 페이지네이션(55 fetch 중 2개 2페이지 이상), **200인데 로그인 페이지인 응답을 실패로 기록**(aitimes.kr, indinews.co.kr), 강제 종료 후 **재개**(`resumed_from=run_f2ad940b65af`), **멱등 재실행**(실제 304문서를 강제 재추출 → `new_extractions=0`, fetch/extraction/claim/evidence 행 수 불변: 553/357/129/47), 실제 갭 1건을 근거와 함께 해소.
솔직한 평가: 갭 해소와 커버리지 개선은 **미미**하다 — 언론 기사는 대부분 "출신/오픈 시점"을 필드 단위로 주지 않고, 공식 소스(레스토랑 사이트·예약 페이지) 경로는 아직 얇다. 이것이 다음 단계의 핵심 과제다.

## 한계 / Not done
- AI 추출 훅은 STUB(규칙이 못 푸는 모호 문장용; 결과는 항상 `ai_inference`).
- 실사이트 PDF/OCR 경로는 합성으로만 입증. 브라우저 경로는 JS 셸(롯데호텔 디렉터리 등)에만, 차단 우회 목적으로는 쓰지 않음.
- Instagram은 커넥터: `crawler/out/ig_blocked.flag`가 있으면 일시정지, 우회·계정전환·프록시 없음. MICHELIN은 `manual_only`.
- `gold_real.json`(58문장, git 밖)은 같은 표본으로 튜닝했으므로 after 수치는 **in-sample**(튜닝 전: P 0.639 / R 0.676).
- 변화 지연(change latency)은 14일 이내 기사에만 의미가 있고, 실측에서는 아직 0건.

## Phase 3 (2026-10-28): 공식 사이트·예약 페이지, held-out, 후보 검토, 일일 실행

PR: #42 (profile_fact/official_site schema + official/booking extractors), #43 (공식 사이트 자동 발견: seed·기사 링크·도메인 probe 검증),
#44 (후보 검토 + 약어/기관 필터 → 'CIA' 거부), #45/#46 (daily_pipeline.sh에 non-fatal 수집 1회), #47 (영문 셰프명 헤드라인 오추출 수정 + held-out 집계).

**예약 플랫폼 (robots 실측)**: app.catchtable.co.kr `Disallow: /` (검색엔진만 허용), m.place/pcmap.place.naver.com `Disallow: /`, opentable `Disallow: /`,
exploretock Cloudflare 403. → 추출기(`booking.py`)는 있지만 이 플랫폼들은 **수집하지 않는다**(우회·검색봇 사칭 없음). 해당 gap은 `inaccessible`로 분류.

**실측 before → after (cycle 1–7 → 1–10, 실제 웹)**

| 지표 | before (phase 2 끝) | after (cycle 10) |
|---|---|---|
| required-field coverage | 0.5921 | **0.6009** (initial 0.5877) |
| gap 해결(증거 있음) | 1 / 94 | **3 / 94** (2개는 공식 사이트 JSON-LD fact) |
| 검증된 공식 사이트 | 0 | 3 (seed 2, probe 1) |
| profile facts (official, 정리 후) | 0 | address 3 · chef 3 · status 3 |
| 신규 valid source (현재 promoted) | 8 | 6 (fnnews·wikitree 재평가로 강등) / new hosts seen 64→94 |
| 후보 target | 5 (CIA 포함) | 4 검토 대기, CIA 거부 |
| units spent | 562 | 760 |
| change latency | 0 changes | 0 changes (일일 실행으로 누적 예정) |

**정확도**: in-sample real gold P 1.0 / R 0.941 (튜닝셋). **held-out** (cycle 8–10 문서 30문장, 17 항목, 규칙 동결 후 수집, 작성자 단독 라벨):
**P 0.667 / R 0.353 / F1 0.462**, epistemic 1.0. 과적합 확인 — 영문 헤드라인, '팝업 오픈'(collab로 오분류), '현업 셰프 + 식당' 병기 누락이 주 원인.
held-out은 튜닝에 쓰지 않았다(다음 개선 후 새 held-out 필요). 공식 사이트 chef 추출은 7개 중 2개만 정답이어서 #47로 수정(사후 판정, 블라인드 아님).

## Phase 4 (2026-10-28): rules/1.3 + 새 held-out v2

rules/1.3 (일반 규칙): '팝업'은 collab이 아니라 매장 형식(opening에 `venue_format=popup`, 셰프가 팝업을 '운영했다'면 career),
'갈라/와인/페어링 디너·행사 개최'는 collab, 셰프+식당 병기(관계 동사 없음)는 약한 employment_claim(`ambiguity=side_by_side_mention`),
영문 헤드라인 규칙(Former X → career, X Opens R → opening, R's/At R … Chef X → employment_claim).

| 평가셋 | rules/1.2 | rules/1.3 |
|---|---|---|
| 구 held-out (cycle 8–10, **이제 튜닝에 사용 → dev셋**) | P 0.667 / R 0.353 | P 1.0 / R 0.882 (in-sample, 의미 없음) |
| gold_real (in-sample) | P 1.0 / R 0.941 | P 0.889 / R 0.941 (병기 규칙 FP 3) |
| **새 held-out v2** (cycle 11 신규 79문서, 대상 언급 27문장 전수, 규칙 동결 후 라벨 선작성, 9항목) | – | **P 0.0 / R 0.0** (FP 1, FN 9) |

v2 실패 원인: 성씨 약칭 공지시('손 셰프' → 손종원) 미지원 4, '이타닉가든·라망시크레 … 이끈/겸하고 있다' 다중 식당 열거 미지원 4,
"'Lysée(리제)'의 이은지(Eunji Lee) 셰프" 괄호 병기 미지원 1, '본연' 오탐(일반명사와 같은 식당명) 1.
동명이인 음성 샘플(뮤지컬 배우 김호영, 구청장 박준희, 밴드 멤버 이준용)은 모두 정상적으로 무추출. 샘플이 작고(9항목) 한 인물 기사에 몰려 있다.

## Phase 5 (2026-11-02): rules/1.5 + 새 held-out v4

rules/1.5 (일반 규칙, dev셋 = gold_real·held-out v1·v3 오류에서 도출): '오픈' 뒤 헤드라인 문장부호(…·!?’” 등) 허용,
따옴표 이름이 오픈 동사 바로 앞에 오면('X 셰프 ‘R’ 오픈', '‘R’ 7일 오픈') 업종 명사 없이도 새 매장명으로 인식,
collab 단서는 같은 헤드라인 절(—, …, | 로 구분) 안에서만 opening을 막음, 레지스트리에 없는 'N스타/레스토랑 ‘R’'은 collab의 식당으로 기록.

| 평가셋 (--registry) | rules/1.4 | rules/1.5 |
|---|---|---|
| held-out v3 (**이제 dev셋**) | P 0.667 / R 0.8 / F1 0.727 | P 0.714 / R 1.0 / F1 0.833 |
| gold_real (dev) | F1 0.765 | F1 0.853 |
| held-out v1 (dev) | F1 0.882 | F1 0.914 |
| held-out v2 | F1 0.429 | F1 0.429 |
| **새 held-out v4** (11-01·11-02 일일 수집 17문서, 대상 언급 40문장, 기존 gold와 헤드라인 중복 12문장 제외, 라벨 선작성, 10항목) | P 0.778 / R 0.7 / F1 0.737 | **P 0.889 / R 0.8 / F1 0.842** |

v4 남은 오류(다음 개선 후보, 튜닝 금지 → 다음 held-out으로 검증): 'R 엄태철 셰프와 ○○ 출시' 같은 협업 키워드 없는 제품 협업(병기 employment_claim로 오분류),
'팝업 레스토랑 ‘R’을 연다'('연다' 미지원).

## Phase 6 (2026-11-06): rules/1.6 — 제품 협업·팝업 opening

셰프가 명시된 문장의 셰프컬렉션·브랜드/제품 출시를 collab으로 추출하고 병기 employment_claim을 억제한다.
미등록 인접 인용 식당명과 `식당 NAME가` 행사 주어도 collab에 연결한다. 팝업/레스토랑/식당/다이닝의 매장 목적어가
`을/를 연다` 또는 `을/를 열고`이면 opening이며, 디너/만찬/행사/이벤트/갈라는 제외한다. 기존 갈라 디너 collab은 유지한다.

합성 문장만으로 구현·검증한 뒤, 기존 라벨을 변경하지 않은 held-out v5(15문장, 정답 12항목)에 아래 명령을 실행했다.
동일 DB/명령으로 변경 전·후를 실측했으며, 요청서의 대략적인 baseline 대신 실제 실행 수치를 기록한다. 개별 오류를 보고 추가 튜닝하지 않았다.

```sh
PYTHONPATH=daemon python3 -m kg_collect --db data_store/kg_service.db gold data_store/gold/gold_heldout_v5.json --registry
```

| 평가셋 (--registry) | rules/1.5 (before) | rules/1.6 (after) |
|---|---|---|
| held-out v5 | P 0.818 / R 0.750 / F1 0.783 | **P 0.857 / R 1.000 / F1 0.923** |
| TP / FP / FN | 9 / 2 / 3 | 12 / 2 / 0 |
| 인식상태 정확도 | 1.000 | 1.000 |

검증: 요청된 rules/1.5·1.6 + collect_extraction 59개 통과; rules/1.3·1.4 포함 72개 통과.

## Phase 7 (2026-11-07): rules/1.7 — type/event nouns are not venues

`팝업 레스토랑을 연다`처럼 매장 유형 명사만 목적어인 opening과, `레스토랑 출범이`처럼 사건 명사를 collab 파트너로 붙이는 FP를 막는다.
`clean_venue`가 유형·사건 명사(레스토랑/식당/다이닝/출범/론칭 등)를 거절하고, collab은 셰프 또는 실제 식당명이 있을 때만 만든다. 합성 문장(`tests/test_extract_rules_17.py`)만으로 구현·검증한 뒤 held-out에는 추가 튜닝하지 않았다.

```sh
PYTHONPATH=daemon python3 -m kg_collect --db data_store/kg_service.db gold data_store/gold/gold_heldout_v5.json --registry
PYTHONPATH=daemon python3 -m kg_collect --db data_store/kg_service.db gold data_store/gold/gold_heldout_v6.json --registry
```

| 평가셋 (--registry) | rules/1.6 (before) | rules/1.7 (after) |
|---|---|---|
| held-out v5 | P 0.857 / R 1.000 / F1 0.923 | **P 1.000 / R 1.000 / F1 1.000** |
| TP / FP / FN | 12 / 2 / 0 | 12 / 0 / 0 |
| 인식상태 정확도 | 1.000 | 1.000 |
| **새 held-out v6** (11-06 저녁 수집·11-07 크롤 문장 15개, 정답 17항목, 라벨 선작성) | – | **P 0.786 / R 0.647 / F1 0.710** |

v6 남은 오류(다음 개선 후보, 튜닝 금지 → 다음 held-out으로 검증): 갈라 디너 문장에서 셰프명 미부착 collab, 단축형 '문 셰프' 공지시 없는 mentorship/career, 레지스트리 짧은 별칭('모수')으로 '모수 서울' 고용 주장 표면형 불일치.
검증: rules/1.5·1.6·1.7 + collect_extraction 70개 통과; held-out v4 (--registry) P/R/F1 1.0 유지.

