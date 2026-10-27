# 3. 출처·수집·검증 / Sources, collection, verification

기계 판독본: [`sources/registry.yaml`](../../sources/registry.yaml) (이 문서의 쌍둥이; `daemon/kg_daemon/registry.py`가 로드 시 검증, `tests/test_source_registry.py`가 불변식을 테스트). 서비스 DB의 `source`·`source_policy`로 동기화된다.
각 출처는 *무엇을 모으는지 · 허용 여부 · 주기 · 보존 · **지원하지 않는 범위***를 명시한다.

## 3.1 출처 표 / Registry (version 2026-10-27.1)
| source_id | tier | 허용 | 역할 | 주기 | 어댑터(Phase 1 상태) | 지원하지 않음(요약) |
|---|---|---|---|---|---|---|
| `seed_legacy` | human | allowed | 마이그레이션 전용 | 1회 | seed_migration (**구현**) | 사실별 locator/quote 없음 → 재검증 전까지 source_claim/unverified |
| `official_sites` | official | limited | 1차 검증 | 상태 일 1회 | http_fetch (**STUB**; 로컬 crawler 산출물은 수집) | 로그인 벽, 허용 목록 밖 JS 렌더, 예약 플랫폼, 개인 연락처 |
| `press_rss` | press | limited | 발견 | 매일 | http_fetch (STUB; crawler/crawl.py 존재) | 기사 전문(≤200자 인용만), 유료 기사 |
| `press_articles` | press | limited | 사실 출처 | 링크 추적 depth≤2 | crawler_vault (**구현**: vault.json 읽기) | 전문 저장, 유료 |
| `specialist_blogs` | specialist_blog | limited | 발견 | — | crawler_vault (**구현**) | 단독으로 verified 불가 |
| `search_leads` | search_lead | limited | 발견 | 매일 | http_fetch (STUB) | 단독으로 source_claim 이상 불가 |
| **`instagram_public`** | social_lead | limited | **후보 발견(필수 출처)** | 일 1회 06:45–08:00 KST, ≤25 핸들 | instagram_py (**읽기 전용**: crawler/out/ig-*.json + 정지 플래그) | 아래 §3.2 |
| `michelin_manual` | official | manual_only | 사람 검증 | 사람 | manual_findings | 자동 수집 전부(robots 법적 고지) |
| `manual_clip` | human | allowed | 사람 보충 | 수시 | crawler_inbox (**구현**) | — |
| `outreach_replies` | official | allowed | 식당 회신(1차) | 수시 | manual_findings | — |
| `community_threads` | search_lead | manual_only | 발견 | 사람 | none | 자동 수집 |
| `prohibited_hosts` | search_lead | **prohibited** | 제외 | — | none | 수집 불가 목록(어댑터·필드 없음 — 레지스트리 검증이 강제) |

공통 기본값: 도메인당 최소 5초 간격(robots Crawl-delay가 더 길면 그쪽), 도메인당 동시 1, quote ≤200자, **차단 신호(429/403/503/챌린지) → 해당 출처 정지, 신원·프록시·계정을 바꿔 우회하지 않음**.

## 3.2 인스타그램: 필수 후보 발견 출처 — 그리고 한계 / Instagram: required candidate source, with its limit
- 역할: 셰프 이동·오픈·폐업 **후보**를 가장 먼저 포착하는 단서. 공개 게시물의 40자 이내 발췌 + 게시물 URL/날짜만. 신뢰 상한 **`unverified`** — 인스타 단독으로는 `source_claim`도 `verified`도 못 된다. 공식/언론 근거가 붙어야 승격.
- **정지 규칙(pause-on-checkpoint)**: 체크포인트·챌린지·2FA·이메일 코드·의심 로그인·로그인 실패·HTTP 429·login_required 중 하나라도 → 즉시 중단, 정지 플래그 기록, 재시도·같은 날 재로그인 없음. **사람이 플래그를 지워야 재개.**
- **구현하지 않고 계획도 없는 것 / Not implemented, not planned**: 사람 위장(UA·행동 흉내), 계정 교체/순환, 프록시·IP 우회, 캡차 풀이, 정지 중 접근. 이는 레지스트리 검증 코드가 강제한다(`block_evasion: forbidden` 필수, 우회 기법을 쓰는 항목은 "not implemented" 명기 없이 통과 불가). *(그룹 채팅에서 "명시적 금지가 없으면 우회 허용"이라는 취지의 지시가 있었으나, 이 작업 범위에서는 우회를 구현·계획하지 않기로 했고, 변경하려면 준태님의 별도 명시 결정이 필요합니다.)*
- **현재 상태(2026-10-27)**: `crawler/out/ig_blocked.flag` 존재 — 2026-10-24 07:12 KST `LoginException: checkpoint`. **정지 중, 커버리지 0.** 플래그는 그대로 둔다. 데몬은 이 출처를 `paused`로 보고하고 기존 산출물도 읽지 않는다.
- 정지 해제 후에도 IG에서 나온 후보는 `candidate_fact`(unverified)로만 들어가 검토 큐를 거치며, 공개 상태를 바꾸지 못한다(`tests/test_daemon.py`).

## 3.3 수집 → 검증 흐름 / Collection → verification
`fetch` → `document`(content hash, raw는 git 밖 raw store, 보존기간 후 purge) → `extraction`(후보만: `candidate_fact`, quote ≤200) → `entity_resolution`(정확 일치+동명이인 범위; 불확실하면 `needs_review`, **신규 셰프 자동 생성 안 함**) → `evidence_verification`(§2.3 규칙) → `state_history_update`(claim 생성, 충돌 기록) → 서비스 출력 재계산. 자세한 건 [DAEMON.md](DAEMON.md).

## 3.4 개인정보·보존
이메일·전화·집주소는 기존 `crawler/privacy_filter`가 저장 전에 제거. raw 보존기간은 출처별(`retention_days`), 이후 raw만 purge하고 행·quote는 남긴다. 서비스 DB·raw store는 git에 올리지 않는다(`.gitignore`: `data_store/`, `raw_store/`, `*.sqlite*`).
