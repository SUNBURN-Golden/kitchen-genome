# Kitchen Genome global genome

Owner decisions, 2026-11-20 14:30–14:37 KST. Build a worldwide chef-career graph, starting with the latest Michelin selections in
3★, 2★, 1★ order. Store the worldwide graph privately and publish its Korea-connected view at 07:00. The 14:37 access policy
supersedes older rules. This design builds on [PR A](collect-scope-expansion.md) and its source, seed, audit and rollback paths.

Ship four ordered PRs. Each can merge with the existing daily job working and can be built and accepted in one build stage.
Use `schema/migrations/` after PR A's `0011`. Keep applied migrations and their checksums intact. All acceptance runs use a
fresh or fixture-upgraded SQLite DB, raw store, seed, flags and locks under a temp directory. Pin clocks, stub shell/network
transports and use fake AI responses. Run builds in the temp fixture checkout. Acceptance uses zero live network calls, paid
runs, production jobs or access to `/workspace/kitchen-genome` and its data store.

## G0 · Minimum access and account policy

- Share one host policy in `kg_collect/net.py:PoliteNet` across `fetch.py:Fetcher`, `heavy.py:HeavyCrawler`, `proxynet.py`,
  and `crawler/crawl.py:network/get`. Normalize hostname case, IDNA and `www.` consistently and honor known host aliases.
  Introduce `sources/registry.yaml:access.korean_press_hosts` plus a `country: KR` marker for Korean entries under
  `sources/discovery_seeds.yaml:publication.press_hosts`. Populate from Korean press/RSS/sitemaps in `sources/*.yaml`, including
  `.com` and `.news` publishers. Match maintained domains and subdomains, or `.kr` (including `.co.kr`) with publisher/press-feed
  classification or a recognizable news subdomain. A bare `.kr`, Korean article text or Korean route does not make a host press.
  For Korean press check robots with the honest Kitchen Genome UA and enforce `max(5 s, robots Crawl-delay)`; use the robots
  cache and defer when permission cannot be determined. Other hosts have zero fixed spacing and no robots lookup. All web
  dispatches use the same honest UA and at most one in-flight request per normalized host, including robots and redirects.
- Add `0012_web_access_guard.sql` with `web_access_run(run_id, expires_at)` and `web_run_block(run_id, host, reason,
  blocked_at)` keyed by run and host. Extend `web_host_guard` with `retry_after_at`, retaining dispatch timestamps for Korean
  press. Use a per-host process lock under the shared service data directory and an in-process busy key. Under the lock, check
  policy, send, classify, then commit any block before releasing. Recheck each redirect target under its own lock after
  releasing the previous one. Both shell entry points and standalone adapters register expiring run scopes. Stamp a new block
  against the current and overlapping live scopes so another process cannot immediately fetch the host. Expired scopes end the
  run-wide stop. Honor a valid `Retry-After` across runs. Keep historical pause records and retire stale policy-only denies
  by explicit reason, including old Michelin manual/robots state. A recorded active block stays effective for its run.
- `403`, `429`, authentication/login walls and captcha/challenge responses stop and record the host everywhere, even on
  HTTP 200 or at a redirect target. Treat `401` and explicit service blocks such as `503` with `Retry-After` the same way.
  Reject state-changing requests and private destinations, including redirect hops. A blocked result reaches the job as
  `fail_blocked` or `fail_login_page`. RSS, articles, robots and fallback transports share the guard. Keep scheduled HTTP-only
  paths. Disable automatic `playwright_browser()` rendering in `fetch.py` while it cannot enforce the guard on subrequests
  (a rendered page fires parallel same-host and third-party subrequests). This is a known capability regression: JS-shell
  hosts in `crawler/config.json:allowed_browser_hosts` (e.g. hotel dining directories) become HTTP-only; an unreadable shell
  is recorded as deferred/unavailable with its reason, never bypassed, and re-enters the queue if a guarded renderer exists.
- Unban `guide.michelin.com` in `crawler/config.json`, `sources/heavy_sources.yaml` and `sources/discovery_seeds.yaml`. Update
  the existing `michelin_manual` registry entry in place to preserve its `source_id`, allow automated selection reads and keep
  manual findings readable. Requeue stale policy-skipped Michelin jobs once. Remove equivalent blanket robots-only exclusions
  where they conflict with the new policy (including the `unsupported_scope` note in `sources/registry.yaml`); a host whose
  robots answers 403 (e.g. `bluer.co.kr`) stays excluded because active blocks are never bypassed. Keep out-of-scope adapters
  such as booking/social subject to their own collection roles. Update `docs/data/SOURCES.md`, the `crawler/crawl.py` and
  `heavy.py` header comments, runbooks and tests for the new rule.
