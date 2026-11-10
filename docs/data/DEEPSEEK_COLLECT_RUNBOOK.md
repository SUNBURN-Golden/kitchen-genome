# DeepSeek web and Kitchen Genome Instagram collection

Current owner policy (2026-11-10): **no spend; `KG_IG=0`**. Do not run the live
smoke examples below or Argon while this order holds. The current platform
balance is zero; verification uses injected HTTP only. The balance endpoint is
also mocked in tests. `balance_low` / `balance_unavailable` pauses model work
without charging a reservation or failing deterministic stages; `reason=` is
preserved through COLLECT, AI_REVIEW, DAEMON and final RESULT summaries. Review
work awaiting a provider decision remains open with a visible reason.

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
- `KITCHENGENOME_IG_PASSWORD`: used only for the dedicated initial login.
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
PYTHONPATH=daemon python3 crawler/instagram_kg.py --db "$KG_SERVICE_DB" --cost-cap 20 --deepseek
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
dates, page text, or response body. Run `749a8bdcf4fe` stored only
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
| `KG_IG` | `0` | Disabled by default; owner order keeps it off |
| `KG_IG_COST_CAP` | `20` | IG run internal cost cap |
| `KG_DAEMON_COST_CAP` | `20` | Local-document daemon cap |
| `KG_IG_FOLLOW` | `0` | Optional follow gate; read backend cannot follow |
| `KG_SERVICE_DB` | `$KG_HOME/data_store/kg_service.db` (`KG_HOME` defaults to `/workspace/kitchen-genome`) | Shared private service state |

Cost units are internal, not dollar prices: 0.5 units per 1,000 total model tokens.
The client reserves UTF-8 request bytes plus framing allowance plus maximum output
against both the run cap and the day cap before HTTP. When that ceiling is larger
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

Web admission is limited to 500 distinct candidate URLs/day and 200 attempted
content requests/day, including pagination. Candidate URLs are counted when a
request is actually dispatched, not when a feed item is enumerated or a job is
queued, so unfetched links do not consume the quota. robots.txt lookups have a
separate auxiliary counter (200/day) and do not consume content slots. Both web
entry points share durable KST-day counters and a dispatch lock. Depth never
exceeds two. Every domain waits at least five seconds; a longer robots delay
persists across processes. Robots 403/429/503 and challenge responses stop content
dispatch. Domain pauses persist in `web_host_guard` until an operator reviews and
clears the specific pause.

Automatic redirects are declined, preserving the request/robots boundary. Live
browser fallback is disabled because its subrequests are not instrumented by this
guard. Redirect-only and browser-only pages therefore remain collection gaps;
no alternate access is attempted.

Instagram runs only 06:45–08:00 Asia/Seoul; the 09:30–10:30 and 20:30–21:30 blackout
windows are outside that collection window. Request guards cover temporary login
sessions, session validation, profile metadata and lazy pagination. Limits are
25 distinct restaurant/brand handles/day, eight seconds between every dispatch,
six caption inspections per handle/day, and excerpts of at most 40 Unicode
characters. Only public restaurant/brand handles from existing seed records are
eligible; no model-supplied handles or follower harvesting are accepted. Private
profiles are skipped. No media, stories, comments or DMs are collected.

A checkpoint/challenge/429/access denial stops immediately and writes
`crawler/out/ig_kg_blocked.flag`. A transient transport failure (DNS/TLS/socket
reset/timeout) stops the current run without writing the flag or pinning
`stopped_day`, so a same-day rerun may retry; it still never retries in-process or
swaps identity. Partial findings have already been committed to the private inbox.
The account lock, quotas and dispatch time survive restarts. A process killed
during a request leaves a same-day stop marker. A blocked flag survives midnight.
After human account inspection, clearing the flag still does not allow a same-day
retry for an access-control stop.
Neither this collector nor the pipeline falls back to `agamajung`, a proxy or a
new account. `crawler/out/ig_blocked.flag` skips Kitchen Genome Instagram before
any request, same as `crawler/out/ig_kg_blocked.flag`. Neither flag is deleted.
The legacy session file is not read. Outside 06:45–08:00 Asia/Seoul, or without
the saved `kitchen.genome` session, Instagram skips before network. Web collection
still runs.

Following is optional and disabled by default. DeepSeek's validated recommendation
cannot enable it. The bundled read backend reports
`follows_disabled_backend_unavailable` if following is requested. A future injected
follow backend must use the same request guard and account lock; only checked,
public, eligible, not-already-followed handles may be attempted. The durable
10-attempt/day limit consumes uncertain attempts and prevents replay.

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
