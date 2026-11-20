# DeepSeek web and Kitchen Genome Instagram collection

The November 20 access policy enables saved-session Instagram in the daily
pipeline and selects web egress by local tunnel health. The shared model allowance
still applies. Offline verification uses injected HTTP and balance responses.
`balance_low` and `balance_unavailable` pause model work while deterministic
stages continue. The reason is carried through COLLECT, AI_REVIEW, DAEMON and
the final RESULT summary.

AI findings publish only after the common DeepSeek review approves an exact
source quote. Publication adds `ai_reviewed`, the `AI 검토` badge, source URL,
review IDs and `data/ai_review_log.jsonl`. Rejection closes with a reason.
Rollback: `scripts/ai_review_rollback.py`; core seed fields are not overwritten.
This supersedes older pending-only/publication descriptions in this runbook.

Unknown-only pages skip AI (`skipped_no_targets`). A known restaurant with a new
chef, or known chef with a new restaurant, may pass; no model output promotes a
target. The gate uses active seed/discovered Gazetteer aliases in the submitted
sanitized context. Plan requests instead validate active open-gap metadata.

Monthly rechecks bootstrap and stagger published targets over 30 days in the
private service DB. Daily admission, a 20% fetch share and an AI sublimit of
`min(8, 0.4 * shared_daily_cap)` protect fresh work. All changed-state candidates,
including rules, use the same DeepSeek review. Failed/partial inspections and
balance/budget deferrals do not advance last-checked time. Full inspections with
no changed-state candidates mean “no change observed,” not confirmed open.

After the last daily review, the offline weekly writer generates completed KST
weeks beneath `marketing/drafts/`. Drafts MUST pass Gemini Argon review via
`marketing/argon_review.sh` before any human/VIRAL publishes. The pipeline never
runs Argon or posts drafts. Keep revised copy separate; regenerated input needs
a fresh Argon pass. See [draft guidance](../../marketing/drafts/README.md) and
[implementation design](DEEPSEEK_IMPROVE_DESIGN.md).


Implemented on `feat/deepseek-collect-ig-daemon` from main, following
[the collection design](DEEPSEEK_COLLECT_DESIGN.md). Runtime work is local Python;
there are no Grok Bot chat calls or Cursor CloudAgent jobs.

## Prepare the private runtime

Install `daemon/requirements.txt` for the service daemons. The optional Instagram
read backend also needs `instaloader` and its `requests` dependency. Use a trusted,
operator-managed installation. Unit tests need neither Instagram nor credentials.

Use an existing service DB, back it up, then explicitly apply the additive migration:

```bash
PYTHONPATH=daemon python3 -m kg_daemon --db data_store/kg_service.db migrate
```

For a new development DB only, `PYTHONPATH=daemon python3 -m kg_collect init`
creates schema and seeds targets. Do not initialize a replacement production DB to
recover a missing mount. The daily script skips AI/IG when the service DB is absent;
the deterministic crawler uses its separate ignored local web quota DB in that case.
All callers must use the same `KG_SERVICE_DB` path to share daily model/web limits.

Supply credentials through the process environment or the operator's secret
manager. Never pass their values as CLI arguments or enable shell tracing:

- `DEEPSEEK_API_KEY`: shared DeepSeek client; absent key disables AI explicitly.
- Session: `/home/box/.secrets/kg_ig_kitchen_genome.session`, mode 0600.
  A missing file skips before any network call. The collector does not
  password-login and does not create a session.
- Login identity: exactly `kitchen.genome`. The saved session is checked
  against that username. A different or invalid identity stops, without relogin.

