# 2. 온톨로지 / Ontology

SQL: [`schema/migrations/0001_core_ontology.sql`](../../schema/migrations/0001_core_ontology.sql), [`0002_daemon_runtime.sql`](../../schema/migrations/0002_daemon_runtime.sql). Code: `daemon/kg_daemon/{claims,ontology}.py`. 질문: [QUESTIONS.md](QUESTIONS.md).

## 2.1 원칙 / Principles
1. **사실마다 근거** — 모든 시간 한정 사실(Employment, StatusEvent, Mentorship, Alias…)은 `claim`을 가리키고, claim은 `evidence`(source URL · locator · quote ≤200자 · `collected_at` · `effective_from`)에 연결된다.
2. **상태 4종** — `verified` / `source_claim` / `ai_inference` / `unverified`.
3. **삭제 없음, 덮어쓰기 없음** — 모든 사실 테이블에 DELETE 차단 트리거. 종료·정정은 새 상태/새 행 + 이력(`claim_status_history`, `correction_history`). `evidence`는 UPDATE도 차단(불변).
4. **충돌 보존** — 서로 다른 주장은 둘 다 남기고 `conflict` 행으로 묶는다. 자동 해소 없음.
5. **동시 근무 ≠ 사제** — `mentorship_assertion`은 출처가 명시한 경우(예: '사사')에만 존재. 겹침은 파생 뷰 `v_overlap`일 뿐이고 둘을 잇는 테이블은 없다. AI 추론으로는 멘토십을 만들 수 없다(트리거).
6. **추측 금지** — 파싱 불가한 날짜·기간은 날짜로 바꾸지 않고 원문(`role_text`, `tenure_text`, `detail`)에 둔다.

## 2.2 엔티티 / Entities
| 개념 | 테이블 | 메모 |
|---|---|---|
| Chef | `chef` | 불투명 안정 id. 동명이인은 `disambiguator`(예: 출신 주방)로 구분. 레거시 `name@origin` 규칙과 호환 |
| Restaurant | `restaurant` | 브랜드/이름 단위 |
| Branch/Location | `branch` | **열고·옮기고·닫는 단위**. 이전(move)은 branch 간 이벤트 |
| Role | `role` (8개 시드) + `employment.role_text`(원문) | 매핑은 명백한 표현만 |
| Employment | `employment` | Chef–Branch(또는 미해결 `employer_label`) × 기간 × 직책. 아래 §2.4 |
| RestaurantStatusEvent | `restaurant_status_event` | open / move / pause / close / reopen / rename, `effective_date`+precision(없으면 NULL로 기록) |
| MenuEvent | `menu_event` | 메뉴·행사·콜라보(스키마만; 수집은 Phase 2) |
| MentorshipAssertion | `mentorship_assertion` | 출처 명시 시에만 |
| Source / SourcePolicy / Document / Evidence | `source`, `source_policy`, `document`, `evidence` | [SOURCES.md](SOURCES.md) |
| Claim | `claim`, `claim_evidence`, `claim_status_history` | 상태 계층 |
| Alias | `alias` | 옛 이름·로마자·핸들·**legacy_id**(옛 seed id → 새 id) |
| Conflict / Correction / Review | `conflict`, `correction_history`, `review_queue` | |

ID: `chf_…`, `rst_…`, `brn_…`, `clm_…`, `evd_…` — 입력으로부터 결정적(sha 기반)이라 마이그레이션이 멱등이고, 재사용되지 않는다. 옛 seed id는 `alias(kind='legacy_id')`로 보존.

## 2.3 Claim 상태 / Status rules
| 상태 | 의미 | 올라가는 조건 |
|---|---|---|
| `unverified` | 단서(소셜, 검색, 출처 없음) | — |
| `ai_inference` | 모델 추정. **절대 사실로 적용하지 않음** (`state_history_update`가 검토 큐로만 보냄) | `human:` 검토자만 `verified`로 |
| `source_claim` | 출처가 그렇게 말함 (공식/언론/사람 입력 1곳) | 아래 `verified` 조건 |
| `verified` | 독립된 신뢰 호스트(공식·언론·사람 tier) **2곳 이상**, 또는 공식/사람 출처 1곳 | — |

- `social_lead`·`search_lead`·`specialist_blog`는 `verified` 계산에 **들어가지 않는다**. 인스타그램은 `trust_ceiling: unverified`.
- 순위: verified(3) > source_claim(2) > unverified(1) > ai_inference(0). 공개 질의의 기본 하한은 `source_claim`.
- 모든 상태 변화는 `claim_status_history`에 트리거로 기록(누가·언제·왜).
- 품질 검사(`quality_check`)가 불변식을 어기면 실행 실패: 근거 없는 verified/source_claim, AI 추론이 사실 테이블에 들어감, 단서만으로 verified, 출처 없는 멘토십.

