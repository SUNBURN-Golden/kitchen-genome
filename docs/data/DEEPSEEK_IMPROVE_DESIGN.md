# DeepSeek collector efficiency improvements

Status: DESIGN only. Owner order: 2026-11-10. No implementation, migration,
runtime execution, paid call, or deployment is part of this stage.

## Scope and invariants

Implement the five improvements below through the development stages in
`config/dev_lanes.json`: design → design_review → build → build_review →
review_fix → re_review → audit. These development stages are distinct from the
collector's runtime stages, `plan/fetch/extract/evaluate/gaps/apply/quality`.
This document is the handoff to design_review; it does not run other lanes.
Cursor CloudAgent is forbidden. With the current $0 balance and no-spend order,
do not invoke DeepSeek-backed development lanes or live provider smoke tests.

The 2026-11-09 review order and 2026-11-10 constraints take precedence over older
collection documents describing pending-only human review or larger allowances:

- All AI findings and monthly recheck candidates require the existing DeepSeek
  review decision before publication. Approval adds `ai_reviewed`, the `AI 검토`
  badge, source, and `data/ai_review_log.jsonl`; rejection closes with a reason.
  Preserve `scripts/ai_review_rollback.py` compatibility. Core seed fields are
  not silently overwritten by an approved annotation.
- All model work shares `KG_SERVICE_DB`, `ai_request`, `Budget`, and the KST-day
  `KG_AI_DAILY_COST_CAP`, clamped to at most 20 internal units. Clamp the daemon
  run cap to at most 20 too; never increase a cap to make a request fit.
  Internal token units are not dollars or a currency conversion.
- `KG_IG=0` throughout this work and its verification. Do not invoke Instagram
  or touch `crawler/out/ig_blocked.flag` or `ig_kg_blocked.flag`.
- Keep `SHOW_VERDICTS=false`, `SHOW_NEWSLETTER=false`, and existing indexing.
  No credentials, provider response bodies, private runtime DBs, or raw source
  files enter git or logs. Never print secrets, including in exceptions.
- Feature branch from `origin/main` → PR → passing checks → human/coordinator
  squash merge. Never push to main or merge this change automatically. Deploy
  only if site/web output changes; a design-only PR requires no deploy.

## Existing implementation and gaps

`daemon/kg_collect/targets.py` already supplies a seed/discovered-target
Gazetteer with aliases. `Adapters.extract` sanitizes text, tests mentions,
checks rule ambiguity/open gaps, fits text to the budget, and returns up to
eight findings per document. However, context target IDs can currently admit
a document without a textual match. `Adapters.plan` sends named gap metadata,
not page bodies. The local-document daemon reuses the extraction adapter.

`daemon/kg_ai/review.py` prechecks stored evidence and batches at most eight
findings by `fetch_id`, splitting when a reservation cannot fit. It repeats
occurrence excerpts per finding; equivalent articles from different fetches
are not grouped together. Its current instruction rejects contradictions with
the seed without distinguishing a genuine later change from a false claim.

`kg_ai.deepseek.Client` owns injectable completion HTTP, validated result
replay, JSON checks, and bounded retries. `Budget` reserves request bytes plus
framing and output against both run and shared-day limits, settles actual
usage, and retains uncertain reservations. Preserve those guarantees.

The collector already has a refresh pool, status queries, and periodic job
requeueing, but no durable monthly schedule per published restaurant.
`daily_pipeline.sh` reviews backlog, crawls/collects, bridges the daemon,
reviews again, applies, builds, and handles publication. It currently discards
daemon JSON and omits AI reasons from the final RESULT. Its automated crawl-PR
merge behavior is separate from the human merge required for this feature PR;
do not execute that script against this development checkout.

## 1. One cheap eligibility gate

Extend `targets.py` with a reusable eligibility result: eligible target IDs,
matched spans, and a fixed reason code. Reuse Gazetteer normalization and
aliases, not a second chef-name list. Synchronize published seed names, previous
restaurant names, and chef aliases into that registry without promoting targets
from model output. Ambiguous aliases may admit a page but never resolve an
identity without the existing exact-match checks.

