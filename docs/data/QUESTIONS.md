# 1. 질문 정의 / Questions the data must answer

> Phase 1 순서: **질문 → 온톨로지 → 수집·검증 → 상태 추적**. 이 문서가 맨 앞이에요. 아래 질문에 답할 수 없는 필드는 수집하지 않고, 답하는 데 필요한 필드는 빠짐없이 근거(source·위치·수집시각·효력시각·상태)와 함께 저장합니다.
> Order of work: question → ontology → collection/verification → state tracking. If a field answers no question below, we do not collect it.

**서비스 5종 (현재 사이트·README에서 읽은 표면; 준태님 확인 필요)** — the five service surfaces this foundation serves. They are inferred from the product as it exists today; if the intended five differ, only this table needs to change, the schema does not.

| # | Service | 한 줄 설명 |
|---|---|---|
| S1 | 계보도 Lineage map | 어느 주방 출신이 어느 식당을 열었나 |
| S2 | 셰프 경력·이동 Chef career & moves | 이 셰프는 지금 어디에 있고, 어디를 거쳤나 |
| S3 | 식당 상태 Restaurant status | 열었나·닫았나·옮겼나·이름이 바뀌었나 |
| S4 | 소식 피드·뉴스레터 What-changed feed / newsletter (`SHOW_NEWSLETTER=false`) | 새로 문 연 계보 식당, 셰프 이동 |
| S5 | 판정·랭킹 Verdicts & next-to-rise (`SHOW_VERDICTS=false`) | 저평가/과대평가, 다음에 뜰 집 — 입력 신뢰도에 민감 |

**공통 규칙 / Common rules**
- 모든 사실에는 *source · 근거 위치(locator/quote ≤200자) · collected_at(우리가 읽은 때) · effective_from(현실에서 효력이 시작되는 때) · status* 가 붙는다.
- status ∈ `verified` / `source_claim` / `ai_inference` / `unverified`. **공개 화면의 기본 하한은 `source_claim`**; `unverified`·`ai_inference`는 검토 큐와 "확인 중" 표시에만 쓴다.
- 질문마다 "as-of(기준일)"를 받는다. 기준일 없는 "현재"는 오늘 날짜의 as-of.
- 충돌은 보존한다(덮어쓰지 않음). 삭제는 없다.

## S1 계보도 Lineage map
| ID | 질문 | 필요한 사실 | 하한 | 근거 없을 때 |
|---|---|---|---|---|
| S1-1 | 주방 K 출신 셰프는 누구인가 (기준일 D) | employment(chef, branch=K, role_text) 와 그 claim | source_claim | 목록 제외, 검토 큐 |
| S1-2 | 그 셰프가 연 식당은 어디인가 | status_event(kind=open, by_chef) | source_claim | "오픈 주체 미확인"으로 표기 |
| S1-3 | 두 셰프가 같은 주방에 겹쳐 있었나 | employment 구간 교집합(`v_overlap`, 파생) | source_claim | 시작일 불명이면 겹침 주장 안 함 |
| S1-4 | A가 B에게 **사사**했다는 출처가 있나 | mentorship_assertion (출처가 명시한 경우만) | source_claim | 만들지 않음 — 동시 근무 ≠ 사제 |
| S1-5 | 이 계보 연결의 근거와 상태는? | claim + evidence 목록 | — | — |

## S2 셰프 경력·이동 Chef career & moves
| ID | 질문 | 필요한 사실 | 하한 | 비고 |
|---|---|---|---|---|
| S2-1 | 셰프 X는 기준일 D에 어디서 일하나 | employment(valid_from ≤ D < valid_to, open) − 닫힌 지점 | source_claim | `valid_to_kind='unknown'`은 현재로 표시하지 않음 |
| S2-2 | X의 경력 전체(순서·기간·직책) | employment 전부 + role_text/tenure_text 원문 | source_claim | 파싱 불가 기간은 원문 그대로 |
| S2-3 | X가 이동했나? 언제? 근거는? | move = 이전 employment 종료 + 새 employment(검증 대기) | 종료/신규 각각의 claim | 미검증 이동은 "이동 소문"으로만 노출, 기존 근무는 유지 |
| S2-4 | 동명이인 구분 | chef.disambiguator + alias | — | 확신 없으면 자동 병합 금지 → 검토 큐 |
| S2-5 | X가 요즘 확인된 시점은? | last_confirmed_at | — | 오래되면 stale 경고(180일) |

## S3 식당 상태 Restaurant status
| ID | 질문 | 필요한 사실 | 하한 |
|---|---|---|---|
| S3-1 | 식당(지점) B는 기준일 D에 영업 중인가 | status_event(open/pause/reopen/close/move) 시계열 | source_claim |
| S3-2 | 폐업·이전·휴업 날짜와 근거 | status_event.effective_date(+precision) + evidence | source_claim (verified 권장) |
| S3-3 | 이름이 바뀌었나 / 옛 이름 | alias(kind=former_name), status_event(rename) | source_claim |
| S3-4 | 같은 이름의 다른 지점·이전 구분 | branch (restaurant ≠ branch) | — |
| S3-5 | 연·월만 알려진 날짜 | precision 포함 저장; 경계에 걸리면 `uncertain` | — |

## S4 소식 피드 What-changed feed
| ID | 질문 | 필요한 사실 |
|---|---|---|
| S4-1 | 지난 N일간 *검증된* 변화는? | change_event + claim_status_history (status가 source_claim 이상으로 올라간 시각) |
| S4-2 | 새 오픈/폐업/이동 후보(미검증)는? | candidate_fact / review_queue — 검토자 전용, 공개 피드 아님 |
| S4-3 | 정정이 있었나 | correction_history |

## S5 판정·랭킹 Verdicts & next-to-rise
| ID | 질문 | 필요한 사실 | 규칙 |
|---|---|---|---|
| S5-1 | 입력 사실의 신뢰도 | 사용 claim의 status | `unverified`/`ai_inference` 입력은 점수에서 제외 또는 감점(현재 ×0.5 규칙을 상태 기반으로 대체) |
| S5-2 | 계보 점수 입력 | S1-1, S1-2, 최근성(오픈일) | 오픈일 precision이 year면 최근성 가중 완화 |
| S5-3 | 외부 지표(미쉐린·가격) | menu/price, 사람 입력(manual_only) | 자동 수집 금지 출처는 사람만 기록 |

## 범위 밖 (Phase 1) / Out of scope
프런트엔드 변경, 판정 공개, 뉴스레터 발송, 사이트 데이터 변경·배포. `data/seed.json`은 읽기만 하고 수정하지 않는다.
