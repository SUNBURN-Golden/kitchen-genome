# DeepSeek efficiency BUILD REVIEW

Date: 2026-11-10. Decision: **CHANGES REQUIRED** before merge.

Reviewed `origin/main` (`613cccefcedd1e5b38fde50c388d23cd29583fa8`)
against implementation `d893263080a911c02a593521d8ec86e533634edc`
on `review/deepseek-efficiency-build-20261110`. This new branch starts at
`origin/main` and carries BUILD commit `4633096` by cherry-pick. Review scope is
the complete 36-file implementation diff and the design contract in
`docs/data/DEEPSEEK_IMPROVE_DESIGN.md`.

This commit records BUILD REVIEW only. The four findings below belong in the
next `review_fix` stage, followed by `re_review` and `audit` from
`config/dev_lanes.json`. No paid development lane, Cursor CloudAgent, live
DeepSeek request (including balance), Argon invocation, production pipeline,
production migration, network publication, merge, or deploy was executed.

## Findings

### R1 — P1: A recheck sublimit pause stops affordable fresh extraction

Location: `daemon/kg_collect/deepseek.py:354-365`, especially the unconditional
`self.client.stopped = 'cost_cap'` at line 365;
`daemon/kg_collect/cycle.py:351-365` processes fetches by age rather than workload.

The recheck path creates a review hold against the eight-unit sublimit before
fitting extraction. When the remaining recheck room cannot fit the extraction,
it sets the shared client's global stop reason to `cost_cap`. Every subsequent
fresh extraction also exits at that condition, even when the shared daily and
run budgets can afford it. An older recheck therefore starves later fresh
collection for the rest of the invocation, contrary to the required fairness.

Offline reproduction using the existing temporary `world` fixture:

1. Admit a due `r_dal` recheck and construct an `Adapters` with dummy credentials,
   a mocked healthy balance, mocked empty findings, and a fresh 20-unit Budget.
2. Extract `달빛정원 운영 소식입니다. ` followed by 180 repetitions of
   `소개 문장입니다. ` as `workload='recheck'`, passing the attempt ID.
3. The hold fits, but extraction does not. Observe `defer_extract=True`,
   `client.stopped='cost_cap'`, and **zero** completion calls/`ai_request` rows.
4. Set the budget workload to `fresh`: `room_tokens()` is still greater than
   16,000. Extract `달빛정원 운영 소식입니다.` as fresh work on the same adapter.
   Observe zero completion calls again and another deferral.

Required correction: distinguish workload exhaustion from a global cap stop;
continue affordable fresh work. Fit extraction and reserved review context
together, and release or resize unused holds when no paid extraction occurs.
Add a regression with an older deferred recheck followed by an eligible fresh
document; assert fresh extraction runs and total/recheck caps remain enforced.

### R2 — P1: Korean rule dates make valid recheck findings unreviewable

Location: the new rule-to-review routing in
`daemon/kg_collect/pipeline.py:60-64,84,91-96`, interacting with
`daemon/kg_ai/review.py:105-106`.

Rule extractions carry both a verbatim `date_text` and a normalized `date_iso`.
Persistence prefers `date_iso` in the extraction's `date_text` column. This was
usable for the ordinary rule apply path, but rechecks now route these rows into
the strict DeepSeek review precheck. That check requires the stored `date_text`
to occur literally in the article, so Korean-formatted dates cause automatic
rejection before any model decision. The current monthly test uses an ISO date
and does not cover the normal Korean source format.

Offline reproduction using the existing temporary `env` fixture:

1. Link fetch `f` to a due `r_dal` attempt and store this article as its source:
   `달빛정원은 2026년 10월 1일 문을 닫았다.`
2. Run `process_doc` with the Gazetteer and official tier. Its closure row has
   `workload='recheck'`, `pending_only=1`, and `date_text='2026-10-01'`.
3. Invoke the common review with a mocked healthy balance and an approving
   completion transport.
4. Observe **zero** completion requests, a deterministic rejection, and audit
   reason `date_not_verbatim: date not in source` despite the explicit date and
   supported closure. The same problem applies to normalized relative/English
   dates when the ISO string is absent from the source.

Required correction: preserve the verbatim date for review and retain its
normalized value separately for consumers that need ISO dates. Do not weaken
the model-extraction evidence checks. Test Korean date, relative date, ISO date,
approval/rejection, badge/log, rollback, and no core-field mutation through the
common review path.

### R3 — P2: A page about another restaurant advances the due restaurant

Location: `daemon/kg_collect/pipeline.py:99-100,253-256`;
`daemon/kg_collect/recheck.py:157-158`.

The inspection marker uses the general eligibility gate, which accepts any
active known chef or restaurant. It never checks that the inspected prose is
about the restaurant associated with this attempt. Search and discovery can
return unrelated registered restaurants; an empty rule extraction from such
a page is treated as a completed no-change inspection of the original target.

Offline reproduction using the temporary `world` fixture:

1. Admit a recheck specifically for `r_dal` (`달빛정원`) and attach fetch `f`.
2. Process only `모래시계 공식 소개 페이지입니다.`. This mentions a different
   registered restaurant and contains no mention of `달빛정원`.
3. Mark the job done and the fetch extracted, then call `recheck.reconcile`.
4. Observe `r_dal.last_checked_at` set, `last_outcome='reviewed_or_no_change'`,
   and its next check moved forward 30 days.

Required correction: bind completion evidence to the attempt's target, using
the existing Gazetteer aliases and any explicit, supported source association.
General AI eligibility alone is insufficient proof that this restaurant was
inspected. Test an unrelated registered restaurant and an unrelated registered
chef, plus a genuine target alias; only supported target inspection may advance
`last_checked_at`.