Eligibility requires a chef or restaurant mention in the actual sanitized text
being submitted, mapped to an active seed or registered active discovered
target. A region/cuisine, trusted source host, candidate-only target, query
name, or caller-supplied context target ID alone is insufficient. Existing
Gazetteer users may still need candidate targets, so implement this narrower
policy in the eligibility helper rather than changing all matching behavior.

Apply it before reservation/completion in extract, local-document extraction,
and review. Review first performs its existing quote/name/date checks, then
uses the same gate on the sanitized article context. A missing source or no
eligible target closes an existing review deterministically with the reason,
records it in the log, and closes linked candidates; it never approves one.
Count `considered`, `eligible`, `skipped_no_targets`, and `skipped_filter` per
stage, retaining existing extraction counters for compatibility. Count documents
once, with separate finding counts for review, so batching does not inflate skips.

Planning currently has no article body: require every gap's target ID to exist
and be an eligible chef/restaurant; do not search empty text and disable all
planning. If a future plan includes source text, apply the article gate before
sending it. Count invalid/unknown planning targets separately. Recheck eligibility
after budget-driven context shortening; do not accidentally remove the sole
known mention and still send the request.

Unknown-name policy:

| Source content | Decision |
| --- | --- |
| Known restaurant plus a new chef, or known chef plus a new restaurant | Eligible; unknown names may become evidence-backed findings, never automatic registry promotion |
| Only new/unknown names, including a nominal recheck URL with no known mention | Skip AI with `skipped_no_targets`; deterministic discovery can retain leads |
| A previously unknown name independently registered as an active target | Eligible on a later pass after rebuilding the Gazetteer |

No automatic model-only discovery exception is introduced. This deliberately
trades some recall for predictable cost; it must be documented in the runbook.

## 2. Article batching and stable prompt prefixes

Define an article key from the existing canonical URL plus the digest of the
sanitized source snapshot and its normalization version. Different snapshots
at the same URL remain separate; URL canonicalization alone must never mix
conflicting revisions. Preserve each finding's extraction/fetch IDs and exact
source offsets for audit and rollback.

Introduce a shared article prompt builder in `kg_ai` with this message order:

1. Fixed system instruction identifying source content as untrusted data.
2. Stable source message: canonical URL, publication metadata, and one sanitized
   article body/context, serialized deterministically.
3. Task-specific instruction and sorted candidate/target IDs, site records,
   questions, or retry correction. Variable batch data follows the article.

Current sorted JSON can put `findings` before source content. Explicit messages
avoid that layout. Keep retry corrections out of the fixed prefix. Ensure
request keys and source hashes cover all messages, model, task/prompt/schema
versions, selected context, and candidate data; increment affected versions.
Do not invalidate unrelated completed requests or uncertain reservations.