## 2.4 시간 모델 / Time model
- 날짜는 ISO 문자열 `YYYY` | `YYYY-MM` | `YYYY-MM-DD` + precision(`year|month|day`). 구간은 **반개구간 [valid_from, valid_to)**.
- `valid_to_kind`:
  - `open` — 출처상 현재 진행 중(마지막 확인 `last_confirmed_at`까지). 현재로 취급.
  - `known` — `valid_to` 있음.
  - `unknown` — 끝났는지 기록이 없는지 모름. **절대 "현재"로 표시하지 않음** (출신 주방이 대표적).
- 날짜가 연/월 정밀도라 기준일과 경계에 걸리면 `uncertain`/`end_ambiguous`로 표시하고 단정하지 않는다.
- `effective_from`(현실에서 효력 시작) ≠ `collected_at`(우리가 읽은 때) — 둘 다 evidence에 저장. "과거 시점의 상태"는 effective 기준, "그때 우리가 뭘 알았나"는 `claim_status_history`/`created_at` 기준.

### 기준일 질의 / As-of queries (`ontology.py`)
`current_employment(chef, day, min_status)`, `employments_asof`, `branch_state_asof(branch, day, min_status)`, `pending_moves`, `alumni_of`.
- claim이 `min_status` 미만이면 제외, **미검증 종료는 `end_pending`** — 기존 근무를 유지하고 새 근무는 "이동 후보"로만 노출.
- 폐업/이전된 지점의 근무는 현재가 아님(`branch_closed`).

## 2.5 이동 의미론 / Chef move
`ontology.move_chef(chef, effective_date, evidence, status, to_branch|to_employer_label)`
1. 열린 employment가 정확히 1개면 → 그 행을 이동 시점으로 **닫는다**(`valid_to_kind='known'`, `state='closed'`, `end_claim_id`=이동 claim). 삭제 없음, 감사 로그 자동 기록.
2. 새 employment를 **검증 대기**(claim=이동 근거의 status, 기본 `unverified`)로 추가하고 `pending_verification` 검토 항목을 큐에 넣는다.
3. 열린 employment가 0개면 새 행만 추가. **2개 이상이면 추측하지 않고** `AmbiguousMove` + `ambiguous_move` 검토 항목.
4. 이동 근거가 `source_claim` 미만이면 공개 질의는 계속 *이전* 근무를 현재로 보여준다(`end_pending=true`).

## 2.6 겹침(동시 근무) / Overlap
`v_overlap` — 같은 지점에서 **알려진** 구간이 교집합인 두 셰프. 시작일 모르는 행은 제외(단정 불가). 사제 관계 아님.

## 2.7 충돌·정정 / Conflicts & corrections
- 같은 대상·필드에 서로 다른 값의 claim 두 개 → `conflict(open)` (예: 같은 폐업이 날짜만 다르게 보도). 둘 다 질의 가능. 해소는 사람이 `state`/`resolution_note`로.
- 사실 행의 필드 수정은 `correction_history`(옛값·새값·사유·근거·누가) — 트리거가 employment에 자동 적용.
- 원문이 바뀌면(content hash 변경) 이미 적용된 사실이 있는 경우 `source_modified` 검토 항목. 옛 claim은 그대로.

## 2.8 마이그레이션 / `data/seed.json` → ontology (`seed_migration.py`)
`python3 -m kg_daemon --db data_store/kg_service.db seed-migrate [--dry-run]` (PYTHONPATH=daemon). 읽기 전용, 멱등.
| seed | → |
|---|---|
| `kitchens[]` | restaurant + branch (같은 이름의 established 식당과는 **이름 일치로 병합** + `entity_merge_by_name` 검토 항목) |
| alumni `restaurants[]` | restaurant+branch, chef(`name@origin`), **출신 주방 Employment**(기간 불명, `valid_to_kind=unknown`, role_text 원문) + **본인 식당 Employment**(`open`, 폐업 시 `known`) |
| `opened` | 파싱되면 `open` StatusEvent(precision), 안 되면 날짜 없는 이벤트(원문 `detail`) — 날짜를 만들지 않음 |
| `closed_at/closed_src` | `close` StatusEvent |
| `prev_names` | alias(former_name) |
| role 텍스트의 '사사' | MentorshipAssertion (**텍스트가 주장할 때만**) |
| `chef_career[]` | Employment 정제(기존 own 행을 갱신, 중복 생성 안 함), 해소 불가 고용주는 `employer_label` + 검토 |
| 이름 없는 셰프 / `+` 메모 / `lineage_status=conflict` | Employment 만들지 않음 → `review_queue` |
| `lineage_status=verified` | claim `source_claim`으로 **강등** (seed에는 locator/quote가 없어 새 `verified` 기준 미충족 → 재검증 단계에서 승격). 트레이드오프로 문서화 |
실제 seed 결과(2026-10-27 기준): 식당 54, 셰프 47, 근무 88, 상태 이벤트 36, 멘토십 2, alias 114, 검토 17.