- In `proxynet.py:request/egress_ok`, select zecbot when its local tunnel is healthy. If the tunnel is down, route directly from
  the box. If the local connection/SOCKS handshake fails before reaching the origin, confirm tunnel failure and allow one
  direct dispatch under the same host guard. An origin response or ambiguous upstream timeout never triggers a route change
  (`socks5_connect` already distinguishes a refused CONNECT from an unreachable target). Keep the confirmed direct route for
  the rest of the invocation. The exit-IP equality check becomes informational: `exit_ip_unknown`/`exit_ip_mismatch` never
  skip or halt a run now that the box IP is a permitted route; only tunnel health selects the route, and the periodic
  `heavy.py:run` IP recheck no longer halts. Remove `KG_EGRESS_GATE` and the skip/wait branches in
  `scripts/collect_daily.sh`, `heavy.py:run` and legacy `crawler/crawl.py`. Log `egress=zecbot|box` and a reason code, without
  network credentials or IP details. Leave the zecbot server untouched.
- `scripts/daily_pipeline.sh` defaults `KG_IG=1` and forwards it to `collect_daily.sh`; `heavy_crawl.sh` keeps `KG_IG=0`.
  `crawler/instagram_kg.py:Guard/collect/main` uses only the existing `@kitchen.genome` session, with one reader, public
  restaurant/brand handles, and no password login, follows, other accounts or proxy switching. Remove the 06:45–08:00 window,
  the blackout periods and fixed request-spacing checks, `KG_IG_COST_CAP` and the IG `--cost-cap`. Remove artificial daily
  handle/caption/follow quotas (`CAP/POSTS/FOLLOWS`); keep per-handle dedupe, the existing 30-day scope/retention and the
  bounded shell invocation, and **add** a per-handle last-seen post watermark (none exists today) so quota-free caption reads
  stop at the previous run's newest post instead of re-reading full history. Vendor the `KW` keyword lexicon into
  `instagram_kg.py` (today imported from `crawler/instagram.py`) so the legacy module is never imported or invoked; legacy
  `crawler/instagram.py`, the agamajung account and `tests/test_instagram.py` stay frozen and untouched.
  Set the honest crawler UA at `LoaderBackend` dispatch. Update `registry.py:validate_registry` to require safeguards on
  `instagram_kitchen_genome` and disable the `instagram_public` entry (its adapter is `instagram_py`; it is paused today, not
  banned). Check `kg_daemon.registry.IG_FLAG` (`crawler/out/ig_blocked.flag`) and the existing `ig_kg_blocked.flag` before
  contact. On checkpoint/challenge/2FA/429 or access failure, stop immediately, create the flag exclusively (`Guard.block`
  today overwrites via `atomic_json`) and preserve either existing flag byte for byte. Missing session skips. Ordinary
  transient transport failures and wrapper timeouts stop the invocation and keep its checkpoint **without writing a block
  flag**: change the `collect_daily.sh` timeout path and `--mark-timeout` accordingly (today they call `guard.block('timeout')`);
  only provider block signals write the flag. IG excerpts stay private and pending. Never invoke agamajung.
- **Acceptance.** Replace superseded cases in `test_source_registry.py`, `test_heavy_crawler.py`, `test_collector_v2.py`,
  `test_daily_collect_wiring.py`, and `test_instagram_kg.py`. Fixtures cover Korean `.co.kr` and `.com` press, a Korean venue,
  Michelin and Wikipedia, exact host matching, Korean disallow and longer delay, and zero robots fetches for other hosts.
  Fake concurrent callers exercise locks and block propagation through redirects and login/captcha bodies. Fake a tunnel outage
  before and during a block and assert that a host refusal never retries via direct transport, and that an exit-IP mismatch
  with a healthy tunnel neither skips nor halts. Test saved-session reads outside
  the old window, a flag from either account, first 429/2FA, immutable existing flags, wrapper timeouts that stop without
  writing a flag, watermark-resumed caption reads, and zero legacy-account calls or `crawler/instagram.py` imports.

## G1 · Remove volume gates, meter work, and extend the ontology

