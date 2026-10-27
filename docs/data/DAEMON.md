# 4. 수집·검증·상태추적 데몬 / Daemon design (Phase 1 skeleton)

코드: `daemon/kg_daemon/` — `runner.py`(실행 골격), `stages.py`(9단계), `cli.py`, `db.py`, `claims.py`, `ontology.py`, `registry.py`, `seed_migration.py`. 기존 `crawler/` 스택(vault.json, instagram 산출물, privacy_filter, canonicalize_url)을 **재사용**하고 대체하지 않는다. 일일 파이프라인(`scripts/daily_pipeline.sh`)과 `data/seed.json` 흐름은 건드리지 않는다 — 데몬은 별도 서비스 DB에만 쓴다.

```
PYTHONPATH=daemon python3 -m kg_daemon --db data_store/kg_service.db migrate
PYTHONPATH=daemon python3 -m kg_daemon --db data_store/kg_service.db seed-migrate      # seed.json 읽기 전용
PYTHONPATH=daemon python3 -m kg_daemon --db data_store/kg_service.db run [--cost-cap N] [--no-resume]
PYTHONPATH=daemon python3 -m kg_daemon --db data_store/kg_service.db status | requeue <failure_id>
pip install -r daemon/requirements.txt   # pyyaml
```
(`__main__`이 `kg_daemon.cli`를 호출. 종료코드: succeeded/locked/paused=0, failed=1.)

## 4.1 단계 / Stages (모두 체크포인트 + 멱등)
| # | stage | 하는 일 | Phase 1 상태 |
|---|---|---|---|
| 1 | `source_discovery` | 레지스트리 → active / paused(플래그) / skipped(manual_only·prohibited). 전부 정지면 run이 `paused_source` | 구현. 주기 스케줄러는 STUB(활성=전부 due) |
| 2 | `incremental_fetch` | 로컬 crawler 산출물(`vault.json`, `out/ig-*.json`)을 `document`로 수집, content hash로 중복 제거, raw는 git 밖 raw store | 구현(로컬). **네트워크 fetcher(http_fetch)는 STUB** — 주입 지점·비용 계량·정지 규약만 있고, 없으면 `stubbed_adapters`로 보고 |
| 3 | `change_detection` | (source, canonical_url#target&kind) 기준 new / modified. 적용된 사실이 있던 문서가 바뀌면 `source_modified` 검토 | 구현 |
| 4 | `extraction` | 규칙 추출기 → `candidate_fact`(후보만). IG는 키워드 단서만 | 구현(규칙). **AI 추출기는 STUB 훅**(`env['ai_extractor']`, 결과는 항상 `asserted_by='ai_inference'`) |
| 5 | `entity_resolution` | legacy_id→branch, 이름/alias→chef(동명이인 범위). 모호하면 `needs_review`(homonym / unknown_chef / unknown_target …) | 구현. 퍼지 매칭은 STUB |
| 6 | `evidence_verification` | 후보에 상태 부여: AI→`ai_inference`, 소셜/검색/블로그→`unverified`, 공식·사람 또는 독립 신뢰 호스트 ≥2→`verified`, 그 외 `source_claim` | 구현 |
| 7 | `state_history_update` | 후보 → claim + evidence + 사실(폐업/오픈/이전/이름변경 이벤트, 근무, 이동, 출처가 주장한 멘토십). **`ai_inference`는 적용 안 함(검토 큐)**. 날짜 불일치는 `conflict`. 같은 사실이 또 오면 evidence만 추가 | 구현 |
| 8 | `service_output_recompute` | `current_employment`, `branch_status`를 기준일로 `service_output`에 upsert(`min_status=source_claim`) | 구현 |
| 9 | `quality_check` | 불변식 위반 → **재시도 없이 실패**(NonRetryable). 경고(오래된 열린 근무, 폐업 지점의 열린 근무, 열린 충돌) → 검토 큐 | 구현 |

## 4.2 실행 보증 / Guarantees
- **실행 잠금**: `run_lock`(DB 임대, TTL 1800s). 이미 잡혀 있으면 `locked`로 즉시 종료(단일 실행). TTL 만료된 잠금은 인수. 종료 시 해제.
- **체크포인트**: 단계마다 `stage_checkpoint(status, attempts, cursor, output, error)`. 단계 출력은 다음 단계의 입력(`ctx.prev`).
- **멱등**: 결정적 id, `INSERT OR IGNORE`, `candidate_fact.idempotency_key`(문서 해시+종류+payload), seed_ref 유니크 인덱스. 재실행해도 행이 늘지 않는다(테스트).
- **재시도(상한)**: 단계 실패 시 `max_attempts=3`, 지수 백오프(2s·2ⁿ, 상한 30s). 시도 중 부분 쓰기는 롤백. NonRetryable은 즉시 실패.
- **실패 큐**: 상한 초과 → `failure_queue(stage, error, attempts)` + run `failed`(`failed_stage`). 사람이 `requeue`로 표시.
- **비용 상한**: `CostMeter`가 `http_request` / `llm_tokens_k` 단위로 계량해 `cost_ledger`에 기록. 상한(`cost_cap_units`, 기본 200) 초과 직전에 **중단 → run `paused_cost_cap`**(실패 아님, 큐 적재 없음). 상한을 올려 다시 실행하면 이어서 진행.
- **실패 단계부터 재개**: 다음 `run`은 직전 failed/paused/중단된 run을 `resumed_from`으로 잇고, 이미 `done`인 단계는 출력만 복사해 **다시 실행하지 않는다**(테스트로 검증).
- **정지 규약**: 출처가 429/403/503/챌린지/체크포인트 신호를 내면 그 출처만 정지(플래그/`paused`), 신원·프록시·계정 교체 없음. 레지스트리 검증이 `block_evasion: forbidden`을 강제.

## 4.3 Phase 1에서 **실제로 구현된 것 / 스텁**
구현: 스키마 2개, 레지스트리 검증·동기화, seed→온톨로지 마이그레이션, claim 승격 규칙, 이동·기준일 의미론, 데몬 9단계의 로컬 데이터 경로, 잠금·재시도·실패 큐·비용 상한·재개, 품질 불변식, 테스트.
스텁(인터페이스·보고만): ① http fetcher(공식 사이트·RSS·검색) ② AI 추출기 ③ 퍼지 엔티티 매칭 ④ 주기(cadence) 스케줄러 ⑤ raw purge 작업(`retention_expires_at`만 기록) ⑥ `daily_pipeline.sh` 연동 ⑦ IG는 기존 `crawler/instagram.py` 산출물만 읽음(**데몬이 IG에 로그인·접속하지 않음**) ⑧ D1 배포(로컬 SQLite만).

## 4.4 다음 단계 / Next phases (제안)
1. **P2 수집**: crawler/crawl.py를 `http_fetch` 어댑터로 감싸 비용 계량·정지 규약 연결; 공식 사이트 상태 페이지 우선. 추출 규칙에 `chef_move`/`opening` 텍스트 패턴 추가.
2. **P3 검증**: 검토 큐 UI 없이 CLI로 `human:` 승격/기각, 레거시 `source_claim` → 근거 locator 채워 `verified` 승격 배치, 아웃리치 회신을 `official` 근거로 연결.
3. **P4 서비스 출력**: `service_output` → 읽기 API(Workers/D1) + S1–S5 질의 구현; 점수의 신뢰도 입력을 claim 상태 기반으로 교체(공개는 준태님 승인 후).
4. **P5 운영**: 일일 파이프라인에서 crawl → daemon run → 요약 보고, 알림(failure_queue, 열린 conflict, IG 정지).
