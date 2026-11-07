# BUILD REVIEW — DeepSeek collection (web + Kitchen Genome Instagram)

- **Stage**: `build_review` (lane order `config/dev_lanes.json`: glm → deepseek → codex → swe2)
- **Scope**: full branch diff `origin/main` (`3554317`) → `feat/deepseek-collect-ig-daemon`
  (`bea455d` docs/lanes, `033420c` design review, `1679374` implementation; 37 files, +2813/−126).
- **Date**: 2026-11-05. **Verdict**: **mergeable after F1 is fixed or consciously accepted** — the
  safety architecture is sound and well tested; one high-severity functional defect (F1) makes the
  flagship web-extraction feature a practical no-op on real documents.
- **Method**: line review of the whole diff plus the pre-existing modules it touches
  (`kg_daemon/runner|claims|db`, `kg_collect/net|extract|gaps|scheduler`, `privacy_filter`,
  `crawler/instagram.py`), schema/CHECK-conformance checks against `schema/migrations/0001–0006`,
  secret scan of the diff, `python3 -m pytest tests/ -q` (225 passed, 1 skipped — optional
  Tesseract), `bash -n scripts/daily_pipeline.sh`. No live DeepSeek or Instagram calls, no
  deploy, no secret values read or printed, per the build constraints.

## Findings

Severity-ranked. Status: CONFIRMED = reproduced or proven from code; PLAUSIBLE = reasoned, not
reproducible offline. Line numbers refer to this branch.

### F1 — HIGH (CONFIRMED): web DeepSeek extraction skips essentially every real document

`daemon/kg_collect/deepseek.py:73-75` guards the model call with
`clean != text.strip() → return []`, intending "redaction changed the text, so offsets would
mislead". But `privacy_filter.sanitize_before_store` (`crawler/privacy_filter.py:101-103`)
*unconditionally* collapses internal whitespace (`re.sub(r'\s+', ' ', out).strip()`), even when
nothing was redacted. Verified offline:

```
'달빛정원의 운영에 대한\n중요한 소식입니다.  …'  →  clean != text.strip()  → skipped
'달빛정원  소식'                                 →  clean != text.strip()  → skipped
```

