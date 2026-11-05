# DeepSeek web and Kitchen Genome Instagram collection

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
- Login: exactly `kitchen.genome`. The authenticated saved session is checked
  against that username. A different/invalid identity stops, without relogin.

`DEEPSEEK_MODEL` defaults to the owner-ordered `deepseek-chat`. An operator can set
`DEEPSEEK_MODEL=deepseek-flash` explicitly. As of 2026-11-05 the
[official quick start](https://api-docs.deepseek.com/) lists Flash and V4 Pro;
there is no silent substitution if the default is unavailable. Requests use
non-thinking JSON output, 1,024 maximum output tokens and a 30-second timeout.
The client rejects redirects and disables proxy inheritance. It has no retries.
The API's [usage contract](https://api-docs.deepseek.com/api/create-chat-completion/)
supplies input/output/total counts; cached input is recorded without counting it twice.

## Run bounded collection

The following commands make live calls **only when an operator runs them**.
They were not run during BUILD or in CI:

```bash
PYTHONPATH=daemon python3 -m kg_collect --db data_store/kg_service.db cycle --units 60 --cost-cap 78 --no-browser --deepseek
PYTHONPATH=daemon python3 crawler/instagram_kg.py --db data_store/kg_service.db --cost-cap 20 --deepseek
PYTHONPATH=daemon python3 -m kg_daemon --db data_store/kg_service.db run --cost-cap 20 --deepseek
```

The collector refreshes deterministic gaps, adds validated query suggestions,
then uses the existing scheduler and fetcher. Model extraction runs only for
ambiguous rule output or relevant documents with open gaps and no usable rules.
It verifies exact quote offsets and never substitutes fetch time for event time.
Missing/invalid model output leaves deterministic collection available.

Default pipeline controls:

| Variable | Default | Effect |
| --- | --- | --- |
| `KG_COLLECT` | `1` | Run web collector and daemon bridge |
| `KG_COLLECT_UNITS` | `60` | Scheduler allowance before run/model budget gates |
| `KG_COLLECT_COST_CAP` | `78` | Web run internal cost cap |
| `KG_DEEPSEEK` | `1` | Pass `--deepseek` to all three callers |
| `KG_AI_DAILY_COST_CAP` | `20` | Shared KST-day model allowance, clamped to 0–20 |
| `KG_IG` | `1` | Run dedicated Instagram read collector |
| `KG_IG_COST_CAP` | `20` | IG run internal cost cap |
| `KG_DAEMON_COST_CAP` | `20` | Local-document daemon cap |
| `KG_IG_FOLLOW` | `0` | Optional follow gate; read backend cannot follow |
| `KG_SERVICE_DB` | repo `data_store/kg_service.db` | Shared private service state |

Cost units are internal, not dollar prices: 0.5 units per 1,000 total model tokens.
The client reserves UTF-8 request bytes plus framing allowance plus maximum output
against both run and day before HTTP. Successful responses settle to actual usage.
Malformed task JSON with valid usage is charged, without partial results. Unknown
usage, timeout and process loss keep the reservation. New runs cannot erase it.
A deterministic request key reuses validated results; failed/unknown attempts do
not replay. Investigate unknown attempts manually; do not delete accounting rows
or alter task versions to retry them. Model/prompt/schema versions belong in audit
metadata and must change when the extraction contract changes.

## Limits and stop behavior

Web admission is limited to 500 distinct candidate URLs/day and 200 attempted
requests/day, including robots and pagination. Both web entry points share durable
KST-day counters and a dispatch lock. Depth never exceeds two. Every domain waits
at least five seconds; a longer robots delay persists across processes. Robots
403/429/503 and challenge responses stop content dispatch. Domain pauses persist in
`web_host_guard` until an operator reviews and clears the specific pause.

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
`crawler/out/ig_kg_blocked.flag`. Unknown transport outcomes stop too. Partial
findings have already been committed to the private inbox. The account lock,
quotas and dispatch time survive restarts. A process killed during a request
leaves a same-day stop marker. A blocked flag survives midnight. After human
account inspection, clearing the flag still does not allow a same-day retry.
Neither this collector nor the pipeline falls back to `agamajung`, a proxy or a
new account. The legacy session and `ig_blocked.flag` remain untouched.

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
caches when the collector is invoked (even outside the network window). Keep the
collector scheduled daily for retention cleanup; offline backups require their own
retention policy. Audit identifiers and budget reservations remain.

No review row auto-publishes. Human acceptance must record a separate evidence and
decision trail; do not rewrite model provenance into a source assertion. The legacy
seed apply step rejects pending/model/KG records before grouping, so they cannot
supply a host, date, quote or corroborating vote.

## Daily pipeline and rollback

`scripts/daily_pipeline.sh` takes an exclusive pipeline lock, requires clean
tracked files, branches from updated main with a unique name, and runs collection,
apply and build guards. It stages only tracked publication artifacts. If there
are publishable changes it pushes the branch and opens a PR against main, then
stops. It never force-pushes, merges or deploys. Private review growth alone does
not create a PR. Squash merge is a later human action.

Each IG invocation prints one `IG_OK`, `IG_SKIPPED`, `IG_BLOCKED` or `IG_FAILED`
line with `account=kitchen.genome`. The shell prints `COLLECT=...`, a daemon exit
summary, and one final `RESULT=NO_CHANGE|PR_CREATED|FAILED|SKIPPED`. Independent
collection failure remains visible without blocking deterministic build work.
Timeout handling marks the IG account stopped before another run can dispatch.

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
request spacing, restart quotas, block persistence, optional follows, and PR-only
shell behavior. No real secret paths, accounts, passwords or paid calls are needed.