| Existing limit and callers | New configuration and behavior |
| --- | --- |
| `KG_AI_DAILY_COST_CAP`, `KG_AI_DAILY_COST_CAP_OVERRIDE`, 20-unit clamp and `room_tokens()` gates in `kg_ai/budget.py`, `review.py`, `kg_collect/deepseek.py`, `kg_daemon/deepseek.py` and IG | Remove both env keys and all spend-admission logic. Keep `ai_request` atomic request registration/dedup, actual usage settlement, unknown-attempt audit and request-size validation. Batch by per-request context/output size. `recheck.admit/hold_review` loses its 15k room floor and daily admission count; the recheck workload's 8-unit/40% share lives in `budget.py:room_tokens/reserve` and disappears with the daily cap. Pending review is normal durable work. |
| `KG_COLLECT_COST_CAP`, `KG_DAEMON_COST_CAP`, `KG_COLLECT_UNITS`, `--units`, `--cost-cap`; pool shares and model headroom in `cycle.stage_plan/stage_fetch`, `scheduler.plan`, collector/service CLIs, `runner.CostMeter/Daemon`, shell callers | Remove those keys/options from scheduled entry points with no replacement. `CostMeter.charge` only records. Write `0` as the compatibility value in `daemon_run.cost_cap_units`, retain old rows/statuses, and resume old `paused_cost_cap` runs. Select by priority, age and due time with work-conserving rotation. |
| `limits.py:Limits` at 200 fetches, 500 candidate URLs and 200 robots fetches per KST day; `net.py`, `fetch.py`, `crawl.py`, `cycle.py` | Remove `DailyLimit` and quota checks. Keep `web_daily_url` and `web_daily_fetch` counters for reporting/dedup. No new daily web quota. |
| `discovery_seeds.yaml:caps.max_queued_per_pool`, `keyword_search.daily_allowances` (8/8/8), `discover.QuerySlots/claim_query`, planner slices, per-parent link/query counts and fixed crawl depth | Remove admission ceilings and `400/480/200` defaults. Remove `pagination_max_pages`, `max_official_subpages`, `max_followups_per_extraction` and `max_official_probes_per_cycle`. Reuse `0011` history for query dedup and rotation. Page through due work until empty or deadline. Traverse unseen canonical URLs on scoped source/parent relations, with cursor repeat detection. Keep relevance/type filters and revisit/freshness rules. PR A’s three-alumni goal remains a priority signal for fading out redundant mother-kitchen queries. |
| `heavy_sources.yaml:caps.daily_candidates/daily_content/daily_requests/daily_bytes/monthly_bytes`, `max_queries/max_links_per_page/max_depth`, heavy `CEIL/HARD` clamps and `--max-requests` | Remove volume and traffic ceilings, including the 10 GB monthly proxy allowance. Traverse all relevant feed items and page the due-host selection in `pick()` past blocked/busy hosts. Keep request/byte counters for reporting. Set `backlog_for_optional_block: 1` due work item (was 800). Use `max_concurrency: 6` as a cross-host executor size, without a hard-coded maximum. |
| `site_publish.py:CAP=12` and `apply_ready()` daily slots | Remove the count and config entirely. Apply every eligible row in the existing atomic seed update. Keep KST timestamps, stable IDs, audit events, inherited daily lock, rollback tombstones and edit detection. |

- Retain wall-clock deadlines, request timeouts, per-response parsing/body bounds, source-specific freshness, dedup and
  access stops. Per-provider context and service errors govern individual requests. No daily spend or throughput gate.
  Ship at full speed as each lane enables. Record actual/estimated transport cost even when the path is direct or a cap is
  absent. Report proxy wire traffic because it shares the existing zecbot allowance with other workloads.
- Add `kg_collect/guard.py` and share it with `kg_daemon/runner.py` and `kg_ai/deepseek.py`. Operational defaults are
  `KG_GUARD_IDENTICAL_FAILURES=5` consecutive identical normalized failure signatures within the same adapter/stage,
  `KG_GUARD_NO_PROGRESS=10` runnable passes with the same cursor/state fingerprint and no durable change, and
  `KG_GUARD_MAX_ATTEMPTS=3` per transient job and document/version. Respect existing tighter provider correction bounds.
  Stop a faulty lane, persist a reason and the last committed item, and let independent lanes continue. Idle, delayed and
  host-blocked work yields normally. Attempt exhaustion quarantines that item for review. A retry, a refetch and a lease
  takeover cannot reset the same failure's attempt history. Success counts include meaningful durable transitions.