`doc.text` is raw fetched text (the rule extractor's span offsets index into it), so any document
containing one newline, tab, or double space is silently dropped before the model — i.e. nearly
all real pages, PDFs and OCR output. Consequences:

- Owner goal 1 "hard extractions" silently never runs in production while the cycle reports
  `ai=enabled`; there is no counter or log for the skip, so nobody will notice.
- The daemon-local path (`daemon/kg_daemon/deepseek.py:7-12`, title-only) is partially affected:
  titles are clean more often, but multi-space/newline titles are skipped the same way.
- Unit fixtures (`test_deepseek_collect.py:62` `text='달빛정원의 운영에 대한 중요한 소식입니다.'`)
  are single-spaced, which is why the suite is green while the feature is dead in production.

Fix direction (pick one, keep span semantics honest): send `clean` to the model *and* validate
quotes/offsets against `clean` (the IG collector already does exactly this for excerpts —
`crawler/instagram_kg.py:239-255`); or canonicalize `doc.text` (whitespace-collapse once) before
*both* rule and model extraction so stored spans and model offsets share one reference text. Add a
fixture with realistic whitespace plus a "model considered / skipped-by-filter" counter so the
dead path is observable.

### F2 — MEDIUM (CONFIRMED): candidate-URL day budget burned at enumeration time

`crawler/crawl.py:98,108` calls `Limits.admit(it['url'])` for every RSS item *before* the
`mentions()` filter, and `daemon/kg_collect/discover.py:332-335` admits every enqueued child URL.
Non-matching feed items and enqueued-but-never-fetched links permanently consume the 500/day
`web_daily_url` budget. A feed-heavy morning can exhaust the URL quota with few real fetches,
silently shrinking deterministic collection for both web entry points. Direction is conservative
(never over-collects) but the runbook's "500 distinct candidate URLs/day" does not mention that
discovery-time enumeration, not fetching, is what spends it. Consider admitting only URLs that
pass the relevance filter / are actually selected by the scheduler.

### F3 — MEDIUM (CONFIRMED, documented — confirm intended): robots fetches count against the 200/day dispatch cap

`daemon/kg_collect/net.py:148` — `limits.dispatch()` runs inside `_dispatch`, and `robots()`
(line 165) goes through `_dispatch`, so every robots.txt fetch consumes one of the 200 daily
dispatch slots and each previously-unseen host costs two dispatches (robots + page): at most
~100 new hosts/day, fewer when pagination applies. The runbook states "including robots and
pagination", so this matches the design; recorded because it materially caps throughput versus
the pre-branch crawler and an operator should confirm that is acceptable.

### F4 — SUPERSEDED: daily pipeline no longer merges or deploys

Superseded on 2026-11-07. The daily script again squash-merges and emits
`MERGED_NO_DEPLOY`, `NEEDS_DEPLOY`, or `DEPLOYED`. The note below describes the
PR-only revision that was withdrawn. AI output remains excluded from that publish path.

`scripts/daily_pipeline.sh:96-100` stops at `RESULT=PR_CREATED`; the removed code did
`gh pr merge --squash`, rebuilt main, and ran `./deploy.sh`. Site updates now need a human
squash-merge *and* a separately scheduled deploy that nothing currently performs; the RESULT
vocabulary changed (`DEPLOYED|MERGED_NO_DEPLOY|NEEDS_DEPLOY` → `PR_CREATED|NO_CHANGE`), so any
external consumer of those lines (cron wrappers, notifications such as BabyShower) must be
updated. Tests and both docs deliberately encode PR-only (`test_daily_pipeline_syntax_and_pr_only_boundary`),
and it fits the owner's "branch → PR → squash merge later" rule as applied to daily data; called
out because the order literally targeted the dev workflow, not the daily publish path.

### F5 — MEDIUM (CONFIRMED, documented): any transient transport error stops the IG account for the day

`crawler/instagram_kg.py:119-122` converts every non-`Skip` exception (DNS failure, TLS reset,
socket timeout — not just checkpoint/challenge/429) into `block()`: block flag +
`ig_account_state.stopped_day`, and even after a human removes the flag, same-day retry stays
blocked (`Guard.check`, line 84-86). Stricter than ordered, fail-closed, documented in the
runbook ("Unknown transport outcomes stop too"). Recorded so ops knows a single flaky network
moment costs the day's IG collection by design.

### F6 — LOW (CONFIRMED): 30-day purge can disable the IG model for a whole run

`crawler/instagram_kg.py:290` nulls `result_json` of done `ig-extract-1` rows older than 30 days;
`daemon/kg_ai/deepseek.py:62-65` then classifies a repeat of the same request key as
`previous_attempt_unknown_or_failed` and stops the client. An identical caption resurfacing after
>30 days silently disables AI for the rest of that run (keyword findings still collected).
Fail-closed; acceptable — noted for operator awareness.

### F7 — LOW (PLAUSIBLE): `max_tokens: 1024` invites truncation → run-long model stop

`daemon/kg_ai/deepseek.py:54` with up to 60 planner queries (3 × 20 gaps) or 8 findings
including ≤200-char quotes: a `finish_reason='length'` response becomes `invalid_task_json`
(lines 93-100), which stops the client for the run. Fail-closed and visible via `ai_reason`, but
a chatty planner loses all planning. Consider a higher limit for the plan task or a lower query
ceiling in the validator.

### F8 — LOW (PLAUSIBLE): verify `thinking: disabled` is accepted by the live endpoint once

`daemon/kg_ai/deepseek.py:54` sends the newer `thinking` parameter with `deepseek-chat`. If the
endpoint rejects it, every call returns 400 → `model_or_request_rejected` → AI silently disabled
(deterministic collection unaffected, observable as `ai=paused reason=model_or_request_rejected`).
No live call was made in this review per the constraints; an operator should confirm once during
first live enablement, when F1 is fixed.

### F9..F12 — NITS

- **F9** `daemon/kg_collect/deepseek.py:37`: recent queries are sent to the model under the key
  `failed_queries`, but the SQL includes successful queries too — mislabeled model input.
- **F10** (pre-existing on main, untouched by this branch, but newly load-bearing): running the
  full pytest suite from a checkout mutates tracked `crawler/vault.json` and
  `crawler/inbox_summary.json` (`tests/test_collector_v2.py:160` `sync_vault()` — observed live
  during this review; files restored afterwards). The new pipeline hard-fails on a dirty tracked
  tree (`scripts/daily_pipeline.sh:23`), so running tests inside the daily clone aborts it with
  `FATAL: tracked changes present`.
- **F11** `scripts/daily_pipeline.sh:90`: `git add -u` stages only already-tracked artifacts.
  Verified correct for today's writers (`crawler/apply.py`, `data/enrich_en.py`,
  `scripts/build_deploy.py` only modify tracked files; `crawler/out/**`, `dist/` are ignored),
  but any future script that adds a *new* file under those pathspecs will silently never ship.
