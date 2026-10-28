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
