# 3. 출처·수집·검증 / Sources, collection, verification

기계 판독본: [`sources/registry.yaml`](../../sources/registry.yaml) (이 문서의 쌍둥이; `daemon/kg_daemon/registry.py`가 로드 시 검증, `tests/test_source_registry.py`가 불변식을 테스트). 서비스 DB의 `source`·`source_policy`로 동기화된다.
각 출처는 *무엇을 모으는지 · 허용 여부 · 주기 · 보존 · **지원하지 않는 범위***를 명시한다.

## 3.1 출처 표 / Registry (version 2026-11-20.1)
| source_id | tier | 허용 | 역할 | 주기 | 어댑터(Phase 1 상태) | 지원하지 않음(요약) |
|---|---|---|---|---|---|---|
| `seed_legacy` | human | allowed | 마이그레이션 전용 | 1회 | seed_migration (**구현**) | 사실별 locator/quote 없음 → 재검증 전까지 source_claim/unverified |
| `official_sites` | official | limited | 1차 검증 | 상태 일 1회 | http_fetch (**STUB**; 로컬 crawler 산출물은 수집) | 로그인 벽, 브라우저가 필요한 JS 셸, 예약 플랫폼, 개인 연락처 |
| `press_rss` | press | limited | 발견 | 매일 | http_fetch (STUB; crawler/crawl.py 존재) | 기사 전문(≤200자 인용만), 유료 기사 |
| `press_articles` | press | limited | 사실 출처 | 링크 추적 depth≤2 | crawler_vault (**구현**: vault.json 읽기) | 전문 저장, 유료 |
| `specialist_blogs` | specialist_blog | limited | 발견 | — | crawler_vault (**구현**) | 단독으로 verified 불가 |
| `search_leads` | search_lead | limited | 발견 | 매일 | http_fetch (STUB) | 단독으로 source_claim 이상 불가 |
| `instagram_public` | social_lead | prohibited | 기존 산출물 보관 | — | none | 자동 접근 |
| `instagram_kitchen_genome` | social_lead | limited | 비공개 후보 발견 | 매일 | instagram_kg_review_only | 아래 §3.2 |
| `michelin_manual` | official | allowed | 선정 목록 참조 | 선정 페이지 읽기, 사람 확인 | manual_findings | 인증이 필요한 페이지와 접근 차단 |
| `manual_clip` | human | allowed | 사람 보충 | 수시 | crawler_inbox (**구현**) | — |
| `outreach_replies` | official | allowed | 식당 회신(1차) | 수시 | manual_findings | — |
| `community_threads` | search_lead | manual_only | 발견 | 사람 | none | 자동 수집 |
| `prohibited_hosts` | search_lead | **prohibited** | 제외 | — | none | 수집 불가 목록(어댑터·필드 없음 — 레지스트리 검증이 강제) |

국내 언론사에는 정직한 Kitchen Genome UA로 robots.txt를 조회하고 요청 간 최소 5초 또는 더 긴 Crawl-delay를 적용한다.
국내 언론사는 레지스트리 목록, 한국 언론 피드 및 `.kr` 언론 호스트 분류로 정한다. 다른 호스트에는 고정 간격과 robots 조회가 없다.
모든 호스트의 요청은 동시에 하나씩 처리한다. 401·403·429, 재시도 시각을 명시한 서버 차단, 로그인 벽과 캡차가 감지되면
해당 실행에서 호스트를 정지하고 기록한다. 리디렉션은 목적지마다 같은 규칙을 적용한다.
일반 웹은 zecbot 터널을 우선 사용하며 터널 장애가 확인되면 수집 서버 IP로 연결한다.
블루리본은 알려진 403 접근 거부로 제외한다. 일반 기사 인용 한도는 200자다.

## 3.2 인스타그램 / Instagram

- `@kitchen.genome`의 저장된 세션으로 시드에 등록된 공개 식당·브랜드 프로필과 최근 30일 게시물을 읽는다. 핸들별 최근 게시물을 기록해 다음 실행에서는 새 캡션만 검사한다. 다른 계정, 비밀번호 로그인, 팔로우, 프록시 전환은 지원 범위 밖이다.
- 체크포인트·챌린지·2FA·429·접근 거부 시 즉시 중단하고 `crawler/out/ig_blocked.flag`를 최초 1회 생성한다. 이 파일이나 기존 `ig_kg_blocked.flag`가 있으면 요청을 보내지 않는다. 기존 플래그는 그대로 보존한다. 세션이 없으면 건너뛰고, 타임아웃과 일시적 전송 오류는 마지막 체크포인트를 보존한 채 종료한다.
- 40자 이내 발췌와 게시물 URL·날짜는 비공개 검토 큐에 보관한다. 신뢰 상한은 `unverified`이며 공개 상태에 반영하려면 별도의 출처 확인이 필요하다.

## 3.3 수집 → 검증 흐름 / Collection → verification
`fetch` → `document`(content hash, raw는 git 밖 raw store, 보존기간 후 purge) → `extraction`(후보만: `candidate_fact`, quote ≤200) → `entity_resolution`(정확 일치+동명이인 범위; 불확실하면 `needs_review`, **신규 셰프 자동 생성 안 함**) → `evidence_verification`(§2.3 규칙) → `state_history_update`(claim 생성, 충돌 기록) → 서비스 출력 재계산. 자세한 건 [DAEMON.md](DAEMON.md).

## 3.4 개인정보·보존
이메일·전화·집주소는 기존 `crawler/privacy_filter`가 저장 전에 제거. raw 보존기간은 출처별(`retention_days`), 이후 raw만 purge하고 행·quote는 남긴다. 서비스 DB·raw store는 git에 올리지 않는다(`.gitignore`: `data_store/`, `raw_store/`, `*.sqlite*`).