- **F12** style: `from kg_ai import pending` re-imported per loop row (`daemon/kg_collect/resolve.py:68`),
  `sys.path` mutation and imports inside `Adapters.extract`, `from .limits import DailyLimit`
  inside the fetch loop (`daemon/kg_collect/cycle.py`). All functional; tidy on the F1 rework.

## Requirements conformance (owner order 2026-11-05)

| Requirement | Where | Verdict |
| --- | --- | --- |
| Web: fill AI stubs, gap-driven planning + hard extraction | `daemon/kg_collect/deepseek.py`, `daemon/kg_daemon/deepseek.py`, `stages.extraction` | ✅ built, ⚠️ dead in practice (F1) |
| `asserted_by=ai_inference` → review queue only, never auto-publish | `_cand` held→`needs_review`+`queue_review`; `entity_resolution` intercept; verification forces `ai_inference`; `state_history_update` holds; `kg_collect` apply/gaps/promote SQL excludes AI rows; `crawler/apply.py:36-38` gate; pipeline stages only the 4 tracked artifacts; inbox is the private DB | ✅ verified incl. forged-field test (`epistemic='fact'` still cannot apply) |
| IG: dedicated @kitchen.genome collector, not agamajung | `crawler/instagram_kg.py`; legacy path untouched; separate flag/session/account; separation tested | ✅ |
| IG caps: ≤25 handles/day, ≥8s, ≤6 captions, excerpt ≤40, ≤10 follows/day | `Guard` + `ig_handle_day`/`ig_follow_day` (durable, KST day), `DELAY/POSTS/FOLLOWS`; restart-survival tested; follows default-off and read backend cannot follow | ✅ |
| Stop on checkpoint/challenge/429; no proxy/account swap; findings pending-only | `Guard.dispatch` body-signal scan, `requests.Session.send` patch restricted to `instagram.com`, no proxies/redirects/retries; findings only in `review_queue` + private `crawler/out/ig_kg/` (outside the `ig-*.json` importer glob) | ✅ (plus F5 stricter stop) |
| Thin shared client under `daemon/`, OpenAI-compatible, default `deepseek-chat`, flash documented | `daemon/kg_ai/` (URL `https://api.deepseek.com/chat/completions` asserted in test); runbook §model | ✅ |
| Token metering into CostMeter/cost_cap | `Budget.reserve/settle` → `cost_ledger`, `daemon_run.cost_used_units`, KST-day allowance clamp 0–20; `CostMeter.charge` re-reads used; concurrency + lost-response tests | ✅ |
| Pipeline wiring with clear RESULT / IG_* lines; robots/403/429 stops, ≥5s, depth 2, ~500/200 caps | `scripts/daily_pipeline.sh` (`IG_OK|SKIPPED|BLOCKED|FAILED`, `COLLECT=`, `DAEMON=`, one `RESULT=`); `PoliteNet` min delay `max(5,…)`, `web_host_guard.paused` persisted, depth `min(2,cap)` + fetch-stage drop; `Limits(500,200)` shared via `KG_SERVICE_DB` + flock | ✅ (F2/F3 budget nuances) |
| Docs under docs/data/, unit tests with mocked DeepSeek HTTP, no live IG/DeepSeek in CI, no secrets in git | design + runbook; 4 new/extended test files, all transports/fixtures injected; `*.session`, `devpipe-runs/` ignored; diff scanned for secrets (none) | ✅ |
| Prefer parallel `crawler/instagram_kg.py` over rewriting agamajung path | agamajung collector and its flag untouched; pipeline no longer invokes it | ✅ |

Schema/migration 0006 reviewed: additive only; `ai_request.run_id` FK satisfied by insert order
(`daemon_run` row created before any reservation in all three callers); every emitted
`fact_type` (`menu`, `mentorship`, `employment`, …) and `asserted_by` value is inside the
existing CHECK constraints; `extraction` label CHECK matches the validator's label set exactly.

## Verification performed

- `python3 -m pytest tests/ -q` → **225 passed, 1 skipped** (`test_multipage_pdf…`, optional
  Tesseract) on this worktree.
