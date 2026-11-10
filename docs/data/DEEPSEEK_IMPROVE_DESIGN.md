# DeepSeek collector efficiency improvements

Status: RE-REVIEW implemented (2026-11-10), awaiting audit. Owner order:
2026-11-10. The reviewed design below is preserved as the implementation contract.
Verification uses mocked completion and balance HTTP only. No paid call, live
provider smoke, runtime database migration, deployment, or publication was run.

BUILD notes:

- `kg_ai.balance` guards the shared client before reservation/HTTP, with explicit
  injectable completion and balance transports. USD 0.50 / CNY 4.00 floors, a
  60-second healthy TTL and invocation pause latch apply to all model paths.
- `kg_collect.targets.eligibility` admits only active registered chef/restaurant
  mentions after sanitization and fitting. Context IDs alone cannot admit text.
  Planning validates active open-gap targets because it sends metadata, not pages.
- `kg_ai.article` provides stable source messages before variable findings and
  correction data. Review batches equivalent URL/snapshot articles across fetches;
  extraction reuses equivalent snapshot results and records every contributing
  fetch in `article_extraction_fetch`. Request versions are bumped independently.
- Migration 0008 adds private monthly schedules, attempt/job/fetch/extraction
  lineage, workload attribution and review holds. Rechecks preserve fresh priority
  and cost/fetch sublimits. Rules inspect the whole eligible source first: clear
  changed-state findings go directly to the common DeepSeek review queue; a full
  rule scan without changes records only “no change observed,” never “confirmed
  open.” Ambiguous rechecks may request model extraction with a two-finding limit
  and 1024 output tokens (within the 4096 ceiling), reserving review room first.
  Budget, balance, partial inspection and unfinished reviews do not advance the
  completed-check timestamp. Coverage remains best effort under the shared cap.
- Local daemon `candidate_fact` orphans enter the same review/decision/publish
  path; linked candidates follow the extraction decision. Approved annotations
  retain rollback-compatible extraction/review IDs and cannot apply core facts.
- `scripts/build_weekly_digest.py` writes offline drafts only, reconciles live
  seed approvals and rollbacks, catches missed weeks, protects human/Argon copy,
  and requires a separate explicit Argon review before publication. The inspected
  `marketing/argon_review.sh` is tracked with an explicit `--digest` mode; its
  original profile/posts mode is retained. Neither mode was executed.
- Both shell summaries retain AI pause reasons across deterministic work and
  review passes. `KG_IG` defaults to zero. Local-daemon caps are clamped to 20;
  collector HTTP units remain separate from the shared model allowance.

BUILD REVIEW corrections (R1–R4 in `DEEPSEEK_IMPROVE_REVIEW.md`):

RE-REVIEW also keeps a partially fitted recheck deferred when its remaining
prefix contains no registered names, so the final pre-filter cannot certify an
incomplete inspection. Child-job lineage excludes finished history from a
coalesced root; queued children and activity since admission still count toward
the current attempt, including real failures. Both cases have offline regressions.

- Recheck extraction fits its own byte reservation plus full review-context hold
  before reserving either. A workload deferral leaves fresh extraction enabled;
  empty/error results release only the new hold or restore prior headroom.
- Migration 0009 separates verbatim rule `date_text` from normalized `date_iso`.
  Review uses the former; date consumers prefer the latter. Existing queued rule
  rechecks recover literal dates from their structured findings. Model evidence
  retains the strict verbatim/ISO extraction checks and is never repaired.
- A completed monthly inspection must mention the due restaurant's own active
  Gazetteer alias. Chef-only mentions and job/search metadata do not establish
  a restaurant inspection, even when they pass general AI eligibility.
- A coalesced job contributes only fetches at or after the attempt's `queued_at`
  boundary, consistently during extraction and reconciliation. Historical
  extracted/unextracted fetches retain their ordinary provenance; current
  uninspected fetches and open reviews keep the attempt pending.


Review fixes folded into this revision:

- R1: review admission for orphaned daemon-local `candidate_fact` AI findings,
  which the current `kg_ai.review` never selects.
- R2: explicit active-target eligibility, re-checked after sanitization and
  budget truncation, plus plan metadata rules.
- R3: `fresh`/`recheck` workload attribution so review priority and the recheck
  sublimit are enforced inside the budget transaction.
- R4: balance-low clean-skip counters and the no-network transport-injection
  contract, so a provider pause is not counted as an extraction failure.