### R4 — P2: Coalesced jobs include historical fetches in the current attempt

Location: `daemon/kg_collect/recheck.py:148-156`, with job coalescing at
`daemon/kg_collect/recheck.py:59-64`.

Reconciliation joins every successful prose fetch ever recorded for a linked
job, without an attempt-time boundary or an explicit attempt/fetch constraint.
An ordinary official job reused for a monthly check commonly has older,
already-extracted fetches. Those fetches have no inspection marker for the new
attempt and are classified as incomplete inspection even after the current
fetch is fully inspected. If an old fetch has no `extracted_at`, it can instead
keep the attempt open indefinitely.

Offline reproduction using the temporary `world` fixture:

1. Queue an `official_chain` refresh job for `r_dal` and insert a successful,
   already-extracted prose fetch for it dated `2026-10-01`.
2. Admit the due recheck: it coalesces this job (`dedicated_root=0`). Link
   current fetch `f` to the same job.
3. Process `달빛정원 공식 소개 페이지입니다.`, mark the current fetch extracted
   and the job done, and reconcile.
4. Observe the attempt fail with `partial_inspection`, no `last_checked_at`,
   and failure backoff, solely because the old fetch has no new-attempt marker.

Required correction: explicitly associate the current attempt with the fetches
it dispatches/inspects, or enforce an appropriate attempt boundary while
preserving retries and shared-fetch provenance. Prior job history must neither
complete nor invalidate the new attempt. Add coalesced-job tests with both
extracted and unextracted historical fetches and a completely inspected current
fetch.

## Five-item implementation assessment

| Owner requirement | Reviewed implementation and result |
| --- | --- |
| Pre-filter | Shared active Gazetteer eligibility covers extraction/review; plan uses active registered gap metadata rather than page bodies. Unknown-only input is skipped; a known mention can admit mixed known/unknown text. Existing mocked tests pass. Monthly completion needs the stronger association in R3. |
| Article batching/cache | Stable sanitized source messages precede variable findings/corrections; equivalent URL/snapshots group across review fetches, extraction reuses validated article results, and strict review ID validation and budget splitting are tested. No additional blocking finding identified in these paths. |
| Monthly recheck | Additive durable schedule, daily admission, staggering, workload subcap, holds, provenance, and common review routing are present. R1–R4 prevent accepting this requirement as complete. |
| Weekly digest | Offline writer reconciles published career/employment approvals and rollbacks, preserves separate human/Argon copy, and writes under `marketing/drafts/`. Header/docs require explicit Gemini Argon review through `marketing/argon_review.sh`; daily generation does not invoke it. Mocked filesystem/no-network tests pass. |
| Balance warning | USD 0.50 / CNY 4.00 floors, unavailable/low-balance fail-closed handling, healthy TTL, pause latch, pre-reservation guard, and summary propagation are present. Mocked low-balance, unavailable, schema, bounds, replay and shell-result tests pass. |

## Verification

Existing regression suites: **352 passed, 1 skipped, plus 285 passed subtests**.
The skip is the existing synthetic acceptance case in the collector suite.
Four additional disposable confirmation probes, using existing temporary
fixtures and mocked balance/completion transports, reproduced R1–R4. Their
assertions checked the observed defective behavior; they are not acceptance
tests and were not committed as tests that would preserve the defects.

Commands (all with `KG_IG=0 PYTHONPATH=daemon:tests`):

```sh
pytest -q tests/test_deepseek_balance.py tests/test_deepseek_efficiency.py \
  tests/test_monthly_recheck.py tests/test_weekly_digest.py \
  tests/test_deepseek_collect.py tests/test_deepseek_client.py \
  tests/test_ai_review.py tests/test_daily_collect_wiring.py \
  tests/test_daemon.py tests/test_ontology_schema.py
# 184 passed

pytest -q tests/test_collect_acceptance.py tests/test_collect_bot_units.py \
  tests/test_collect_extraction.py tests/test_collect_foundation.py \
  tests/test_collect_official.py tests/test_collector_v2.py \
  tests/test_extractor_alerts.py tests/test_seed_migration.py \
  tests/test_source_registry.py tests/test_net_iri.py
# 91 passed, 1 skipped

pytest -q tests/test_extract_rules_13.py tests/test_extract_rules_14.py \
  tests/test_extract_rules_15.py tests/test_extract_rules_16.py \
  tests/test_extract_rules_17.py tests/test_ux_ads_labels.py
# 77 passed, 285 subtests passed

bash -n scripts/daily_pipeline.sh scripts/collect_daily.sh marketing/argon_review.sh
git diff --check origin/main
```

`tests/conftest.py` blocks unexpected live DeepSeek urllib requests; the
additional probes imported that guard as well. Shell integration tests use
stubbed commands in temporary directories. The real daily pipeline and Argon
script were inspected/syntax-checked only.

The added implementation diff has zero matches for the inspected credential
patterns (DeepSeek/GitHub tokens, private-key headers, and long literal provider
key assignments). No credential values were printed. `web/`, published seed/log,
and `config/dev_lanes.json` are unchanged against `origin/main`;
`SHOW_VERDICTS` and `SHOW_NEWSLETTER` remain false. Daily scripts default to
`KG_IG=0`; neither protected Instagram flag was accessed. Unrelated untracked
`.devpipe-font/`, `marketing/x_launch.md`, and `marketing/x_launch_argon.md` stay
outside the commit. No site output changed, so **no deploy is required**.

Open the branch as a draft PR with these findings visible. Keep merge with the
human/coordinator after REVIEW FIX, re-review, audit, and passing regression
tests; passing existing tests alone does not resolve these four findings.