- `bash -n scripts/daily_pipeline.sh` → clean; wiring tests exercise OK/BLOCKED/SKIPPED/FAILED IG
  paths, timeout `--mark-timeout` path, collect failure, build failure, PR-only boundary, and
  assert neither synthetic secret appears in any output.
- Offline reproduction of F1 (whitespace collapse vs skip-guard) shown above.
- Full-diff secret scan (`sk-`, `Bearer`, `password=`): none; only env-var *names* appear.
- Pre-existing test side effect (F10) observed, then `git checkout -- crawler/vault.json
  crawler/inbox_summary.json` restored the tree.

## Recommended next steps

1. Fix F1 (and add a realistic-whitespace fixture + skip counter), ideally folding in F9/F12.
2. Owner decision: accept F3 throughput cap and F4 PR-only daily flow (and schedule the missing
   human deploy step); confirm F5's stop-on-any-transport-error is desired.
3. On first live enablement: one manual operator run to confirm the API accepts the payload
   (F8), watching `ai=` / `ai_reason=` in the `COLLECT=` line.
4. Optional: F2 admission point, F6/F7 hardening, F10 test hygiene.

## Review-fix disposition (2026-11-05)

All findings were reworked in the same branch. `F4` is not a code defect: it is the owner's
explicit branch → PR → squash-merge-later decision, so it is documented rather than reverted.

| Finding | Status | Change |
|---|---|---|
| F1 | **Fixed** | `Adapters.extract` now runs `sanitize_before_store` first, then sends that exact sanitized text to the model and validates every quote, span, field name, and date against it. Added `ai_stats` (`considered / skipped_filter / skipped_no_targets / skipped_not_needed / called / failed / findings`) to the `COLLECT=` line. |
| F2 | **Fixed** | Candidate admission moved out of `discover.enqueue` / feed enumeration into actual dispatch (`PoliteNet.get` → `Limits.admit`). Unfetched candidates no longer spend the 500/day candidate budget. |
| F3 | **Fixed** | Robots fetches now use a separate durable counter on `web_daily_fetch.robots` (migration `0007_aux_robots_counter.sql`, `Limits.dispatch(robots=True)`), with their own 200/day ceiling. They no longer consume the 200/day content-dispatch budget. |
| F4 | **Superseded** | The 2026-11-05 PR-only decision is withdrawn. The daily script again squash-merges and uses `MERGED_NO_DEPLOY` / `NEEDS_DEPLOY` / `DEPLOYED`. AI and KG findings still cannot enter seed or the site. |
| F5 | **Fixed** | Added `TransientStop`: transport/network failures stop the run with `IG_FAILED` but leave `ig_account_state.stopped_day` and the block flag unset, so a retry is possible the same day. Checkpoint/challenge/429/session failures still write the flag and pin the day. |
| F6 | **Fixed** | Purge marks old `ai_request` rows `state='expired'`; the client raises `cached_result_purged` without setting `stopped`, and IG `model_call` no longer disables the client for an ordinary `DeepSeekError`. A purged cache entry no longer silences the model for the rest of the run. |
| F7 | **Fixed** | Per-task output limits: 2048 tokens by default, 4096 for gap planning and web extraction; the budget reservation bound uses the same value. Tests pin the payload. |
| F8 | **Fixed** | Removed the vendor-specific `thinking` parameter entirely, eliminating the 400-risk. No live call was made; first live enablement should still watch `ai=` / `ai_reason=`. |
| F9 | **Fixed** | `failed_queries` SQL now selects only rows that did not finish cleanly (`NOT (state='done' AND last_error IS NULL)`), so successful queries are not fed back as failures. |
| F10 | **Fixed** | `tests/test_collector_v2.py` patches `worker.VAULT_PATH`, `worker.SUMMARY_PATH`, and `worker.DB_PATH` into `tmp_path`; the full suite no longer mutates tracked `crawler/vault.json` / `crawler/inbox_summary.json`. |
| F11 | **Fixed** | Pipeline uses `git add -A -- <explicit pathspecs>`, so newly created artifacts under those paths are staged; the test asserts the flag. |
| F12 | **Fixed** | Hoisted `pending`, `DailyLimit`, and privacy-filter imports to module scope; removed loop/call-time `sys.path` mutation. |

Verification after the fix: `python3 -m pytest tests/ -q` → **231 passed, 1 skipped**; no live
Instagram or DeepSeek traffic, no secrets printed, and no tracked test fixtures modified.