- R5: shell `KG_IG` default, daemon JSON parsing, and reason-precedence details.
- R6: migration/bootstrap/requeue specifics for monthly rechecks.
- R7: digest week/file/path boundaries and the explicit Argon separation.

## Scope and invariants

Implement the five improvements below through the development stages in
`config/dev_lanes.json`: design → design_review → build → build_review →
review_fix → re_review → audit. These development stages are distinct from the
collector's runtime stages, `plan/fetch/extract/evaluate/gaps/apply/quality`.
The design was reviewed in DESIGN REVIEW; this branch implements BUILD without
invoking any paid development lane. Cursor CloudAgent is forbidden. With the current $0
balance and no-spend order,
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
  only if site/web output changes; this implementation changes no site/web output and requires no deploy.

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

### Review admission and pending-work isolation (R1)

`kg_ai.review` currently selects only `review_queue.subject_type='extraction'`.
`candidate_fact` AI reviews are closed only by `close_linked_candidates` when
their source extraction has a decision. The daemon-local adapter
(`daemon/kg_daemon/deepseek.py`) writes `candidate_fact` rows whose source is a
daemon `document`, not a collector `extraction`, so those reviews have no
admission path and can remain open forever. That is an existing violation of the
2026-11-09 “no pending pile-up” order and must not be extended by this work.

Required resolution:

- Recheck findings are emitted as collector-compatible `extraction` rows plus
  `review_queue(subject_type='extraction', reason='ai_inference')`. They must
  not be emitted only as daemon `candidate_fact` rows.
- `kg_ai.review` admits a second category: open `candidate_fact` AI reviews not
  linked to an extraction review, built from the candidate's stored document
  (`document.raw_ref`), sanitized text, evidence quote, and payload. It uses the
  same eligibility gate, batched review request, validator, decision writer,
  badge, log, and rollback lineage. Approve publishes the verbatim quote; reject
  closes the review and marks the candidate rejected. If the source is missing
  or the document has no eligible target, the item is closed deterministically
  with the existing `source_unavailable` / `skipped_no_targets` reason and
  counted; it is never left pending merely because it has no extraction link.
- A `candidate_fact` item that already follows an extraction decision is not
  reviewed a second time.
- Summaries expose `unreviewed_candidate_findings` (and its reason classes) so
  a future orphan path cannot hide.

This keeps one review implementation and one publication path. The workload
marker described in section 2 applies to both admission categories.

## 1. One cheap eligibility gate

Extend `targets.py` with a reusable eligibility result: eligible target IDs,
matched spans, and a fixed reason code. Reuse Gazetteer normalization and
aliases, not a second chef-name list. Synchronize published seed names, previous
restaurant names, and chef aliases into that registry without promoting targets
from model output. Ambiguous aliases may admit a page but never resolve an
identity without the existing exact-match checks.

Eligibility requires a chef or restaurant mention in the actual sanitized text
being submitted, mapped to a `target` row that is `kind IN ('chef','restaurant')`,
`status='active'`, and either `origin='seed'` or `origin='discovered'` (a
registered active discovered target). A region/cuisine, trusted source host,
`status='candidate'` target, query name, or caller-supplied context target ID
alone is insufficient. Existing Gazetteer users may still need candidate
targets, so implement this narrower policy in the eligibility helper (returning
eligible IDs, matched spans, and a fixed reason code) rather than changing all
matching behavior. Reuse Gazetteer normalization, aliases, and exact-match
rules; do not add a second chef/restaurant name list.

