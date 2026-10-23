# Kitchen Genome 수집·검증 파이프라인 v2 설계 및 감리 문서

## 1. 설계 목표
- **원칙:** 넓게 발견(후보 수집)하고, 근거 수준을 나눠 저장하며, 공개할 때 엄격하게 거른다. 공식 자료가 아니라는 이유로 발견 단계에서 버리지 않되, 발견(`candidate`)과 사실 확인(`source_checked` / `verified`)을 엄격히 분리한다.
- **수집과 게시의 분리:** 자동 수집·정리는 상시 돌아가지만 사이트 공개(`data/seed.json` 반영)는 자동 직결하지 않고 `ready_to_publish`(공개 대기) 단계에서 검수 후 반영한다.

## 2. 3대 수집 경로 및 역할 분리
1. **자동 수집 (`auto`):** 언론사 RSS, 공개 API, 사이트맵, 목록 및 본문 내 관련 링크(최대 깊이 `max_link_depth = 2`)에서 신규 후보 URL을 발견한다. `robots.txt`를 준수하며 달력·검색 조합·무한 페이지네이션 URL은 차단한다.
2. **브라우저 수집 (`browser`):** 자바스크립트 렌더링이 필요한 공개 페이지는 승인된 도메인(`allowed_browser_hosts`)에 한해 필요한 본문 영역만 읽는다.
3. **수동 보충 (`manual_clip`):** Chrome 확장프로그램(`extension/`)에서 사용자가 보고 있는 페이지의 ‘이 자료 담기’를 누르면 Kitchen Genome 전용 수집함(`crawler/inbox.json`)으로 전달된다.
- **작업 분리:** 브라우저·확장프로그램은 페이지의 필요한 영역만 읽는 도구로 한정하고, 대기열(큐) 관리·개인정보 재검증·중복 묶음·근거 수준 분류는 로컬 작업기(`crawler/worker.py`)가 전담한다.

## 3. 저장 전 개인정보(PII) 및 프롬프트 인젝션 필터 (`crawler/privacy_filter.py`)
- 브라우저 추출 시 `<article>`, `<main>` 등 필요한 영역만 추출하고, 로컬 저장 및 AI 전달 전에 `sanitize_before_store()`를 강제 적용한다.
- **제외 대상:**
  - 개인 전화번호, 개인 이메일, 주거 주소, 주민등록번호
  - 아동 개인정보, 개인 건강·금융정보(계좌·카드번호 등)
  - 로그인 쿠키·세션 토큰·비밀번호·입력창(`input`) 값
  - DM·비공개 메일·회원 전용 대화·개인 프로필의 불필요한 사적 정보
- **보존 대상:** 공식 소개·인터뷰·보도에 나타난 셰프 이름, 소속 주방, 재직 기간, 직책(역할), 개업·이전·폐업 연월 등 공개 직업 정보(출처 URL 및 짧은 근거 인용 포함).
- **프롬프트 인젝션 방어:** 본문 내 `이 명령을 실행하라`, `ignore previous instructions`, 시스템 지시 사칭 문구는 명령으로 해석하지 않고 데이터 수집 단계에서 무력화·표시(`prompt_injection_detected`)한다.

## 4. Kitchen Genome 전용 추출 스키마 및 동명이인 분리 (`crawler/extractor.py`)
- **추출 필드:** `chef`(셰프), `restaurant`(식당), `origin_kitchen`(원류 주방), `tenure_period`(재직 기간), `role`(역할), `independence`(독립/개업), `relocation`(이전), `closure`(폐업), `price`(가격 단서).
- **근거 연결:** 각 관계마다 `evidence_url`, `evidence_quote`(200자 이내 짧은 발췌), `evidence_locator`(문단·문자 오프셋), `source_tier`(`official` | `press` | `specialist_blog` | `event_notice` | `search_lead`)를 연결한다.
- **동명이인 분리:** `chef_entity_id = "{chef_name}::{origin_kitchen_or_restaurant}"` 키와 활동 연도·주방 교차 검증을 통해 동명이인을 분리하고, 불일치 시 `homonym_review_required` 플래그를 부여한다.
- **출처 충돌 보존:** 서로 다른 출처가 직책·재직 여부·폐업 여부를 다르게 기술할 경우 어느 한쪽을 임의 삭제하지 않고 두 근거를 모두 보존하며 `conflict: true`로 표시한다.

## 5. 저장 구조 및 다중 신호 중복 묶음 (`crawler/vault.py`)
- **레코드 필수 메타데이터:** `url`, `title`, `source_host`, `published_at`, `collected_at`, `target_id`, `extracted_facts`, `evidence_locator`, `dedup_cluster_id`, `verification_status`.
- **경량 저장:** 전문·사진 무단 복제를 금지하고 구조화된 사실 필드와 200자 이내의 짧은 근거만 기본 저장한다(전문 보관이 허용된 `fulltext_allowed_hosts`만 예외).
- **다중 신호 중복 판정 (`compute_dedup_cluster`):**
  1. 정규화 URL 일치(트래킹 파라미터·모바일 서브도메인 제거)
  2. 동일 기사 재전송(포털·신디케이션 기사의 정규화 제목 + 핵심 셰프·식당 시그니처 일치)
  3. 한·영 번역본 및 동일 보도자료(`target_id + event_kind + year + chef_entity_id` 지문 일치)
  - 동일 클러스터의 부차 문서는 `dedup_excluded` 상태로 분류해 수집량 부풀리기를 방지한다.

## 6. 초기 안전 설정값 및 감속·재개 정책 (`crawler/config.json`, `crawler/worker.py`)
- 브랜드당 일일 신규 후보 URL 발견 상한: `500`개 (`daily_candidate_url_limit`)
- 브랜드당 일일 새 본문 수집 상한: `200`개 (`daily_content_fetch_limit`)
- 동일 도메인 동시 요청: `1`개 (`per_domain_concurrency`)
- 요청 간격: 기본 최소 `5.0`초 (`min_delay_sec`), 대상 사이트 `robots.txt`의 `Crawl-delay`가 더 길면 그 값을 우선 적용
- 링크 추적 깊이: 최대 `2`단계 (`max_link_depth`)
- 달력·검색 조합·끝없는 페이지네이션 URL 수집 제외 (`trapped_url_patterns`)
- 가격·휴업·이전·상호변경 등 변동성 높은 항목은 신규 탐색보다 재확인(`recheck_priority`) 우선 배정
- `429`, `403`, `503` 등 서버 제한·오류 응답 시 즉시 감속 및 해당 도메인 수집 중단(`backoff_and_halt`). 차단 회피용 계정·프록시·접속 신원 전환 기능은 일절 포함하지 않음.

## 7. Chrome 확장프로그램 권한 및 상태 집계 (`extension/`, `crawler/status_report.py`)
- 수동 담기 기능은 `activeTab`, `scripting`, `storage` 최소 권한으로 동작하며, 승인된 사이트의 추가 수집이 필요할 때만 `optional_host_permissions`로 요청한다.
- MV3 서비스 워커 종료에 대비해 큐와 진행 오프셋을 `chrome.storage.local` 및 로컬 `crawler/worker_state.json`에 체크포인트로 저장하고 재개한다.
- **완료 기준 5단계 상태 구분:**
  1. `candidate` (후보 발견)
  2. `source_checked` (원문 확인 완료·단일/보조 근거)
  3. `dedup_excluded` (중복 묶음 제외)
  4. `verified` (1차 원문 또는 복수 신뢰 출처 검증 완료)
  5. `ready_to_publish` (공개 대기 — 사이트 자동 게시 미연결)