DeepSeek documents prefix-based, best-effort caching and cache-hit/miss usage
fields. Stable prefixes improve the opportunity for reuse; a hit is not
guaranteed, and batching alone does not prove one. Preserve hit metrics and add
miss metrics where useful; do not discount reservations based on expected hits.
See [DeepSeek context caching](https://api-docs.deepseek.com/guides/kv_cache/).

Extraction remains one multi-finding request per article, not one per chef,
target, or ambiguous rule candidate. Deduplicate equivalent fetched snapshots
before requesting extraction, retaining provenance links. Gather all applicable
targets/ambiguities before dispatch; keep the eight-finding schema limit. For
long sources use deterministic bounded windows with explicit original offsets,
deduplicate overlapping findings, and never silently treat a truncated window
as coverage of the entire page. Defer additional windows when room is exhausted.

Review groups by article key instead of only `fetch_id`, uses a shared sanitized
context containing the eligible mentions and required quote occurrences, and
keeps at most eight findings per request. Raw-text fallback must be sanitized
before it enters a model prompt; a quote lost to sanitization is not eligible
for publication. Keep shared context stable across splits where it fits; if a
smaller context is required, create an explicit new context digest and recheck
all evidence. Never trim a required quote/occurrence to force an approval.

Retain `max_tokens=4096` as the extraction ceiling and review's starting formula
`200 + 120 * finding_count`. Reserve the actual final payload including framing,
output, and correction separately for every attempt. Split candidate batches
and planning gap lists deterministically until they fit remaining run/day room.
An unfittable singleton is deferred with `cost_cap`, not rejected as false.
Strict validation requires bounded arrays, allowed fields/types, exact quotes,
and exactly one review for every requested ID with no missing/duplicate/foreign
IDs. Reject the whole invalid response; retain bounded retry/accounting behavior.
Local validated-result replay and provider prefix caching remain distinct.

## 3. Monthly published-restaurant rechecks

Add a small additive private-state migration (next available number, expected
`0008_deepseek_efficiency.sql`) containing:

| State | Required information |
| --- | --- |
| `restaurant_recheck` | Stable seed restaurant ID, target ID, `last_attempted_at`, `last_checked_at`, `next_due_at`, active attempt ID, failure count, last outcome |
| `restaurant_recheck_attempt` | Attempt ID, restaurant ID, due-period key, queued/start/completed timestamps, state and reason; unique restaurant/due-period |
| Recheck job/evidence links | Attempt-to-job/fetch/extraction references, including shared article provenance; no new unsupported `crawl_job.path` value |
| AI workload attribution | Request-to-workload metadata for reservation/settlement (`fresh` or `recheck`), preserving existing `ai_request` accounting |
| Review admission holds | Attempt/day, held units, state and associated review request; atomic conversion/release, included in available room without double billing |

Initialize eligible published seed restaurants with deterministic due-date
staggering over 30 days, leaving `last_checked_at` null. Thereafter a completed
check is due again after 30 days. Select oldest due first and admit at most
`min(10, max(1, ceil(published_restaurants / 30)))` restaurants per KST day.
Persist admission across reruns using transactions and uniqueness, not an
in-memory count. Overdue checks remain visible; bounded monthly coverage is
best effort when the population, source access, or budget exceeds capacity.

Use existing permitted official/source discovery and `entity_followup` or
`keyword_search` jobs in the refresh pool for closure, relocation, and chef
change. Reuse/coalesce existing status/official jobs where possible. Teach
generic `requeue_due` not to reset monthly-linked work to its daily volatile
cadence. Preserve source permissions, robots checks, URL quotas, and deduplication.
No new Tavily or other paid discovery service is required.

Fresh work receives priority: process ordinary review backlog before recheck
backlog, preserve the scheduler's discovery reservation, and limit monthly jobs
to at most 20% of selected fetch slots (round down; if zero, defer). Recheck AI
uses only remaining daily room and at most `min(8, 0.4 * daily_cap)` units per
KST day, including extract, review, retries, and unknown reservations. Enforce
the sublimit atomically inside the existing budget transaction, not only in
the scheduler. It is a subset of the 20-unit cap, never an additional allowance.
Batch same-article rechecks; if mixed with fresh findings, conservatively charge
the whole call to the recheck sublimit or defer the recheck members when it does
not fit, allowing fresh work to proceed.

Before admitting paid recheck extraction, leave enough room for a conservative
review reservation using the planned context and maximum candidate count.
Unspent review room is an admission hold, not double-charged usage. Persist it
and release/consume it transactionally so a competing run cannot steal it.
Prioritize actual review over speculative new extraction. Lower caps may make
rechecks impossible for a day; report deferral instead of increasing allowances.

All changed-state candidates, including rule-derived recheck findings, enter
the existing extraction review queue (`reason=ai_inference`) with
`pending_only=1`, `epistemic=inference`, and review-required provenance. Retain
the actual rule extractor identity in metadata rather than inventing a model
extraction request. Route these before resolve/apply can promote a target,
resolve a gap, or mutate public/service facts. Ensure the daemon bridge and
legacy apply path cannot publish a duplicate unreviewed version.

Use the same review validator, decision writer, badge, log, and rollback IDs as
fresh AI findings. Refine the common review instruction: a dated, explicit later
closure/move/chef change may supersede an older site record; a conflicting,
ambiguous, speculative, or undated assertion is rejected. Approval publishes
the supported annotation and does not silently change `chef` or `closed_at`.

Advance `last_checked_at` only after permitted source inspection completes and
all resulting candidates are approved/rejected, or the inspected evidence yields
no change candidate. The latter means “checked; no supported change found,” not
“confirmed open.” A blocked fetch, missing source, budget/balance stop, partial
inspection, or unfinished review is not a completed check. Retry failures after
1, 3, then 7 days (maximum), respecting existing access blocks; keep an in-flight
attempt for deferred review so restarts do not enqueue another monthly check.

## 4. Offline weekly chef-move digest draft

Add a deterministic writer, `scripts/build_weekly_digest.py`, called after the
last review pass. No LLM, Argon subprocess, HTTP, social SDK, email, or publishing
command is permitted in this writer. Write only beneath `marketing/drafts/`
and reject path escapes/symlinks. Do not alter `marketing/x_launch_argon.md`.

Default window: the most recently completed Monday 00:00–Monday 00:00 week in
`Asia/Seoul`, selected by approval time, not article date. A daily invocation
catches a missed Monday. Allow an explicit week argument for offline regeneration.
Emit `chef-moves-YYYY-MM-DD.md` with deterministic ordering/content and atomic
replacement; no timestamp churn and no empty-file churn when no findings qualify.

Read approval/rollback events from `data/ai_review_log.jsonl` and reconcile with
entries still present in seed `ai_reviewed`/`ai_reviewed_unlinked`. Include only
published approvals for `career` and `employment_claim` with a named chef and
source, deduplicated by extraction/review identity and claim. Exclude rejected,
pending, rolled-back, duplicate unpublished approvals, restaurant relocation
alone, menu, and speculative career claims. Render verbatim approved quotes and
existing names/URLs with review IDs; do not generate unsupported prose. Describe
these as findings approved during the week, not necessarily moves occurring then.

Required header on every generated file:

> DRAFT — NOT APPROVED FOR PUBLICATION. This draft MUST pass Gemini Argon review
> via `marketing/argon_review.sh` before any human/VIRAL publishes it. Generation
> does not run Argon or publish to X, Instagram, email, or any network service.

The existing untracked `marketing/argon_review.sh` invokes `agy`/Gemini and has
no established offline mode. Do not execute or track it during DESIGN. During
BUILD, inspect and track only that reviewed script if appropriate; adapt its
fixed five-post/profile format for digest input if necessary without adding a
daily invocation. Argon is a separate explicit step, subject to the no-spend
constraint. Until available and approved, the draft remains unpublished.
Changed or regenerated copy requires a fresh Argon pass; mere script existence
or a successful generator run is not evidence of approval.

Keep drafts available locally through the daily flow; if they are included in a
later PR, stage only the generated `marketing/drafts/` paths, never the entire
pre-existing untracked marketing directory. Draft-only changes require no deploy.
Regeneration must remove rolled-back entries, including from older generated
weeks, or mark an emptied draft withdrawn; it must not leave stale publishable
copy. Preserve human-edited/Argon output in separate files and flag it for renewed
review when the underlying generated input changes.

## 5. Balance gate and summaries

Add `daemon/kg_ai/balance.py` and inject a balance transport/clock into the shared
client. Production behavior is a bounded authenticated
`GET https://api.deepseek.com/user/balance` with Bearer authorization, disabled
proxy inheritance, rejected redirects, a short timeout (5 seconds), and a
bounded body (16 KiB). Tests inject both balance and completion transports;
injecting a completion fake alone must never fall through to real balance HTTP.

Parse `is_available` as a strict boolean and `balance_infos[].total_balance`
as finite nonnegative Decimal strings. `total_balance` already includes grant
and top-up funds; do not add those fields again. The provider documents USD and
CNY entries: [Get User Balance](https://api-docs.deepseek.com/api/get-user-balance/).
Use clear operational floors: USD 0.50 and CNY 4.00. For mixed supported wallets,
sum `USD_total / 0.50 + CNY_total / 4.00` and require at least 1; reject duplicate
currency rows or unknown currencies as an unsupported/malformed response. This
is a conservative configured policy, not a claim about current exchange rates,
and requires no FX network request.

`is_available=false` or a valid total below the floor yields `balance_low`.
Exactly the floor is allowed. Invalid/missing totals, unsupported currencies,
HTTP/auth/rate errors, timeout, oversized/malformed JSON, or redirects yield
`balance_unavailable` and skip AI conservatively. Missing key yields
`missing_key` without HTTP. Explicitly disabled AI does not query balance.

Check before collect planning/extraction, local-daemon extraction, and each
review invocation, with an in-process successful-result TTL of at most 60 seconds
and revalidation before a later outbound completion after expiry. Latch a low
or unavailable result for the invocation to avoid retry storms. Each process
checks independently; never serialize a credential or an untrusted shell “OK”
override. The shared client enforces the guard before reservation/HTTP so direct
CLI callers cannot bypass it. Offline validated-result replay may remain usable
without another paid call; it must retain valid provenance and review semantics.

Balance checks consume no model reservation and cannot erase existing holds.
A low balance is an expected pause, not a daemon failure. Continue deterministic
collection, linked-decision cleanup, build, and local digest generation. Review
items awaiting a model decision remain open with a visible deferral reason;
never reject them merely to hide a backlog or fabricate approval. At $0, safe
deferral is the unavoidable exception to the “no pending pile-up” goal. Avoid
creating new paid-work backlog until review can resume.

Expose only fixed reason codes and counts, not keys, authorization headers, raw
responses, or account balances. Preserve existing summary fields and add explicit
`reason=` consistently. Illustrative outputs:

```text
COLLECT=succeeded ai=paused reason=balance_low ai_skipped_no_targets=12
AI_REVIEW approved=0 rejected=0 published=0 calls=0 remaining=3 stopped=balance_low reason=balance_low
DAEMON=finished exit=0 ai=paused reason=balance_low
RESULT=NO_CHANGE reason=balance_low
```

Parse daemon JSON instead of discarding it. Aggregate both review passes and
collector/daemon outcomes in shell state, including NO_CHANGE and every other
RESULT branch. If any AI phase reports `balance_low`, retain it through later
successful deterministic phases. On a real pipeline failure preserve its primary
`reason=command_failed` and add `ai_reason=balance_low`; do not conceal errors.
Keep exactly one final RESULT line and distinguish missing/invalid summaries
from a provider pause. Do not scrape arbitrary stderr or exception strings.

## Planned file touch list (BUILD, not this stage)

| Files | Purpose |
| --- | --- |
| `daemon/kg_collect/targets.py` | Shared eligibility policy and registry aliases |
| `daemon/kg_collect/deepseek.py`, `pipeline.py`, `cycle.py`, `cli.py` | Plan/extract gating, article batching, recheck routing, counters/results |
| `daemon/kg_collect/discover.py`, `scheduler.py`; new `recheck.py` | Monthly admission, durable progress, coalesced refresh jobs and fairness |
| New `daemon/kg_ai/balance.py`, `article.py` | Injected balance guard and stable source-context/prompt builder |
| `daemon/kg_ai/deepseek.py`, `budget.py` | Guard enforcement, message hashing, reservations and recheck sublimit/holds |
| `daemon/kg_ai/review.py` | Gazetteer gate, cross-fetch article batches, temporal-change review, workload priority and reasons |
| `daemon/kg_daemon/deepseek.py`, `cli.py`, `collect_bridge.py`, `stages.py` as needed | Shared gating, cap enforcement, review-required bridge isolation and daemon summary |
| Next additive `schema/migrations/0008_deepseek_efficiency.sql` | Monthly state, provenance links, AI workload/hold bookkeeping; verify number at BUILD |
| `scripts/collect_daily.sh`, `scripts/daily_pipeline.sh` | Safe disabled IG setting, monthly/digest wiring and reason aggregation |
| New `scripts/build_weekly_digest.py`, generated `marketing/drafts/*.md` | Offline weekly draft and rollback reconciliation |
| `marketing/argon_review.sh` | Review/track existing local script in BUILD only; explicit separate Argon step |
| `docs/data/DEEPSEEK_COLLECT_RUNBOOK.md`, `DAEMON.md`; marketing draft guidance | Reconcile stale policy, monthly cadence, balance reasons and mandatory Argon review |
| Existing DeepSeek/review/daemon/wiring tests; new recheck, balance and digest tests | Mocked acceptance coverage below |

`config/dev_lanes.json` is read-only orchestration context, not a proposed model
or lane change. No site template/JS or public seed change is needed to implement
these mechanisms. Approved findings may subsequently change data/site output
through the existing reviewed publication flow. Rollback script changes are
unnecessary unless implementation introduces new entry fields it must preserve.

## Offline verification and acceptance plan

BUILD must run relevant pytest with fake HTTP, clocks, temporary databases, and
temporary seed/log/draft files. Block unexpected outbound networking so a missing
fake fails immediately. Shell integration tests stub crawling, git, gh, deployment,
and subprocesses. No production pipeline, live DeepSeek (including balance),
Argon, Instagram, paid search, secret files, or deployment is used in tests.

| Improvement | Required mocked tests |
| --- | --- |
| Pre-filter | Known chef/restaurant and aliases admitted; unknown-only, candidate-only, context-ID-only and region-only pages skipped; mixed known/unknown accepted; plan metadata handled correctly; post-truncation eligibility; review skip logged/closed; no completion/reservation for ineligible input; stage counters |
| Batching | Same canonical article/snapshot across fetches produces one request; different snapshots separate; source appears once before variable data; deterministic order/prefix and replay keys; evidence offsets preserved; invalid/missing/duplicate reviews rejected; batch/window splits fit actual reservations; unfittable singleton deferred; retries/unknown usage retained |
| Monthly recheck | Initial 30-day staggering, due boundaries, persisted timestamps across restarts, concurrent daily admissions, no duplicate jobs, generic requeue exclusion, daily fetch share/subcap, review headroom, fresh-work priority, low-cap deferral, backoff, no-change versus failed/partial inspection, rule candidates cannot bypass review, dated changes can approve, reject/approve/log/badge/rollback lifecycle |
| Weekly draft | KST Monday/year boundaries, approval-week selection, career/employment allowlist, no duplicates/pending/rejections/rollbacks, source/quote fidelity, idempotent regeneration and missed-week recovery, older-draft rollback removal, mandatory header, path containment, no network/Argon/publish subprocess |
| Balance | `is_available=false`, USD below/equal/above 0.50, CNY floor/mixed wallets, Decimal/schema failures, timeout/redirect/HTTP errors, missing key/disabled AI, TTL expiry, stage checks/direct CLI, no completion or new reservation on pause, no secret output, all COLLECT/AI_REVIEW/DAEMON/RESULT branches preserve reason |

Extend `tests/test_deepseek_client.py`, `test_deepseek_collect.py`,
`test_ai_review.py`, `test_daily_collect_wiring.py`, and `test_daemon.py`; add
`test_deepseek_balance.py`, `test_monthly_recheck.py`, and
`test_weekly_digest.py`. Run the related collector/ontology migration tests when
schema or apply isolation changes. Validate shell syntax without executing the
pipeline. Existing accounting, strict evidence, review rollback, and apply
isolation tests remain mandatory regressions.

At DESIGN completion, verify only this Markdown file is changed, review its
diff/links, and run `git diff --check`. Runtime pytest is deferred to BUILD
because no implementation or tests change here. Do not stage unrelated local
`.devpipe-font/` or marketing files. Open a design-only PR listing all five
items and explicitly state that implementation and mocked acceptance tests are
future stages; do not claim the overall implementation is complete.