Apply it before reservation/completion in extract, local-document extraction,
and review. The helper re-runs on the final text actually submitted, not only
the pre-truncation document: sanitize, normalize, budget-shorten, then call
`gaz.find` on the shortened text and require an eligible span. Review first
performs its existing quote/name/date checks, then applies the same gate to the
sanitized article context (not only the finding's occurrence excerpts). For the
raw-text fallback, sanitize the raw text first and re-run the quote check; a
quote that survives only in redacted raw text is not eligible for publication
or a model prompt. A missing source or no eligible target closes an existing
review deterministically with the reason, records it in the log, and closes
linked candidates; it never approves one.

Every stage counts `considered`, `eligible`, `skipped_filter`,
`skipped_no_targets`, `skipped_not_needed`, `skipped_budget`, `called`,
`failed`, and `findings`, and separately counts `skipped_balance` (section 5)
so a provider pause is not misreported as a target/filter skip or a failure.
Retain the existing extraction counter names for compatibility. Count documents
once; report finding counts separately in review so batching does not inflate
skips.

Planning has no article body. Do not apply the article-text gate to empty
planning metadata, which would disable all planning; instead require every
gap's target ID to exist, be an active eligible chef/restaurant, and still own
an open gap. Count invalid/unknown planning targets separately. If a future plan
includes source text, apply the article gate before sending it.

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
sanitized source snapshot and an explicit normalization version
(`ARTICLE_NORMALIZATION_VERSION`, bumped whenever sanitization/NFC/truncation
changes). The snapshot digest covers the sanitized normalized text actually
eligible for the prompt, not the raw bytes alone. Different snapshots at the
same URL remain separate; URL canonicalization alone must never mix conflicting
revisions. Preserve each finding's extraction/fetch IDs and exact source offsets
for audit and rollback.

Introduce a shared article prompt builder in `kg_ai` (`article.py`) with this
message order:

1. Fixed system instruction identifying source content as untrusted data.
2. Stable source message: canonical URL, publication metadata, and one sanitized
   article body/context, serialized deterministically.
3. Task-specific instruction and sorted candidate/target IDs, site records,
   questions, or retry correction. Variable batch data follows the article.

The builder accepts only sanitized text; callers must not hand it raw stored
text. For extraction, the stable source is the sanitized body that fits the
budget. For review, it is the full sanitized body when it fits; otherwise use a
deterministic bounded shared context containing the eligible mentions and every
required quote occurrence, with an explicit context digest. Never silently drop
a required quote to make a context fit. The same article key plus the same
context digest must produce identical prefix bytes across batch splits and
retries.

Current sorted JSON can put `findings` before source content. Explicit messages
avoid that layout. Keep retry corrections out of the fixed prefix. Ensure
request keys and source hashes cover all messages, model, task/prompt/schema
versions, article key, selected context digest, and candidate/target data;
increment affected versions. Do not invalidate unrelated completed requests or
uncertain reservations.

For extraction, group `fetch_record` rows by article key before dispatch. Reuse
one request/result for equivalent snapshots and fan the validated findings back
to every contributing fetch/extraction row with provenance retained; different
snapshots remain separate requests. Do not send one request per chef, target, or
ambiguous rule candidate.

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

Review carries an explicit `workload` (`fresh` or `recheck`) on extraction
provenance, the review payload, and the `ai_request`. It processes fresh
workload items before recheck items, then recheck items in oldest-due order. A
mixed article batch is charged to `recheck` when it contains any recheck
member; if that would exceed the recheck sublimit, deterministically split out
the recheck members and defer them, allowing the fresh members to proceed. This
priority applies to both admission categories in R1.

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
| Recheck job/evidence links | Attempt-to-job/fetch/extraction references, including shared article provenance; no new unsupported `crawl_job.path` value. Use a link table such as `restaurant_recheck_job(attempt_id, job_id, fetch_id, extraction_id)` rather than overloading `crawl_job`. |
| AI workload attribution | Add `ai_request.workload TEXT NOT NULL DEFAULT 'fresh'` with CHECK (`fresh`,`recheck`) and an index on `(day, workload)`. `Budget.reserve` records the workload and enforces the recheck sublimit in the same `BEGIN IMMEDIATE` transaction; existing rows and accounting stay valid. |
| Review admission holds | Attempt/day, held units, state and associated review request; atomic conversion/release, included in available room without double billing |

Define “eligible published seed restaurant” as a `target` row with
`kind='restaurant'`, `origin='seed'`, `status='active'`, and a non-empty
`attrs_json.seed_id` that backs a published seed record. Include
`role='origin_kitchen'` targets because their kitchen pages are published; do
not treat their chef as current employment without the usual evidence rules.

Bootstrap with a deterministic, idempotent `ensure_schedule(conn, seed)` called
from `stage_plan` (and safe to call from a maintenance CLI), not with seed data
embedded in the SQL migration. It inserts missing scheduled restaurants and
assigns deterministic due-date staggering over 30 days from a stable hash of the
seed ID, leaving `last_checked_at` null. Thereafter a completed check is due
again after 30 days. Select oldest due first and admit at most
`min(10, max(1, ceil(published_restaurants / 30)))` restaurants per KST day.
Persist admission across reruns using transactions and uniqueness, not an
in-memory count. Overdue checks remain visible; bounded monthly coverage is
best effort when the population, source access, or budget exceeds capacity.

Use existing permitted official/source discovery and `entity_followup` or
`keyword_search` jobs in the refresh pool for closure, relocation, and chef
change. Reuse/coalesce existing status/official jobs where possible. Link each
admitted job in `restaurant_recheck_job`. Generic `requeue_due` must skip a done
job while an active recheck link exists, or set `crawl_job.not_before` to the
attempt's `next_due_at`; the daily/14-day cadence must not queue monthly-linked
work early. Clear or replace the link when the attempt completes or fails so
normal cadence resumes. Preserve source permissions, robots checks, URL quotas,
and deduplication. No new Tavily or other paid discovery service is required.

Fresh work receives priority: process ordinary review backlog before recheck
backlog, preserve the scheduler's discovery reservation, and limit monthly jobs
to at most 20% of selected fetch slots (round down; if zero, defer). Recheck AI
uses only remaining daily room and at most `min(8, 0.4 * daily_cap)` units per
KST day, including extract, review, retries, and unknown reservations. Enforce
the sublimit atomically inside the existing budget transaction via
`ai_request.workload='recheck'`, not only in the scheduler. It is a subset of
the 20-unit cap, never an additional allowance. A balance pause
(`balance_low`/`balance_unavailable`) defers the attempt rather than charging or
advancing `last_checked_at`. Batch same-article rechecks; if mixed with fresh
findings, conservatively charge the whole call to the recheck sublimit or defer
the recheck members when it does not fit, allowing fresh work to proceed.

Before admitting paid recheck extraction, leave enough room for a conservative
review reservation using the planned context and maximum candidate count.
Unspent review room is an admission hold, not double-charged usage. Persist it
and release/consume it transactionally so a competing run cannot steal it.
Prioritize actual review over speculative new extraction. Lower caps may make
rechecks impossible for a day; report deferral instead of increasing allowances.

All changed-state candidates, including rule-derived recheck findings, are
written as collector-compatible `extraction` rows on the recheck fetch and
enter the existing extraction review queue (`reason=ai_inference`) with
`pending_only=1`, `asserted_by=ai_inference`, `epistemic=inference`, a
`workload='recheck'` marker, and review-required provenance. Retain the actual
rule extractor identity in metadata rather than inventing a model extraction
request. Route these before resolve/apply can promote a target, resolve a gap,
or mutate public/service facts. The existing pending-only filters in
`gaps.resolve_gaps`, `apply`, and the daemon bridge must exclude them; ensure
the daemon bridge and legacy apply path cannot publish a duplicate unreviewed
version, and do not emit them only as daemon `candidate_fact` rows (R1).

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

Default window: the most recently completed Monday 00:00 (inclusive) to the next
Monday 00:00 (exclusive) in `Asia/Seoul`, selected by approval time in the log,
not article date. A daily invocation catches a missed Monday. An explicit
`--week YYYY-MM-DD` (a Monday) enables offline regeneration. Emit
`marketing/drafts/chef-moves-YYYY-MM-DD.md` using that Monday date, with
deterministic ordering/content and atomic replacement (write a temporary file in
the same directory, then `os.replace`); no timestamp churn and no empty-file
churn when no findings qualify. Refuse non-Monday week arguments and paths
containing `..`, absolute paths, or symlink targets.

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
no established offline mode. Do not execute or track it during DESIGN or
DESIGN REVIEW. During BUILD, inspect and track only that reviewed script if
appropriate; adapt its fixed five-post/profile format for digest input if
necessary without adding a daily invocation. Do not change the script's default
behavior or output contract. If a digest mode is added, make it an explicit
argument, keep it network-only when a human invokes it, and never call it from
the daily pipeline or the writer. Argon is a separate explicit step, subject to
the no-spend constraint. Until available and approved, the draft remains
unpublished. Changed or regenerated copy requires a fresh Argon pass; mere
script existence or a successful generator run is not evidence of approval. A
human-edited or Argon-revised artifact is kept in a separate file; regeneration
must not overwrite it and must flag it for a fresh Argon pass.

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
proxy inheritance, rejected redirects, a short timeout, and a bounded body.
Name the policy constants explicitly: `BALANCE_FLOOR_USD = Decimal('0.50')`,
`BALANCE_FLOOR_CNY = Decimal('4.00')`, `BALANCE_TIMEOUT_SEC = 5`,
`BALANCE_MAX_BYTES = 16 * 1024`, and `BALANCE_TTL_SEC = 60` (the TTL must not
exceed 60 seconds).

The client performs the balance check only after looking up an offline cached
result and before reservation/provider HTTP. The check is therefore absent when
AI is disabled or the key is missing, and a validated cached result can still
replay at $0 balance. The production balance transport is used only when the
client is using the production completion transport. An injected completion
transport without an explicit balance transport must fail closed as
`balance_unavailable` without any network call. BUILD updates every test client
construction to inject a healthy balance fake (or explicitly request production
balance), and adds a regression test that a completion-only fake never opens a
socket.

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

A `balance_low` or `balance_unavailable` result latches `client.stopped` and is
reported as an expected pause, not a failure. Callers must check it before
counting a `DeepSeekError` as `failed` or rejecting a review item:

- `Adapters.plan` returns no jobs and leaves `client.stopped` set; it does not
  count a planning failure.
- `Adapters.extract` and the daemon-local adapter return no findings, increment
  `skipped_balance` (not `failed`), and do not mark the document extracted, so
  a later run can retry it.
- `kg_ai.review` stops before splitting or retrying, leaves items open, and
  reports `stopped=balance_low` / `reason=balance_low`; it never rejects them
  merely to make the queue empty.
- A recheck attempt is deferred without advancing `last_checked_at`, and no new
  speculative paid extraction is admitted while the pause holds.

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

The sample lines above are illustrative additions, not a replacement: retain
existing fields such as `run=`, `cost=`, `ai_docs=...`, `prechecked=`, and
`calls=`. A `balance_low` pause must not change a stage's exit status to
failure, must not discard deterministic stage output, and must survive later
successful phases in the same pipeline. Distinguish
`reason=missing_summary`/`reason=invalid_summary` from `reason=balance_low`.

Parse daemon JSON instead of discarding it. The daemon `run` report must include
`ai`, `ai_reason`, and the AI/counter fields the shell prints; aggregate both
review passes and collector/daemon outcomes in shell state, including NO_CHANGE
and every other RESULT branch. If any AI phase reports `balance_low`, retain it
through later successful deterministic phases. On a real pipeline failure
preserve its primary `reason=command_failed` and add `ai_reason=balance_low`;
do not conceal errors. Keep exactly one final RESULT line and distinguish
missing/invalid summaries from a provider pause. Do not scrape arbitrary stderr
or exception strings.

`scripts/collect_daily.sh` currently defaults `KG_IG` to 1. For this work it
must default to 0 (explicit opt-in only), and neither daily script may create,
remove, read, or write `crawler/out/ig_blocked.flag` or
`ig_kg_blocked.flag`. Shell tests assert the default and the absence of flag
manipulation. Do not execute `scripts/daily_pipeline.sh` against this
development checkout.

## Reviewed BUILD file touch list

| Files | Purpose |
| --- | --- |
| `daemon/kg_collect/targets.py` | Shared eligibility policy and registry aliases |
| `daemon/kg_collect/deepseek.py`, `pipeline.py`, `cycle.py`, `cli.py` | Plan/extract gating, article batching, recheck routing, counters/results |
| `daemon/kg_collect/discover.py`, `scheduler.py`; new `recheck.py` | Monthly admission, durable progress, coalesced refresh jobs and fairness |
| New `daemon/kg_ai/balance.py`, `article.py` | Injected balance guard and stable source-context/prompt builder |
| `daemon/kg_ai/deepseek.py`, `budget.py` | Guard enforcement, message hashing, reservations and recheck sublimit/holds |
| `daemon/kg_ai/review.py` | Gazetteer gate, cross-fetch article batches, candidate_fact admission (R1), temporal-change review, workload priority and reasons |
| `daemon/kg_daemon/deepseek.py`, `cli.py`, `collect_bridge.py`, `stages.py` as needed | Shared gating, cap enforcement, review-required bridge isolation, balance clean-skip and daemon summary |
| Next additive `schema/migrations/0008_deepseek_efficiency.sql` | Monthly state, provenance links, `ai_request.workload`, AI hold bookkeeping; verify number at BUILD |
| `scripts/collect_daily.sh`, `scripts/daily_pipeline.sh` | `KG_IG=0` default, monthly/digest/balance wiring, daemon JSON parsing and reason aggregation |
| New `scripts/build_weekly_digest.py`, generated `marketing/drafts/*.md` | Offline weekly draft and rollback reconciliation |
| `marketing/argon_review.sh` | Review/track existing local script in BUILD only; explicit separate Argon step |
| `scripts/ai_review_rollback.py` | Extend only if R1 adds a candidate ID field that rollback must match |
| `docs/data/DEEPSEEK_COLLECT_RUNBOOK.md`, `DAEMON.md`; marketing draft guidance | Reconcile stale policy, monthly cadence, balance reasons and mandatory Argon review |
| Existing DeepSeek/review/daemon/wiring tests; new recheck, balance and digest tests | Mocked acceptance coverage below |

`config/dev_lanes.json` is read-only orchestration context, not a proposed model
or lane change. No site template/JS or public seed change is needed to implement
these mechanisms. Approved findings may subsequently change data/site output
through the existing reviewed publication flow. Rollback script changes are
unnecessary unless R1 or recheck implementation introduces new entry fields it
must preserve; preserve `--extraction-id`, `--review-id`, and `--since`
compatibility.

## Offline verification and acceptance plan

BUILD must run relevant pytest with fake HTTP, clocks, temporary databases, and
temporary seed/log/draft files. Block unexpected outbound networking so a missing
fake fails immediately. Shell integration tests stub crawling, git, gh, deployment,
and subprocesses. No production pipeline, live DeepSeek (including balance),
Argon, Instagram, paid search, secret files, or deployment is used in tests.

| Improvement | Required mocked tests |
| --- | --- |
| Pre-filter | Known chef/restaurant and aliases admitted; unknown-only, candidate-only, context-ID-only and region-only pages skipped; mixed known/unknown accepted; plan metadata handled correctly; sanitize-then-truncate eligibility; review skip logged/closed; `candidate_fact` admission and orphan closure (R1); no completion/reservation for ineligible input; `skipped_balance` distinct from `failed`; stage counters |
| Batching | Same canonical article/snapshot across fetches produces one request; different snapshots separate; source appears once before variable data; stable prefix bytes across batch splits and retries; context digest; full-body versus bounded-context review; deterministic order/replay keys; evidence offsets preserved; invalid/missing/duplicate reviews rejected; batch/window splits fit actual reservations; unfittable singleton deferred; retries/unknown usage retained |
| Monthly recheck | Idempotent bootstrap, 30-day staggering and due boundaries, persisted timestamps across restarts, concurrent daily admissions, no duplicate jobs, additive 0008 migration, `ai_request.workload`, generic requeue exclusion, daily fetch share/subcap, review headroom, fresh-before-recheck priority, low-cap and balance-low deferral without advancing `last_checked_at`, backoff, no-change versus failed/partial inspection, rule candidates cannot bypass review, dated changes can approve, reject/approve/log/badge/rollback lifecycle |
| Weekly draft | KST Monday/year boundaries, non-Monday argument rejection, approval-week selection, career/employment allowlist, no duplicates/pending/rejections/rollbacks, source/quote fidelity, idempotent atomic regeneration and missed-week recovery, older-draft rollback removal, mandatory header, path/symlink containment, human/Argon artifact preservation, no network/Argon/publish subprocess |
| Balance | `is_available=false`, USD below/equal/above 0.50, CNY floor/mixed wallets, Decimal/schema failures, timeout/redirect/HTTP errors, missing key/disabled AI, TTL expiry and invocation latch, cached replay at zero balance, completion-only fake opens no socket, stage checks/direct CLI, clean-skip counters, no completion or new reservation on pause, no secret output, all COLLECT/AI_REVIEW/DAEMON/RESULT branches preserve reason |

Extend `tests/test_deepseek_client.py`, `test_deepseek_collect.py`,
`test_ai_review.py`, `test_daily_collect_wiring.py`, and `test_daemon.py`; add
`test_deepseek_balance.py`, `test_monthly_recheck.py`, and
`test_weekly_digest.py`. Run the related collector/ontology migration tests when
schema or apply isolation changes. Validate shell syntax without executing the
pipeline. Existing accounting, strict evidence, review rollback, and apply
isolation tests remain mandatory regressions.

BUILD verification runs the collector, review, daemon, migration, shell-wiring,
balance, recheck and weekly-digest tests with injected HTTP and temporary state.
`tests/conftest.py` rejects unexpected live DeepSeek requests. Shell tests execute
only stubbed external commands in temporary clones. Run `bash -n` on both daily
scripts and the Argon script; do not execute the real daily pipeline or Argon.
Keep unrelated `.devpipe-font/` and `marketing/x_launch_argon.md` out of the commit.
Open the feature PR for human/coordinator review and squash merge; never merge it
from this BUILD task.
