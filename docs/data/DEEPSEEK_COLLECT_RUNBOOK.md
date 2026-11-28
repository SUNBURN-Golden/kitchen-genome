# DeepSeek web and Kitchen Genome Instagram collection

The November 20 access policy enables saved-session Instagram in the daily
pipeline and selects web egress by local tunnel health. Offline verification uses
injected HTTP and balance responses. `balance_low` and `balance_unavailable`
select Qwen when configured. Deterministic work continues during provider pauses. The reason is carried through COLLECT, AI_REVIEW, DAEMON and
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
private service DB. Due work enters the same rotating scheduler as other
collection. Changed-state candidates, including rules, use the common review
path. Deferred or partial inspections preserve the last completed check.
A full inspection with no changed-state candidates records “no change observed.”

Publisher-attributed lineage with clear identities and a sourced destination is
eligible for publication as 확인 중. Two independent sources with verbatim quotes
on each edge upgrade the entry in place to 출처 확인. An official biography counts
as one source. Role and date use their own evidence. Conflicts go to review.
The seed update, publication audit and rollback tombstones keep the stable row ID.

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
the deterministic crawler can use its ignored local DB. The shared `KG_SERVICE_DB`
path combines request accounting, access state and outcome history.

Supply credentials through the process environment or the operator's secret
manager. Never pass their values as CLI arguments or enable shell tracing:

- `DEEPSEEK_API_KEY`: primary model credential. A configured Qwen Token Plan credential also supports Qwen-only runs.
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
unmerged branch. The daily pipeline calls `collect_daily.sh` for in-place
collection with `--no-browser`. `KG_DEEPSEEK` enables the model chain. Web
collection also runs when Instagram is skipped. Direct equivalents:

```bash
PYTHONPATH=daemon python3 -m kg_collect --db "$KG_SERVICE_DB" cycle --no-browser --deepseek
PYTHONPATH=daemon python3 crawler/instagram_kg.py --db "$KG_SERVICE_DB" --deepseek
PYTHONPATH=daemon python3 -m kg_daemon --db "$KG_SERVICE_DB" run --deepseek
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
rejection gets one correction that names the category.

| Variable | Default | Effect |
| --- | --- | --- |
| `KG_COLLECT` | `1` | Run web collector and daemon bridge |
| `KG_COLLECT_TIMEOUT` | `1680` | Shell timeout in seconds (120s finalize + 360s source scoring + 1200s extract). The collector leaves time for scoring and finalization. |
| `KG_DEEPSEEK` | `1` | Enable the configured model chain |
| `KG_QWEN_MODEL` | `qwen4-max-preview` | Model ID on the existing Qwen Token Plan route |
| `KG_GUARD_IDENTICAL_FAILURES` | `5` | Consecutive identical failures in one lane |
| `KG_GUARD_NO_PROGRESS` | `10` | Runnable passes with unchanged cursor or state |
| `KG_GUARD_MAX_ATTEMPTS` | `3` | Transient attempts per job or document version |
| `KG_INSTAGRAM_ENABLED` | `1` | Master switch. Set to `0` to skip collection, login, and session load. |
| `KG_IG` | Daily pipeline `1`, heavy and standalone wrapper `0` | Read Instagram with the saved Kitchen Genome session. Ignored while the master switch is off. |
| `KG_SERVICE_DB` | `$KG_HOME/data_store/kg_service.db` | Shared private service state |

The scheduler ranks due work by priority, age and query history and rotates
across work pools. HTTP dispatch and byte counters are for reporting. Heavy
collection keeps its scheduled blocks and uses six cross-host workers by
default. Its optional block runs with at least one due item. Request timeouts,
body bounds, freshness, deduplication and host access stops still apply.

AI requests are registered atomically by request key. Settled requests record
actual provider tokens and cache usage. Ambiguous responses retain an unknown
usage row. Request context and output bounds determine batching, and cached
validated results can be reused. A faulted lane records its reason and last
committed item. Exhausted items go to review. Delayed and host-blocked jobs
remain available under their normal policies.

The daily `COLLECT_METRICS` line rolls up the previous closed KST day. It
includes HTML documents, proxy and direct traffic, provider/model usage and
versioned cost estimates, followed by per-chef and per-career-link cost using
first transitions. Zero denominators show `n/a`. Unpriced or missing usage
shows `unknown`. The Korean rule gold set reports precision, recall, F1,
quote fidelity and false merges. A private
`gold/gold_heldout_articles.json` beside the DB can supply a version and an
`articles` array with `fetch_id`, `language`, labelled `expect` and the
sanitized source `text`. The scorer reads saved model output and includes
prefilter misses. An absent model sample is `n/a`. Reports affect no work
selection or publication.

## Access and stop behavior

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

The daily Instagram reader is on by default (`KG_INSTAGRAM_ENABLED=1`) and uses the existing `@kitchen.genome` session under a
single-reader lock. Set the flag to `0` to skip the reader. It reads the public restaurant and brand handles in the seed
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
policy. Request IDs and usage audit rows remain.

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
them before `seed.json`, `applied.json`, and the site are written. The cron command stays
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
usage accounting, lost responses, concurrent request registration, quote
validation, bridge provenance, publication, circuit breakers, request spacing,
block persistence, Instagram checkpointing and squash-merge result lines. No real secret paths, accounts, passwords or paid calls are needed.