- Keep `kg_ai/deepseek.py:Client` on the existing Token Plan route and credential with today's working `QWEN_MODEL`
  (`qwen4-max-preview`) as the default, exposed as config (`KG_QWEN_MODEL`); switch to a stable `qwen4-max` ID only after
  confirming that ID is served on this route — provider model availability is not offline-testable, so the build stage must
  not hard-switch it. Keep the existing DeepSeek-to-Qwen chain on eligible provider/balance failures, cache and one
  correction for malformed responses. A valid Qwen configuration also runs when the DeepSeek key is absent: today
  `Client.status` returns `disabled` and `cycle.run_cycle` reports `ai_reason=missing_key` without a DeepSeek key, so both
  must treat a configured Qwen fallback as enabled. With near-zero DeepSeek balance Qwen supplies the actual work. Thinking
  stays disabled for structured extraction (already implemented for both providers). Store provider, model, prompt/response/cache tokens,
  result state and request/run IDs. Store unknown usage explicitly, including ambiguous transport results, and retain
  per-task token bounds. Expectations from the old spend reservation never deny a valid next request.
  [Qwen4-Max model documentation](https://docs.modelstudio.console.alibabacloud.com/en/model-studio/qwen4-max)
- At 07:00, aggregate usage for the previous closed KST day across heavy and daily runs. In `scoreboard.py`, `gold.py`,
  `scripts/collect_daily.sh` and `scripts/daily_pipeline.sh`, report provider/model cost from actual tokens and versioned
  rate estimates, cache hits, missing usage, successful HTML docs, proxy traffic, and distinct newly resolved chef IDs
  and verified career-link IDs first reached that day. Report total model cost divided by each count separately, with
  `n/a` when zero and `unknown` for unpriced usage. Record first transitions by canonical chef and career-link IDs so
  refreshes, replay and alias merges cannot add to the counts. Start with the existing collector/publication records and
  use the G3 assertion history once available. Render per-language and overall gold precision, recall, F1, quote fidelity
  and false merges on versioned human-labelled data. Score the Korean rules on the existing gold set and score multilingual
  output from labelled, held-out copies of normal article jobs, including prefilter misses. Use saved actual responses
  and report the version, sample size and missing model sample as `n/a`. Fake model responses exercise the metric offline.
  Neither metric admits work.
  Keep reports redacted and out of commits except the existing sanitized summary fields. Never log session material, keys,
  credential-bearing URLs, provider bodies or raw prompts. No new accounts or paid search APIs.
- `0013_global_ontology.sql` adds `branch.country_code` (ISO alpha-2, nullable for legacy unknown), with existing `branch.city`,
  and source-backed `chef` location claims (`country_code`, `city`, validity and basis through `claim`/`evidence`). Chef location
  can be derived with provenance from an evidenced current employment. It never implies nationality. Add a stable target-to-entity
  mapping to chef/restaurant/branch IDs. Add `guide_selection(selection_id, scope, country_code, edition_key, guide_year,
  observed_at)` and append-only `guide_listing(selection_id, branch_id, stars, source_kind, source_url, fetch_id, locator,
  observed_at)` with stars 1–3 and tier derived from stars. `guide_year` can be null for a live list with no labelled edition.
  Keep observation time separate. Uniqueness by selection, branch, source and observation fingerprint makes list replays
  idempotent. Choose a current list fact at read time. Seed migration fills country only where an existing
  location or curated mapping supports it. `targets.load_seed_targets`, `seed_migration.py`, and `ontology.py` retain IDs.
- `site_publish.py:Corpus/prepare/apply_ready` and `kg_ai/review.py` retire PR A’s second AI bundle approval required for
  checking-gate bundles (verified-gate bundles already auto-approve in `prepare`) and the single-official-source verified
  shortcut in `site_publish.verified()`. For in-scope rows with resolved identities and sourced locations, a source-attributed
  single or unverified assertion is ready as `lineage_status=unverified` with `career_checking` on affected fields. It may carry
  the source link if the raw snapshot or quote check is missing, without an excerpt or asserted structured relation. Two
  independent sources with valid exact quotes on the **same** edge give `출처 확인`. Check role and period separately. Use
  `claims.independent_sources()` and PR A’s canonical URL, publisher group and repost checks. An official bio counts as one
  source. Keep PR A’s reliable-source and subject-ownership checks for verification. Conflicts/homonyms go to review.
  A model inference with no verbatim source quote stays private. An AI review can never create source evidence. Update
  the 07:00 PR description and collector runbook to match.
- **Acceptance.** Replace stale cap cases in `test_ai_review.py`, `test_collect_bot_units.py`, `test_deepseek_collect.py`,
  `test_deepseek_client.py`, `test_monthly_recheck.py`, `test_daemon.py`, `test_site_publication.py`, `test_qwen_fallback.py`
  and shell wiring tests. Legacy `crawler/worker.py`/`vault.py` quotas are unscheduled and stay frozen. With fake time settle
  >20 AI units, >200 content fetches, >200 robots requests, >500 distinct candidates and >12 additions on one KST day.
  Cross the old queue, RSS and traffic thresholds and assert that work still flows. Test terminal/retry loops versus real
  progress, cap-paused upgrade, cost aggregation with
  zero denominators and unknown usage, Qwen fallback, Qwen-only operation without a DeepSeek key (enabled status, no
  `missing_key` summary), cache, and `0013` on both a clean and a PR A fixture DB. A single
  supported edge publishes as checking without AI; corroboration upgrades it in place and rollback survives.

## G2 · Michelin lists, tier order and resumable heavy work

- Add `sources/michelin_selections.yaml` and `kg_collect/michelin.py`. Start from the public Michelin country/selection
  directory and maintain country, city/regional scope and locale mappings there, including overlapping multi-city guides.
  Enumerate all Michelin-covered countries and record scopes with no list/coverage separately. Follow the public selection
  pages, tier filters, HTML pagination and linked venue cards. Parse country, city/address, stars, venue URL and explicit
  guide edition/year, with stored fetch/page locator. Ignore Green Stars, Bib and recommendation-only listings. Canonicalize
  locale mirrors and dedupe by Michelin listing ID or canonical venue path, then verified venue + branch location.
  Exempt `guide.michelin.com` selection/list pagination and tier-filter URLs from `crawler/config.json:trapped_url_patterns`
  (`page=`, `filter=`, `sort=` are trapped today and `heavy.py:excluded()` would drop every tier page); keep the traps for
  all other hosts.
  [Global Michelin directory](https://guide.michelin.com/en/restaurants) ·
  [South Korea selection](https://guide.michelin.com/kr/en/selection/south-korea/restaurants)
- In `michelin.py`, cross-check per-country and per-city Wikipedia lists using MediaWiki `action=query&prop=revisions`
  with the main slot's wikitext, revision IDs, and redirects. Parse known table/template families and explicit year columns,
  including rowspans and local star notation. Unknown structure is a parse review, never a zero count. Cross-check with annual
  guide-announcement coverage found via publisher RSS/site links and existing free RSS search. Distinguish original reporting
  from mirrored releases. Store each observation with edition, URL and revision/row locator. The official selection leads;
  when it is unavailable retain edition-matched press/wiki rows as provisional roster leads, with source labels. A differing
  same-edition tier becomes a list discrepancy. A new edition can change the tier without rewriting history. Absence on a
  partial page, a changed count or a blocked host never removes an award. Keep conditional fetch metadata and page hashes.
  [MediaWiki revisions API](https://www.mediawiki.org/wiki/API:Revisions) ·
  [Example edition announcement](https://guide.michelin.com/en/article/michelin-guide-ceremony/all-the-stars-michelin-guide-seoul-busan-2026)
- Add `0014_global_queue.sql` and `kg_collect/global_queue.py`. `global_work` carries a stable job key, snapshot/sweep ID,
  tier, kind (`list`, `crosscheck`, `venue`, `discover`, `article`, `extract`), scope/target or fetch ID, cursor, priority,
  `state`, `available_at`, lease token/expiry, attempts and reason. A join records all restaurant/tier origins for a shared
  article or query. `global_tier_pass` records scope pagination completion, disposition and tier pass state. Reuse existing
  `fetch_record`, `fetch_priority`, `rawstore` and `crawl_job` where they already own fetch identity. Reuse existing queued
  bytes when possible. Add eligible list targets and launch discoveries immediately from 3★ cards. Use venue/chef names,
  city, local-language modifiers and official bios, interviews, reputable press and source-linked sources. Snowball to
  cited employers and aliases, inheriting the initiating tier. Discovery of a roster entry alone creates no career fact.
- Drain the 3★ first-pass frontier worldwide before starting 2★, then 1★. Finish a tier's first pass when every known
  country selection and venue has been attempted and due first-pass frontiers are drained, or recorded as unavailable/
  deferred for access, missing source, retry or review. Record coverage denominators and let deferred tails resume with their
  originating priority. Advance an explicit SQLite tier cursor so an unresolved homonym or blocked country cannot strand
  the world. Deduplicate source links and query variants and record completed scans by content/version. Prioritize remaining
  higher-tier and newly promoted venues when they become due. Dated changes use the new tier without restarting a sweep.
- The queue uses ordered claims with short SQLite transactions and expiry. Give workers their own SQLite connections and a
  bounded busy timeout. Release writes during HTTP/AI calls. Store each page/result and its continuations before acknowledging
  work. Use stable IDs and idempotent upserts for any raw record reused after a crash. Record a cursor at page/job granularity
  and release or reclaim expired leases on restart.
  Ignore late acknowledgments from an old lease. AI request registration also dedupes overlapping heavy and daily consumers.
  Cache and reconcile settled results after a crash. An interrupted in-flight call with unknown billing stays visible for
  reconciliation and is not replayed blindly. Keep independent provider and host work moving.
- Wire `heavy.py:run` and `scripts/heavy_crawl.sh` to drain the global queue during **00:00–02:59** and **11:00–13:59**
  KST and the existing optional **22:00–23:59** block when due work exists. Leave `scripts/heavy_crawl.crontab` unchanged.
  Update the now-stale "HTTP only / no LLM / never calls DeepSeek" docstrings in `heavy.py` and `heavy_crawl.sh`: heavy
  extraction uses the same Qwen-first chain, `ai_request` registration and guard as the daily lane.
  Fetch concurrently across eligible hosts. G3 drains extraction continuously in the same block through a shared worker
  entry so HTTP and model calls overlap. G2 starts roster collection and
  snapshots through the existing extractor where eligible; foreign extraction jobs wait for the G3 extractor version.
  The heavy service writes only private DB/raw state, without publishing, building or using Instagram. Stop dispatch with time
  for bounded in-flight completion before the existing cron timeout. SIGTERM commits progress or leaves a reclaimable lease.
- At **07:00 KST**, keep the current `daily_pipeline.sh` and `collect_daily.sh` flow and `KG_COLLECT_TIMEOUT=1680` (360s reserved for source scoring after extract).
  Use a monotonic soft deadline inside `cycle.run_cycle` to yield before the shell timeout, reserving room for local integration.
  Run Korea refresh, due edition/link deltas and new announcement leads, then process available extraction jobs by relevance
  and urgency. Make any unused time available to due worldwide work. Publish from the latest committed graph in the existing
  apply/build steps even after an interrupted collector. Refresh selection pages conditionally on a staggered weekly basis
  and mark an affected edition due immediately on a documented announcement or promotion. Track the last successful scope
  snapshot and use changed pages/venues to generate follow-ups. The 07:00 job handles due changes rather than full sweeps.
- **Acceptance.** Fixture Michelin selections, paginated star tiers, regional overlaps/branches, localized Wikipedia wikitext
  with two years, and press announcements. Assert that `page=`/`filter=` selection URLs survive the trap exemption while
  traps still apply to other hosts, official precedence, conflicts/provisional labels, missing-year and
  partial-result behavior, explicit `3→2→1` first-pass ordering, and eventual advance past a blocked scope. Simulate all
  existing windows, lease expiry after each write boundary, concurrent daily/heavy claims, conditional 304s and restart in
  the middle of a country. Check that a large due backlog neither starves ready hosts nor gets dropped at a SQL batch boundary.

## G3 · Multilingual career evidence and the Korean projection

Two separable workstreams land in order within this one build stage: (a) extraction and identity, then (b) public projection
and rendering. (b) depends only on G0–G2 and the existing Korean graph, so it is implemented and accepted against fixture
edges independently of (a); a stall in one workstream never blocks acceptance of the other.

### Extraction and identity

- `kg_collect/extract.py:process_doc`, `cycle.stage_extract` and the heavy worker share `kg_collect/lineage.py`. Resolve
  language per document and paragraph from trusted `lang` hints, Unicode script and offline language identification.
  Keep `und` for uncertain/mixed language and use a loose culinary/entity filter there. Add versioned cheap cue lexicons
  (`sources/lineage_languages.yaml`) for ko, en, fr, ja, es, it, de and extend for other coverage such as zh/pt. Examples
  are `수셰프/출신`, `worked at/trained under`, `a travaillé chez/chef de partie`, `勤務/修業`, `trabajó en`,
  `ha lavorato presso`, `arbeitete bei`. Route strong venue/name context or first-party biographies even without a cue.
  Use cues to rank evidence windows and avoid low-signal model calls. Score cue misses on the held-out gold set.
  Keep existing Korean rules and use Qwen4-Max for multilingual and difficult Korean extraction. Expand PR A's
  publisher-group and scope registry to foreign press. Store attributed claims from newly encountered publishers as
  checking and count them toward verification once their coverage and independence are established.
- Call `kg_collect/deepseek.py:Adapters` and `kg_ai/article.py`/`Client` with versioned tasks, bounded paragraph windows and
  stable content hashes. Four concurrent model workers by default (`heavy_sources.yaml caps.model_workers: 4`), independent
  of the single-request-per-web-host rule. Implementation decision (G3 review, 2026-11-20): the default is a shared bounded
  slot pool (`HeavyCrawler.model_slots`) gating extraction across the heavy executor's drainers on the shared cross-host
  executor, so model calls overlap web fetches without a separate model-only pool; the daily `cycle.stage_extract` lane
  stays sequential for cost fairness. Preserve source spellings and the original quote language. One assertion contains
  `{chef_text, venue_text, relation, role_text, from_text, to_text, period_precision, quote, source_span}`. Relations separate
  explicit current employment, former employment, training/stage and opening. Dates retain stated precision or null.
  A former stint with no end date uses `valid_to_kind=unknown`; a source explicitly saying the chef currently works there
  can set `open` with `last_confirmed_at`. Keep overlapping assignments where supported; neither a publication date nor a
  chef’s next job closes the previous tenure. Pop-ups, collaboration, awards and adjacent names do not prove employment.
- Add `0015_lineage_evidence.sql` for `career_assertion` with extraction/fetch/evidence locators, canonical chef and branch
  refs when resolved, language, normalized role/period, assertion/review state and content/quote keys. Add a canonical entity
  redirect table for audited merges, preserving old IDs and links. Store `fetch_id, canonical_url, publisher_group,
  published_at, content_hash, span_start/end, quote, extractor_version, model/request_id` for proof. Code locates the exact
  contiguous ≤200-char quote in stored normalized publisher text, maps the window offset, and checks it binds the named
  chef, venue and supported role/date in context. Check fields separately so uncertain timing does not suppress a supported
  stint. Translation is never quote evidence. Separate source assertions from raw model output. Quote mismatches,
  uncertain referents and unsupported model output remain private review leads.
  Use a dedicated verified-span path through `kg_daemon/collect_bridge.py` and `ontology.add_employment()`; the existing
  pending filter in `kg_ai/__init__.py` continues to protect legacy ingestion from raw model/social findings. Keep immutable
  evidence and source claims in the service ontology. Derive public verification per claim/field using G1’s rule while
  preserving the daemon’s source-claim status for bot-written facts. Collapse
  translated syndication with publisher attribution/wire credit plus canonical and content signals. Wikipedia/guide list
  rows seed venues and awards. A roster alone never corroborates a chef's career.
- `targets.py:Gazetteer/norm` and `resolve.py` preserve Unicode display forms and index language-tagged aliases; use NFC
  for stored forms and NFKC/casefold, punctuation/diacritics folding and script-aware tokenization for match keys.
  Generate romanization, surname-order and kana/Hangul variants only as candidates. Merge `Ryu Tae-hwan`, `류태환` and
  `リュ・テファン` when bilingual evidence links them or independent evidence uniquely joins the same branch, role/period
  and compatible career history. Keep aliases non-unique across people. Check city/branch, overlapping or contradictory
  biographies and dates before merging; queue ambiguous candidates with both IDs and source locators. For restaurants,
  prefer listing IDs or verified branch, address and official links; common names and brand membership leave branches distinct.
  Merges are idempotent, reviewed on contradiction and correctable without changing already published URLs.

### Public rendering

- Keep `data/seed.json` as the build input and curated override, with generated rows for **only the displayed subset**.
  Extend `site_publish.apply_ready` and a `site_projection.py` helper to materialize from a consistent committed service
  snapshot under the existing daily lock. Preserve curated data and all current IDs, publication audit markers and
  rollback tombstones. Generated rows use a stable branch/site-ID mapping, with the existing `/r/` and `/k/` namespaces
  and hash routes. If a current kitchen and restaurant represent the same branch, keep both existing URL IDs and link
  them to the shared entity. No DB requirement for reproducible builds from committed seed. A missing/incomplete DB
  snapshot leaves the last successful projection usable. Build updates after the atomic seed write.
- Seed traversal with all sourced Korean branches and explicitly preserve currently public overseas venues
  (`atomix`, `atoboy`, `jua`, `moono`, `lysee`, `odre`). Project backwards from a chef’s sourced destination affiliation
  through their earlier work/training, with stated sequence or dates. Recurse via earlier chef careers at origin venues
  when evidence links those tenures to the preceding path. Prefer temporal overlap with the connecting stint or an explicit
  training/lineage statement. Use visited branch/chef/tenure keys, with no fixed hop count. Include every evidenced origin
  on these paths; skip unrelated alumni, downstream foreign restaurants, same-brand branches and mere co-employment.
  A single-source, quote-backed edge can carry a path with `확인 중` on that edge. A source-attributed claim awaiting a quote
  check can appear as a checking item on an eligible entry, without opening an unsupported chain. Leave foreign data without
  a qualifying path in SQLite for the future English site. Keep existing English pages on the same subset.
- A projected origin branch becomes `kitchens[]` when it has a displayed alum. Keep all 13 current kitchen IDs and their
  existing pages, including those with no alum yet. Extend public restaurant rows with `country`, city, sourced stars/year
  and `origins[]` entries `{kitchen_id, chef_id, chef_name, role, period, lineage_status, career_checking, evidence}`.
  Each evidence item uses the existing escaped `{kind, src, quote}` shape. Keep scalar `kitchen` as the deterministic primary
  origin for compatibility, preferring the existing link and then strongest evidence. Multiple chefs/origins get their own
  edges. Use `type` and an empty origins list for a standalone restaurant. Make both topology and counts use deduped IDs.
- Adapt `data/enrich_en.py`, `web/app.js`, `scripts/build_pages.py`, `build_pages_en.py`, `lineage_evidence.py` and
  `build_feeds.py` to the same public fields. Keep the Korean UI and wording, with the existing `career_checking` badge and
  `출처 확인` for corroborated edges. Mark checking claims across graph, detail and feeds, and omit unverified `alumniOf` or
  `worksFor` from JSON-LD. Keep `const SHOW_VERDICTS=false;` and `const SHOW_NEWSLETTER=false;`, and no `noindex`.
  Use source spellings or sourced Korean names, and language-appropriate fallback without overwriting sourced aliases.
  `web/data.js`, both locales' static pages, feeds/sitemap and deployment manifest consume only the public projection.
  Strip private publication metadata and model/ledger fields. Reconcile the generated route inventory so a removed generated
  page does not remain in the build, while keeping existing routes and curated paths.
- **Acceptance.** Feed fixture bios/interviews in ko/en/fr/ja/es/it/de through fake Qwen and the legacy filter. Exercise an
  allowed single source, same-publisher translation, independent upgrade, unsupported paraphrase, ambiguous Ryu homonym,
  false-role/date, multi-job and changed-version restarts. Assert identical evidence/IDs on daily versus heavy replays.
  Build a Seoul→Noma→earlier-kitchen chain with a cycle, an unrelated foreign group and an Atomix fixture. Assert all
  qualifying links, stable legacy URLs, correct badges and structured data, unrelated nodes absent from assets and routes,
  and no change to `web/data.js` size when thousands of unrelated rows are added to the private fixture DB.

## Sizing and first-pass estimates

- Michelin's global directory showed **161 three-star, 549 two-star and 3,204 one-star restaurants** on 2026-11-20, about
  3,914 total. This is a live snapshot across editions and guide coverage areas. Country-list counts will move with releases.
  [Michelin global directory](https://guide.michelin.com/en/restaurants)
- Plan on roughly **1,200–3,000 unique readable article/bio docs per day** across the two normal heavy blocks and incrementals,
  with about **400–1,000 AI-processed docs/day** after a 35% cue/identity pass rate. Six hours, four model workers, 40–75 s
  per document and ~45% usable worker time imply ~520–970 model docs/day. HTTP/source discovery may supply fewer; the upper
  end assumes diverse reachable hosts, usable free search/RSS and conditional-cache hits. The optional block is upside.
- Assume 2k–6k input and 300–900 output tokens per AI document across its windows. At 3.5k/550 and reference prices of
  USD $1.50/$4.50 per million, cost is about **$0.0077/document**, or **$0.0089** with 15% extra work for correction and
  review. With 0.35 newly resolved chef IDs and 0.30 newly verified career links per model document, that is about
  **$0.025/new chef** or **$0.030/verified link**. These ratios use separate denominators and are not additive. These are
  undiscounted API-equivalent estimates. The existing Token Plan bills in credits and realized cost/yields vary with cache,
  provider plan, document language and corroboration. [Alibaba Cloud reference pricing](https://docs.modelstudio.console.alibabacloud.com/en/model-studio/qwen4-max)

| Tier | First pass at ~140–350 venues/day | Cumulative from start |
| --- | --- | --- |
| 3★ (~160) | ~1–2 days | ~1–2 days |
| 2★ (~550) | ~2–4 more days | ~3–6 days |
| 1★ (~3,200) | ~10–23 more days | ~13–29 days |

The rate assumes eight unique readable docs per venue on average, about 2.8 model docs per venue, and concurrent list discovery.
First-pass coverage measures roster and documented search attempts, with readable evidence, pending and inaccessible counts
reported separately. It gives no guarantee of verified careers. A dominant host block, weak sources or slower model responses
can extend a tier. The first 07:00 publish can show eligible checking links while coverage and corroboration accumulate.