`DEEPSEEK_MODEL` defaults to the owner-ordered `deepseek-chat`. An operator can set
`DEEPSEEK_MODEL=deepseek-flash` or `deepseek-v4-pro` explicitly. There is no silent
substitution if the default is unavailable. The current
[chat completions contract](https://api-docs.deepseek.com/api/create-chat-completion/)
enables thinking when `thinking` is omitted, and
[JSON mode](https://api-docs.deepseek.com/guides/json_mode) replaces an unrecognized
completion with spaces while `finish_reason` stays `stop` and token usage is still
billed. A cycle can therefore report `succeeded` while `ai=paused` and
`ai_reason=json_blank` (previously collapsed into `invalid_task_json`). Requests
send `thinking.type=disabled` and `temperature=0`, use JSON output, a 2,048-token
default output limit (4,096 for gap planning and web extraction), and a 30-second
timeout. The client rejects redirects and disables proxy inheritance. Blank content
and one schema rejection are retried once; truncation, auth, rate limit, and
transport failures are not. A second blank or invalid task pauses that run.
Schema version is `json-2`.
The API's usage contract supplies input/output/total counts; cached input is recorded
without counting it twice. `reasoning_content` is never accepted as the task result.

## Run bounded collection

The following commands make live calls **only when an operator runs them**.
They were not run during BUILD or in CI:

Stable production entrypoint, after this change is on main:

```bash
bash scripts/daily_pipeline.sh /tmp/kg
```

That script checks out and fast-forwards main, so it must not be pointed at an
unmerged branch. Before merge, or for a bounded web-only check that does not
touch git, publish, or the legacy Instagram account:

```bash
KG_IG=0 KG_COLLECT_UNITS=2 KG_COLLECT_COST_CAP=23 \
  bash scripts/collect_daily.sh /workspace/kitchen-genome
```

That is the bounded smoke. A live run at those settings (`run_b47e3a2d16ce`)
validated one `deepseek-chat` plan, prompt 2399 / completion 1966, cost 2.1825,
and stored a 40-item result. The cycle then paused because the next reservation
was the full-document byte ceiling, which did not fit in the remaining daily
allowance. The ceiling is still the safety bound. An excerpt is shortened until
that bound fits in the remaining run cap and the daily cap of 20, so this smoke
can finish every stage. The stored plan is reused and is not billed again.
`COLLECT=succeeded` means the stages finished, including deterministic apply.
Read `ai=` and `reason=` as well: `ai=enabled` means the model calls that ran
were accepted; `ai=paused reason=cost_cap` means a later call could not fit even
a short excerpt. `COLLECT=paused_cost_cap` means fetch cost, or usage past the
reservation, stopped the run; the next run resumes that stage. Do not raise
`KG_AI_DAILY_COST_CAP` (clamped at 20) or the default `KG_COLLECT_COST_CAP` (78).

`collect_daily.sh` is also what the daily script calls for collection. It always
passes `--no-browser`. `KG_DEEPSEEK` defaults to `1`. Web collection runs when
Instagram is skipped. Direct equivalents:

```bash
PYTHONPATH=daemon python3 -m kg_collect --db "$KG_SERVICE_DB" cycle --units 60 --cost-cap 78 --no-browser --deepseek
PYTHONPATH=daemon python3 crawler/instagram_kg.py --db "$KG_SERVICE_DB" --deepseek
PYTHONPATH=daemon python3 -m kg_daemon --db "$KG_SERVICE_DB" run --cost-cap 20 --deepseek
```

The collector refreshes deterministic gaps, adds validated query suggestions,
then uses the existing scheduler and fetcher. Model extraction runs only for
ambiguous rule output or relevant documents with open gaps and no usable rules.
A quote must be an exact substring of the sanitized text. A unique quote may have
its offsets repaired; the quote itself is never rewritten. An ambiguous or missing
quote is rejected. Dates and names must still occur in that text. Omitted nulls and
extra keys are ignored, and none of those keys can set publication fields. Missing
or invalid model output leaves deterministic collection available and does not
publish facts. `COLLECT=succeeded` is not AI success: read `ai=`, `reason=`, and
`ai_docs=considered/filtered/called/failed/findings`.

An extraction rejection sets `reason=` to one category: `shape_invalid`,
`too_many_findings`, `label_invalid`, `quote_missing`, `quote_too_long`,
`quote_not_exact`, `offset_ambiguous`, `offset_invalid`, `name_absent`,
`name_invalid`, `date_not_iso`, `date_not_verbatim`, `mentorship_unsupported`,
or `field_invalid`. `ai_request.error` adds a sanitized index list such as
`0:quote_not_exact,quote_len=12,in_text=0`. It does not store the quote, names,
dates, page text, or response body. If some findings pass validation, extraction
keeps those rows without retrying and drops the failed findings. The adapter's
`findings_dropped` counter records the drops, and each retained row's
`model_meta_json.dropped` contains the same safe index tokens. A nonempty response
with no valid findings still rejects and retries as before; an empty findings
list remains valid. Run `749a8bdcf4fe` stored only
`invalid_task_json` because that body was discarded. The extraction instruction
now states the same limits (at most 8 findings, an exact quote, no date
conversion, names copied from the page). Schema version stays `json-2`, so a
cached gap plan is unchanged and the next extract is a new request. One schema
rejection is still retried once and the retry names the category. If that retry
cannot be reserved, the stored category remains and no second call is made.

Default pipeline controls:

| Variable | Default | Effect |
| --- | --- | --- |
| `KG_COLLECT` | `1` | Run web collector and daemon bridge |
| `KG_COLLECT_UNITS` | `60` | Scheduler allowance before run/model budget gates |
| `KG_COLLECT_COST_CAP` | `78` | Web run internal cost cap |
| `KG_DEEPSEEK` | `1` | Pass `--deepseek` to all three callers |
| `KG_AI_DAILY_COST_CAP` | `20` | Shared KST-day model allowance, clamped to 0–20 |
| `KG_IG` | Daily pipeline `1`, heavy and standalone wrapper `0` | Read Instagram with the saved Kitchen Genome session |
| `KG_DAEMON_COST_CAP` | `20` | Local-document daemon cap |
| `KG_SERVICE_DB` | `$KG_HOME/data_store/kg_service.db` (`KG_HOME` defaults to `/workspace/kitchen-genome`) | Shared private service state |

Cost units are internal, not dollar prices: 0.5 units per 1,000 total model tokens.
The client reserves UTF-8 request bytes plus framing allowance plus maximum output
against the caller’s run allowance where configured and the shared day cap before HTTP. When that ceiling is larger
than the room left in either cap, the excerpt is shortened until it fits. A call
that cannot fit even a short excerpt is not sent; the model stops for that run
with `reason=cost_cap` and deterministic stages still finish. Successful responses
settle to actual usage, which releases the unused hold so a later call can reserve.
Malformed task JSON with valid usage is charged, without partial results. Unknown
usage, timeout and process loss keep the reservation. New runs cannot erase it.
A deterministic request key reuses a validated result, including one produced by
the single allowed retry. A previously rejected task is not replayed and does not
pause later documents. Unknown attempts still pause the run. Investigate those
manually; do not delete accounting rows or alter task versions to retry them.
Model/prompt/schema versions belong in audit metadata and must change when the
extraction contract changes.

## Limits and stop behavior

Daily and legacy web collection retain the shared KST-day counts and limits
for candidate URLs, content dispatches and robots queries. Heavy collection
retains its scheduled blocks, traffic and frontier limits.

The shared HTTP guard uses one lock per normalized host, including redirects
and robots retrieval. Only Korean press hosts consult robots.txt and wait at
least five seconds or the longer Crawl-delay. Unavailable robots permission
defers the Korean press request. Other hosts have no fixed request interval.
Authentication walls, challenges, 401, 403, 429 and explicit server blocks stop
the host for the run and any overlapping runs. A valid Retry-After can carry
across runs. Migration 0012 keeps historical block records and requeues stale
Michelin policy skips. Existing Michelin selection URLs can be read as HTTP
sources. Browser-only shells are deferred with `js_renderer_unavailable`.

Web egress prefers zecbot. A confirmed local tunnel failure uses the box route
for the rest of the collector invocation. Origin responses and ambiguous
timeouts stay on the selected route. `egress` and `egress_reason` report route
selection and the advisory exit-IP check.

The daily Instagram reader uses the existing `@kitchen.genome` session under a
single-reader lock. It reads the public restaurant and brand handles in the seed
and checks captions within the latest 30 days. A per-handle watermark marks the
newest post in a completed scan. Interrupted scans retain committed post
checkpoints and findings. The excerpt limit is 40 Unicode characters.
Checkpoint, challenge, 2FA, 429 and access denial stop the reader and exclusively
create `crawler/out/ig_blocked.flag`. Either that flag or the existing
`crawler/out/ig_kg_blocked.flag` prevents all contact. Existing flags are
preserved. A missing session skips the reader. A transient failure or wrapper
timeout stops the invocation and keeps its checkpoint. The timeout summary is
`IG_FAILED account=kitchen.genome reason=timeout`.

## Review and retention

Every model extraction has `asserted_by=ai_inference`, `epistemic=inference`,
`pending_only=true`, model/version/request/source-hash metadata, and a review entry.
All KG Instagram findings, including keyword matches, are `pending_only=true`
and `collector_account=kitchen.genome`. AI Instagram rows additionally carry
`asserted_by=ai_inference`. Model results cannot create/promote targets, resolve
gaps, contribute corroboration, change employment/status/history, or materialize
service claims. Collector-imported AI is never sent to the model a second time.

The inbox is SQLite `review_queue` in the private service DB. Inspect it with
read-only SQL or existing daemon `status`; do not export it into a public PR.
KG Instagram outputs are `crawler/out/ig_kg/YYYYMMDD.json`, outside the legacy
`ig-*.json` importer glob. Output is a private view of the same pending inbox.
Privacy filtering happens before model submission and excerpt persistence. Excerpts
are purged after 30 days from review payloads, output snapshots and IG model-result
caches when the collector is invoked (even outside the network window). Purged
cache rows are marked `expired`: a resurfacing caption loses only that cached
answer, it does not disable model calls for the rest of the run. Keep the collector
scheduled daily for retention cleanup; offline backups require their own retention
policy. Audit identifiers and budget reservations remain.

No review row auto-publishes. Human acceptance must record a separate evidence and
decision trail; do not rewrite model provenance into a source assertion. The legacy
seed apply step rejects pending/model/KG records before grouping, so they cannot
supply a host, date, quote or corroborating vote.

## Daily pipeline and rollback

`scripts/daily_pipeline.sh` keeps the existing publication lifecycle. It takes
an exclusive lock, requires a clean tree, updates main, and uses branch
`crawl/$DAY`. Collection runs in place through `scripts/collect_daily.sh` and
does not call the legacy Instagram account. After apply and the build guards it
stages `data`, `crawler`, `web`, `scripts`, and `.gitignore` (runtime output
under `crawler/out/` and `data_store/` stays ignored). Publishable changes are
committed, force-pushed, squash-merged, and deployed only when `web/` changed
and the Cloudflare token file is present.

The last line on success is one of:

```text
RESULT=NO_CHANGE
RESULT=MERGED_NO_DEPLOY pr=<url>
RESULT=NEEDS_DEPLOY pr=<url>
RESULT=DEPLOYED pr=<url>
```

`MERGED_NO_DEPLOY` means the crawl PR was squash-merged and `web/` did not
change, so the site is not deployed. `NEEDS_DEPLOY` means `web/` changed and
deploy did not run or did not succeed. `DEPLOYED` means `./deploy.sh main`
succeeded. AI and dedicated-account findings are not in that PR: apply drops
them before `seed.json`, `applied.json`, and the site are written. A model-budget
stop does not by itself change `web/`. The cron command stays
`bash scripts/daily_pipeline.sh`. After this lands on main that command no
longer runs the legacy Instagram account. Build guards (`SHOW_VERDICTS`,
`SHOW_NEWSLETTER`, no `noindex`) and `scripts/build_deploy.py` still run. No
second cron is required. Do not point the cron job at a feature branch: the
script checks out main.

Each IG invocation prints one `IG_OK`, `IG_SKIPPED`, `IG_BLOCKED` or `IG_FAILED`
line with `account=kitchen.genome`. The shell prints `COLLECT=...` (including
`ai_docs=considered/filtered/called/failed/findings`) before the final `RESULT`
line. `RESULT=FAILED` is only an unexpected command failure. `RESULT=SKIPPED`
means another run holds the lock. Independent collection failure remains visible
without blocking deterministic build work. Timeout handling marks the IG account
stopped before another run can dispatch.

Disable with `KG_DEEPSEEK=0` and `KG_IG=0`; preserve inboxes, daily counters,
reservations, sessions and block flags. Do not enable the legacy account as a
rollback. Do not commit runtime files, DBs, flags, session copies or credentials.

## Offline verification

```bash
python3 -m pytest -q tests/test_deepseek_client.py tests/test_deepseek_collect.py tests/test_instagram_kg.py tests/test_daily_collect_wiring.py
bash -n scripts/daily_pipeline.sh
```

Fixtures inject provider HTTP, IG HTTP, clocks and subprocesses. They check
usage/reservations, lost responses, concurrent daily budgets, schema/quote
validation, bridge provenance, pending-only isolation, daily network limits,
request spacing, restart quotas, block persistence, optional follows, and the
squash-merge result lines. No real secret paths, accounts, passwords or paid calls are needed.
