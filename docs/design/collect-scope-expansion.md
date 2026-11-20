# Kitchen Genome collector scope expansion

Owner direction, 2026-11-20. Grow from roughly 50 to 150+ distinct named chefs in two weeks of daily publication, with at least
one alumni entry for each of the 13 mother kitchens. Prioritize Seoul restaurants and retain source quotes and review. The
live-DB diagnosis in `devpipe-runs/diagnosis.md` records 52 restaurants, 98 career links, four empty kitchens, and no additions
to the site seed since October 20.

The existing 07:00 KST `scripts/daily_pipeline.sh` remains the entry point. It runs backlog review, the legacy crawler,
`kg_collect cycle`, the service daemon, fresh review, and `crawler/apply.py` before the build and daily PR. Add the seed
publication path first so the existing corpus can start producing pages. Then improve source and fetch yield. Both PRs use the
existing routine and schedule.

## PR A · Publish new entities, expand queries, and backfill kitchens

### Assemble evidence from the existing corpus and new fetches

- Add `daemon/kg_collect/site_publish.py`. Call `prepare(conn, seed, raw, now)` from `cycle.stage_gaps()` after target review
  and promotion. It reads source-backed `extraction` rows joined to successful `fetch_record` rows and their stored publisher
  text. Reuse the existing `candidate_fact.evidence_json.locator.extraction_id` when available to find the original extraction.
  Candidate status, counts, and copied snippets carry no publication weight on their own.
- In `extract.py` and `resolve.py`, handle explicitly named new venues in quoted or clearly delimited restaurant phrases in
  completed opening or current owner/head/lead chef clauses. Allow a two-syllable new venue such as 오닐 when the quote explicitly
  identifies it as a venue. Resolve past venue in `org_text` and the current venue in `restaurant_text`. A sentence like “미토우 출신
  김○○ 셰프가 서울에서 레스토랑 ‘○○’을 열었다” should emit both supported edges. A restaurant mentioned elsewhere in the same career sentence is
  evidence only if a rule establishes its connection to the chef. Keep guest shifts, pop-ups, planned openings, adjacent names,
  historical employment at the destination, and co-working out of those edges. Reject contradictory current affiliations or
  known closure for a new entry and send genuine conflicts to human review.
- Reuse `extract.clean_name()`, `extract.clean_venue()`, and `review.reject_reason()`. Apply the normalized stoplist at
  extraction, resolution, candidate promotion, and publication. Include 최근, 이번, 한국, 선보인, 진행한, generic roles, and institution
  names. Exact curated aliases resolve before new-name heuristics. Use `targets.norm()` and `Gazetteer` over KR/EN spellings,
  romanization variants, seed `prev_names`, and `target_alias`. Resolve chef homonyms with sourced venue/career context and
  restaurant homonyms with branch/address evidence. Unresolved collisions go to review. If a venue is already in seed under any
  alias, keep its ID and use the existing correction flow.
- Join only on one unambiguous chef identity. Require a past career edge to a recognized seed kitchen, a completed opening by
  that chef or explicit current lead/owner affiliation with the destination, and an explicit destination city or unambiguous
  address from a reliable source. Require a current official profile or a report of current affiliation or completed opening
  dated within 24 months. Older sources can substantiate former-kitchen tenure. Never infer Seoul from the query, publisher
  section, or the mother kitchen's address. Two genuine past kitchens can support the same chef, but the site row uses one
  kitchen. Choose the underfilled kitchen among equally supported edges and preserve the other sourced evidence in SQLite.
- Bump `EXTRACTOR_VERSION` with the extended rules so the existing `stage_extract()` re-extraction relabels the stored corpus
  in place (rules are free; AI stays behind the existing eligibility and budget gates). Assembly reads current-version rule
  rows and dedupes evidence by canonical URL + quote across versions. A missing raw snapshot is a lead for ordinary
  rediscovery and yields no publication evidence. The bounded 120-snapshot/cycle replay checkpoint moves to PR B as a
  fallback if the one-off relabel is too slow in a single run.

`prepare()` uses `kg_daemon.claims.queue_review()` with `reason='entity_publication'`, `subject_type='publication_bundle'`, and
`subject_id=bundle_id`. Use a stable hash of the resolved chef target, restaurant target, and kitchen ID for `bundle_id`. Its
payload contains target IDs, canonical display names, kitchen ID, city and optional source-backed fields, gate result, and
evidence grouped by `lineage`, `affiliation`, and `location`. Each evidence reference contains the extraction/fetch IDs,
publisher URL, host, quote, publication date, and span. A location span may come directly from the named restaurant's fetched
page. Key the review by bundle ID plus a hash of proposed values and sorted evidence tuples (edge, canonical URL, quote, article
content hash). Exclude refetch IDs and timestamps from the hash so unchanged snapshots reuse the decision. Close superseded open
reviews and require the approval to match the current fingerprint. Store the original review time and selected proof references
in the resolved payload for replay.

### Publication and review gates

- Accept only unambiguous source assertions: reuse `kg_ai.pending()` as the fail-closed provenance filter
  (`asserted_by='source'`, `epistemic IN ('claim','fact')`, `pending_only=0`, rule extractor). Confirm each displayed quote is
  an exact contiguous excerpt of at most 200 characters in the normalized stored
  publisher text, including enough context to substantiate its asserted edge. Retain the exact excerpt, URL and original span.
  Skip excerpts requiring privacy redaction so the public text stays verbatim. A feed headline, Bing result, guessed official
  host, synthetic JSON-LD quote, or job context cannot substantiate an edge.
- Use the trusted-source policy in `kg_daemon/claims.py` and the deny lists behind `crawler/apply.py:reliable()` and
  `kg_collect.net.is_banned()`. For this path, count original reporting from curator-classified press and a verified first-party
  source speaking about its own chef or venue. Check `official.site_basis_for()` and a verified ownership association to the
  subject before using the official shortcut. A domain evaluator's guessed tier or JSON-LD type alone is insufficient. Exclude
  banned, ignored, unreliable, entertainment-only, sports-only, social, booking, search, reference, and blocked sources from the
  evidence count. Korean food reporting from a general-interest publisher can qualify for that article. In PR A, seed a small
  human-edited `publication.press_hosts` mapping in `sources/discovery_seeds.yaml` from vetted hosts in the existing corpus,
  starting with hotelrestaurant.co.kr, foodnews.news and bakerynews.co.kr. Each entry gives a publisher group and an article
  scope. Review the most common useful hosts in the stored backlog as part of that edit. Trust first-party sites per venue and
  require a curator decision before machine-discovered hosts count.
- Check independence per edge using `claims.independent_sources()`, collapsing known publisher mirrors and subdomains, canonical
  duplicates, identical quotes, and syndicated copies. Reuse the existing `evaluate.is_repost()` similarity threshold of 0.55 on
  article text as a conservative duplicate signal. Different reporting about the same edge counts separately. A source that only
  verifies the destination or the address does not corroborate the former kitchen.
- Resolve a bundle as deterministically ready when both the past and current edges each have two independent reliable sources or
  one official source. Location needs its own reliable evidence. Publish with `lineage_status='verified'` and display **출처 확인**.
  Restaurant outreach confirmation is a separate field.
- If either edge has a single eligible source after grouping and all required fields have evidence, keep the bundle open for
  `kg_ai.review`. Extend its dispatcher to submit one bundle with its exact source windows through the existing budgeted
  `Client`. The bundle lane selects `reason='entity_publication'` reviews and validates against the bundle's own resolved
  targets (candidates included); it must not reuse `open_items()`'s ai_inference-only selection or the legacy `eligibility()`
  active-target text gate, which would silently skip every new-venue bundle. The model returns only approve/reject and reason
  against the bundle fingerprint and must approve every provisional assertion presented. Record the result with `_close()` and
  the existing review audit flow, with cache/retry keys tied to the fingerprint. Record approval separately from site publication so a reviewed bundle does not enter the quote-only digest.
  Approved source assertions publish as `lineage_status='unverified'` and `career_checking=['kitchen']`, plus `'role'` when a
  provisional former-kitchen role is shown. PR #114 and `tests/test_career_checking.py` define the existing **확인 중** behavior.
  Use `출신` for an unclear role or a role whose evidence is weaker than the verified lineage.
- A raw `ai_inference` finding has no new-entity publication path, even after legacy quote-only review. The legacy `publish()`
  and `ai_reviewed_unlinked` flow supplies no evidence or publication-approval signal to this path. Entangled identities,
  contradictory claims, and incomplete bundles stay in human review or get a targeted evidence query. Missing model credentials
  or exhausted budget leave actionable reviews open. Alternate eligible bundle and legacy review batches after first taking any
  ready bundle for an empty kitchen. Within the bundle lane rank empty kitchens first, then Seoul, then oldest. Dismiss exact
  duplicates and superseded reviews before model calls. Leave existing conflict and service review handling in place.
- For a source-backed partial bundle, enqueue the missing edge, address, or official bio in the existing general/gap query
  allowance. Use an exact chef and venue query such as `"<chef>" "<restaurant>" 셰프 소개`. An article-linked official site
  may be checked while the target is still a candidate. Reuse `official.verify_site()` and the normal robots and egress gates.

### Write the site record, cap additions, and roll back

- `crawler/apply.py` calls `site_publish.apply_ready()` after the existing update logic. Read the daily pipeline's
  `KG_SERVICE_DB` and the adjacent raw store. Revalidate the prepared bundle, source policy, review fingerprint, aliases, stable
  ID and existing seed at write time. A missing collector DB still permits ordinary crawler application. Publication days
  (`checked`, `day_kst`, cap window) use `zoneinfo('Asia/Seoul')` like `kg_ai.budget.kst_day()`, never the host-local
  `dt.date.today()` that `apply.py` uses for `crawl_checked`. Use a deterministic URL-safe restaurant ID such as
  `alumni-<stable hash of restaurant target ID>`. The same target and aliases retain one site ID across retries and renames.
- Write a `data/seed.json` `restaurants[]` row with `id, name, chef, type:'alumni', kitchen:<existing kitchen id>, city,
  checked:<KST date>, lineage_src, lineage_status`. Use the primary past-career quote's canonical publisher URL for
  `lineage_src`. Add `lineage_evidence` with public `{kind, src, quote}` objects for the supporting past and current edges and
  cited location. Set `role` to the evidenced former-kitchen title or `출신`. Set `opened` to a sourced date or `미확인`. Include
  `area, cuisine, url, press` when substantiated, and use `price:null` when unknown. Omit unknown ratings, awards and outreach
  fields. Let `data/enrich_en.py` generate the current EN fallbacks and `web/data.js`; it currently projects whole rows, so
  strip `publication` there (top-level tombstones are already outside the projection).
- Show the escaped quotes and links in the lineage section or evidence panel in `web/app.js` and the detail pages in
  `scripts/build_pages.py` and `scripts/build_pages_en.py`. Use `career_checking` on provisional claims in those views,
  including the existing SPA badge. On provisional entries omit unverified `alumniOf` or `worksFor` from static structured data.
  Adjust ownership wording on kitchen lists and the graph intro to cover alumni who lead restaurants as well as owners. Keep the
  existing graph, directory, and sitemap paths. Preserve the exact `const SHOW_VERDICTS=false;` / `const SHOW_NEWSLETTER=false;`
  lines and the badge substring asserted by `tests/test_career_checking.py` (the pipeline greps both guards). Only accept
  HTTP(S) publisher URLs without credentials or fragments and escape URLs on output.
- Hard cap **12 additions per KST day**, shared by verified and checking entries. First select at most one ready row per empty
  kitchen, considering Seoul first within each kitchen and allowing another sourced city if it is the sole eligible coverage.
  Resolve duplicate destinations across kitchens before consuming a slot. Fill remaining capacity with known Seoul first, then
  verified over checking, then a newly represented named chef, then oldest ready bundle and stable ID. Venues without a sourced
  city wait for location evidence. Excess candidates remain ready for the next day. If a generated checking row later meets the
  full gate, update its lineage status and evidence in place and remove the applicable checking markers. This uses the original
  site ID and consumes no addition slot. Leave conflicts or edits to the row for human review.
- Store `publication:{bundle_id, chef_target_id, restaurant_target_id, review_id, published_at, day_kst, row_digest}` on
  generated rows. Use a digest of the last bot-written row excluding enrichment fields for rollback and upgrade checks. Keep
  this metadata out of the SPA projection. In seed, count the day's live publication markers plus its rollback tombstones
  against the cap. Apply under the daily Git common-dir lock (`$GIT_COMMON/kg_daily.lock`, held on fd 9 by
  `daily_pipeline.sh`) and replace seed atomically with a same-directory temporary file. Under the pipeline, apply reuses the
  inherited lock fd (passed via `KG_DAILY_LOCK_FD`); standalone apply and rollback flock the same file. Never open a second
  non-blocking flock against the held lock. Put a deduplicated `new_alumni` event with gate, date, sources and stable ID in
  `crawler/applied.json` and include it in `last_summary.json`.
  Regenerate missing creation audit events from seed markers after a partial write. SQLite approvals remain available through
  interrupted builds, PR failures and restarted runs. Seed content on the checked-out main is the authority for site presence.
  Extend `daily_pipeline.sh`'s `SUM` expression and PR body to report verified and checking `new_alumni` additions and point
  at `scripts/site_publication_rollback.py`; otherwise growth days commit as "pending queue only".
- Add `scripts/site_publication_rollback.py`, modeled on `scripts/ai_review_rollback.py`. Select a generated entry by restaurant
  ID, bundle ID or review ID and require a reason. Use an atomic seed replacement to remove only the marked entry and add a
  top-level rollback tombstone with its identity, original publication day, rollback time and reason. The tombstone blocks
  automatic re-addition by identity, even if a later fetch changes the fingerprint. Refuse ambiguous matches or rows with
  subsequent material edits and show the record for a manual correction PR. If a DB is available, dismiss its publication
  reviews after the seed write. Reconcile these dismissals on later apply if the rollback ran without a DB. A repeated rollback
  is a no-op. Rebuild and ship through the ordinary PR/deploy path.
- In `targets.load_seed_targets()`, merge publication rows into the existing targets via the stored IDs. Set the restaurant's
  seed ID and sourced city, add the restaurant seed ID to the chef's known restaurants, and reconcile
  `attrs_json.published_to_site` from seed and tombstones. Preserve `origin='discovered'` on these targets. Mark eligible
  published targets active after the seed write is visible to the loader. Call the loader in `cycle.stage_plan()` before query
  generation, even with a populated service DB. Only source-qualified promotion in `resolve.promote_candidates()` activates an
  unpublished target. Recheck legacy active discoveries against the same name and source filters before adding their queries.
  Keep ontology and tenure verification in the service daemon and count growth on the rendered seed.

### Snowball and Seoul-first backfill

Extend `discover.entity_status_jobs()` to include active chefs and restaurants of both origins. All promoted targets get the
same rotation as seed targets, including those awaiting site publication. In `discover.followup_jobs()`, extract former venue
names only from explicit, non-ambiguous source career quotes. Resolve known kitchens via their aliases. An unknown former venue
with a valid name becomes a query lead and can use `"<venue>" 출신 셰프` and `"<venue>" 수셰프 인터뷰`. Use `"<chef>" 셰프 경력 인터뷰` and
`"<chef>" "<restaurant>" 서울 오픈` for entity seeds. Use sourced city when available and do not promote a name merely because a job
used it.

Add `discover.mother_kitchen_jobs(seed, today)` with **N = 3** published alumni restaurants per kitchen. Count distinct
`type='alumni'` rows linked by `kitchen` so the count matches the graph. Rank zero before one before two. Use the canonical
Korean name, `en`/`name_en`, and explicit aliases in `sources/discovery_seeds.yaml`. Rotate queries by template and language.
When all four initial gaps are empty, schedule one due KR and one due EN search per gap each day. Keep at least three templates
per language for empty kitchens so a 72-hour cooldown can rotate without burning repeat no-result queries.

| Kitchen ID | Korean query examples | English query examples |
| --- | --- | --- |
| `layeon` | `"라연" "신라호텔" 출신 셰프` / `"라연" 수셰프 독립` | `"La Yeon" "Shilla" former chef Seoul` |
| `mitou` | `"미토우" 출신 셰프 서울` / `"미토우" 수셰프 오픈` | `"Mitou" former sous chef Seoul restaurant` |
| `allen` | `"레스토랑 알렌" 출신 셰프` / `"레스토랑 알렌" 수셰프 독립` | `"Restaurant Allen" former chef Seoul opening` |
| `allaprima` | `"알라 프리마" 출신 셰프` / `"알라프리마" 수셰프 오픈` | `"Alla Prima" former chef Seoul restaurant` |

For other underfilled kitchens use the same `"<KR>" 출신 셰프 서울`, `"<KR>" 수셰프 독립` and `"<EN>" former chef Seoul restaurant`
templates. Add a qualified venue or city token when the bare name is ambiguous. Use a 365-day search-item lookback for
mother-kitchen queries and the existing 120 days for general discovery. Stop scheduling and cancel pending dedicated queries
when a kitchen reaches three. Resume if its count falls. Current articles can describe older tenure.

- In `sources/discovery_seeds.yaml` set daily RSS query allowances of **8 mother-kitchen, 8 entity/former-venue, 8
  general/gap**. Every RSS search dispatch claims a bucket — legacy keyword/status/gap and model-planned queries count too,
  so total search volume stays bounded; within the entity bucket rotate by least recently queried so due seed status checks
  keep their turns. Use one KST dispatch ledger with `PRIMARY KEY(day, canonical_query)` and columns `family, target_id,
  job_id`, added by `schema/migrations/0011_collect_discovery_scope.sql` (ledger only in PR A; `crawl_job.discovery_json`
  moves to PR B). Claim the slot transactionally immediately before the first dispatch so quotas survive restarts. Keep the
  shared HTTP counter for actual requests. Prefer fresh templates, then requeue successful query roots after 24 hours and
  no-result roots after 72 hours. An unused mother-kitchen allocation goes to entity queries. Keep one query family per
  normalized query per day.
- In `cycle.stage_plan()`, enqueue dedicated backfill roots before generic `keyword_jobs()` and raise the backfill queue cap
  in `discovery_seeds.yaml` (400 → 480) so the existing ~469-job backlog does not reject them (`crawl_job.path` is
  CHECK-constrained, so roots reuse `keyword_search`/`entity_followup` paths and the ledger carries the family). In
  `scheduler.plan()`, take one due root per empty kitchen before general ranking and before that kitchen's second query.
  Schedule at most two entity queries per target per day and rotate by least recently queried so seed and discovered targets
  both get turns. Preserve the existing discovery pool reservation and depth limit of two. Leave blocked jobs blocked.
- Deduplicate canonical publisher URLs across job paths (the scheduler already picks each URL once per plan). A former-venue
  name from a quote enters the bounded query rotation, so following that lead uses one of the daily slots even when the source
  article is already at the link depth limit. The numeric `job_priority()` bonuses (**+0.6** sourced Seoul, **+0.8** empty
  kitchen, **+0.4** one-or-two alumni), `city_hint` propagation to child article jobs, and stale-queued-slot eviction move to
  PR B; PR A query templates already embed 서울 where it is sourced.

### PR A acceptance

Use a migrated temp SQLite DB, stored text fixtures, a temp copy of the repo for seed/build inputs (the builders write into
`web/` — never run them against the live tree), a fixed KST clock independent of host TZ, and fake model/transport. No network.

1. Run a fixture through rule extraction, `stage_gaps`, review, `crawler/apply.py` and build. Cover both a one-official-source
   case and two independent reports. Verify new restaurant, chef representation, existing kitchen link, escaped verbatim quotes,
   static KR/EN and SPA views, and directory and sitemap links. A single report needs its matching approval and shows **확인 중**.
   Keep the PR #114 badge assertions and both guard lines. A stored fixture with no current-version extraction yields evidence
   after the version-bump relabel, without network; `publication` metadata stays out of `web/data.js`.
2. Show that two reports of different edges, syndicated copies, host aliases, snippets, fabricated quotes, model-origin rows,
   junk names, entertainment and banned/unreliable sources, ambiguous aliases, and a seed duplicate never meet the automatic
   addition gate. Test unsupported location and an older employment claim with conflicting later evidence.
3. Test same-day apply replay, competing apply calls, failure before and after seed replacement, more than 12 ready cases,
   checking-to-verified upgrade, KST rollover, rollback/rebuild, and tombstone suppression after evidence refresh. Check one
   entry per alias, Seoul ordering, and preservation of existing entries and outreach state.
4. Verify promoted targets of both origins enter the rotation, source-backed former venues generate queries, the four empty
   kitchens get KR and EN slots even with a full backlog, quotas survive restart, and threshold and depth rules hold. Run the
   existing daily wiring, review, and build guard coverage against temporary fixtures.

## PR B · Broaden source coverage and spend fetch/model budget on useful documents

### Source configuration and permission

Add approved endpoints to human-maintained `sources/discovery_seeds.yaml` under `feeds`, `sitemaps`, or `directories` with `id,
kind, url, lookback_days, source_role`. Add a curated publication host/tier entry for vetted original food reporting where
needed. `discover.directory_and_feed_jobs()` and `expand_items()` should use this metadata to prefer food, interviews, openings
and Seoul references. Prefer a publisher's RSS or declared sitemap to a listing page. Initial shortlist for the builder's
permission check:

| Candidate | Endpoint or selection | Intended yield |
| --- | --- | --- |
| 식품외식경제 | `https://www.foodbank.co.kr/rss/allArticle.xml`, already listed in the heavy config | Restaurant openings, appointments and original interviews. [Publisher](https://www.foodbank.co.kr/) |
| 월간식당 | `month.foodbank.co.kr`, publisher-declared RSS or sitemap, otherwise the narrow 셰프 스토리/화제의 레스토랑 listing | Chef interviews and restaurant profiles. Group with 식품외식경제 for independence. [Publisher](https://month.foodbank.co.kr/) |
| 매일경제 Citylife and 한국경제 생활 | `https://www.mk.co.kr/rss/60000007/` and `https://www.hankyung.com/feed/life` | Opening roundups and reported profiles with the scoring filter below. [MK RSS](https://www.mk.co.kr/rss/), [Hankyung RSS](https://www.hankyung.com/feed) |
| 메종코리아 | `www.maisonkorea.com`, publisher-declared sitemap scoped to food profiles, with a narrow food listing as fallback | Seoul chef profiles and openings. [Publisher profile](https://www.maisonkorea.com/food/2026/08/beyond-french-cuisine/) |
| Asia's 50 Best | `https://www.theworlds50best.com/asia/en/list/1-50` | Seoul venue and chef leads. Check original interview articles separately for evidence. [Publisher's award list](https://www.theworlds50best.com/asia/en/list/1-50) |

The shortlist identifies publisher surfaces. Add an entry only after a human confirms the actual endpoint, relevant linked paths
and redirects, and applicable robots allowance for the existing user agent via the zecbot-gated polite net. Record the check
date and scope in the YAML. At runtime the same transport checks robots on every destination including redirects. In
`net.PoliteNet.robots()`, defer when robots availability is indeterminate because of transport failure or 5xx. Keep durable
pauses on 403 or challenge and honor crawl delay. An existing heavy-crawl exclusion remains excluded from the expanded daily
sources. Award lists, TV rosters and Michelin announcements on other publishers can seed names. Only qualifying original
reporting or first-party text can support publication. Naver, Instagram and the Michelin Guide are manual only. No paid search
APIs, accounts, browser fallback or block bypass. `KG_IG` stays 0 and Instagram never goes through the proxy.

Gate `crawler/crawl.py` in the daily wrapper as well as `collect_daily.sh`, using the existing `kg_collect.heavy egress` check.
On failure skip outbound web work and retain the ability to apply prepared local evidence. On loss of proxy mid-run stop
dispatching and defer remaining jobs. Only use the current successful legacy findings snapshot plus manual findings, so a
skipped crawl does not pass yesterday's snapshot as fresh. Keep the existing heavy scripts, source list, block times and caps.

### Ranking, rule extraction, and time limits

- Moved from PR A: the bounded historical replay (up to 120 unique stored snapshots per cycle, rules only, preferring
  empty-kitchen mentions then Seoul and named venue cues, with a versioned fetch checkpoint) if the version-bump relabel is
  too slow in one run; the `scheduler.job_priority()` bonuses (+0.6 sourced Seoul, +0.8 empty kitchen, +0.4 one-or-two
  alumni) with `city_hint` propagated to child article jobs for ranking only via optional `crawl_job.discovery_json`
  (`family, kitchen_id, city_hint`); and freeing slots from stale queued generic searches in an overfull pool before adding
  backfill roots.
- In `discover.item_relevance()` and `expand_items()`, score title/summary before fetching. Use **+3** for a known
  mother-kitchen alias within 100 characters of a lineage cue, **+2** for any of 출신/수셰프/헤드셰프/오너셰프/거쳐/경력 or English equivalents,
  **+1** for a known chef or venue, **+1** for Seoul, and **+1** for an opening or chef-interview cue. A curated food/interview
  feed root gives its children **+1** in metadata scoring only. Keep raw score separate from the scheduler’s normalized
  relevance and apply the existing queue bounds. Treat entertainment-only or sports-only hosts such as xportsnews, sports.khan,
  celebinfluencernews and wikitree as low-priority leads with **−5**, ineligible for publication or paid extraction. Use a score
  of **3** for ordinary article admission and keep at most three metadata-poor sitemap trial URLs per host/day. Deduplicate
  before enqueue and preserve the candidate and fetch caps. Keep existing official status jobs eligible for their narrow refresh
  role.
- Unwrap Bing RSS item links in `fetch.parse_feed()`, accept publisher URLs after canonicalization, and reject remaining Bing
  HTML/result URLs in enqueue and dispatch. Feed metadata and listings supply leads only. Reserve up to 12 units from the
  existing discovery/backfill shares for ready high-score article children. Let `cycle.stage_fetch()` schedule them after the
  roots run, then release unused units to the ordinary plan. Charge every dispatch against the same run budget and retain the
  depth bound. Avoid a full extra day of latency for high-score items.
- In `cycle.stage_extract()` and `pipeline.process_doc()`, run and persist rules first across available fresh publisher bodies.
  Then rank eligible unresolved documents with the same score on bounded body windows, requiring **score ≥5** plus the existing
  active target text gate. A document with a concrete unresolved field relevant to a publication bundle gets **+1**. Use
  `fetch_priority.score` from heavy handoff as a tie-breaker. Reuse the score gate in `kg_collect.deepseek.Adapters` for
  eligible fresh documents from the legacy daemon as well. Existing dedicated rechecks keep their scoped eligibility, measured
  quotas and review holds. Defer unaffordable high-value documents for a later run without duplicating rule rows. Suppress
  discretionary AI query planning when deterministic jobs fill their slots. Keep the two review calls in the daily pipeline so
  older reviews get time before new extraction, and fresh source bundles can be reviewed in the same run.
- Review, extraction and gap planning share one **20-unit/day** allowance (40k tokens at 0.5 unit per 1k). Leave the
  `kg_ai/budget.py` clamp and `KG_AI_DAILY_COST_CAP_OVERRIDE` behavior intact. `kg_collect/deepseek.py:Adapters.for_meter()` and
  `kg_ai/review.py` use `kg_ai/deepseek.py:Client`. Keep DeepSeek (`deepseek-chat` by default) first and Qwen4-Max
  (`qwen4-max-preview`) as the configured fallback for every AI stage, including the new bundle review and the service daemon's
  shared adapter. Keep reservation and fallback error handling. Log aggregate counts and sanitized reasons without credentials,
  headers or raw article blobs.
- In `crawler/crawl.py`, rotate at most **10** per-restaurant RSS queries and **10** official-site probes per day, prioritizing
  due Seoul status checks while retaining feed passes. Persist last attempt and last success per target and probe kind in the
  collection DB so growth does not turn the legacy step into 150 daily Bing searches or starve older targets.
- In `scripts/collect_daily.sh` keep the defaults at **60 web units / 78 total cycle units** for initial rollout. Prepare **75 /
  95** as a paired setting, since `cycle.stage_plan()` subtracts the remaining AI allowance from total cycle units before
  planning web jobs. A 75-unit web run adds at most 15 ordinary GET units over baseline. Use the approved daily runs' timing
  data before enabling the higher defaults. Require five runs with fetch stage at most 850 s and cycle at most 1,100 s, then
  look for at least 10 additional successful publisher documents per day and no 1,500 s timeouts over five runs. Keep an
  internal fetch cutoff at 1,100 s from the start of the cycle, with a check before the next request, to leave time for
  extraction and checkpointing. Resume queued work the next run. Restore 60/78 if the increase fails the timing or yield check.
  Keep the existing 1,500 s external timeout and the 200 content dispatch / 500 candidate URL KST ceilings in `limits.py`. Aim
  for roughly 80–100 combined content dispatches outside heavy blocks at 60/78, then 100–120 if 75/95 qualifies. Redirects also
  use the shared dispatch ledger. Treat these as planning ranges and compare actual completed publisher bodies.

### PR B acceptance

Use temporary DBs and fixture RSS, sitemap, redirects, article bodies, frozen clocks, and fake network and AI transports. No
network tests or paid collection runs.

1. Verify root and child robots policy, blocked redirects, 403 durable pause, robots failure deferral, egress skip for both
   daily web paths, no direct fallback, and manual-only exclusions. Check that heavy configuration, schedule and limits keep
   their existing behavior and `KG_IG=0` skips Instagram.
2. Compare the old and new fixture plans. A Seoul chef interview and an empty-kitchen publisher article should be fetched ahead
   of entertainment items, Bing HTML and repeat low-yield searches. Verify same-run child admission within budget, URL and
   syndication dedupe, fair legacy rotation, remaining queue on timeout, and shared 200/500 limits across retries and processes.
   The replay checkpoint advances across cycles without refetching, and the priority bonuses rank empty-kitchen roots first.
3. Show that inexpensive rules run with AI disabled and that paid calls only reach eligible high-score fresh documents. Combine
   gap plan, extraction, legacy and bundle review in one fake KST day and assert the shared default ceiling, existing override
   semantics and recheck holds. Extend `tests/test_qwen_fallback.py` for the new review task and keep gap/extract fallback
   assertions. Stub the pipeline to assert existing Git/deploy flow, no secrets in output, `SHOW_VERDICTS=false`,
   `SHOW_NEWSLETTER=false`, and no `noindex`.

## Expected growth and daily checks

Track unique canonical named chefs visible in seed, added restaurant rows, verified and checking totals, named alumni per
kitchen, rejected and deferred reasons, publication-ready backlog, oldest review age, query/article yield, proxy skips, and AI
units by task. Report these in existing cycle/apply summaries. Count chefs with `targets.chef_names()` and canonical alias
identities across restaurants, including existing multi-chef rows. A deployed graph count is the outcome measure. A merged PR
awaiting deploy is tracked separately.

| Scenario after both PRs | Distinct useful publisher docs/day including heavy handoff | Complete new distinct bundles per doc × gate pass rate | New rows / new chefs per day | Named chefs after 14 days from ~50 |
| --- | ---: | --- | --- | ---: |
| Slow | 35 | 18% × 50% | ~3 / ~2–3 at 80% new chefs | ~85 |
| Planning case | 50 | 20% × 55% | ~5–6 / ~4–5 at 85% new chefs | ~115 |
| On pace for goal | 65 | 24% × 60% | ~9 / ~8 at 85% new chefs | ~160 |

Estimated conversions depend on both PRs being active; the stored corpus may improve the first week if unresolved names have
usable article text and both edges already documented. Reposts, already-listed chefs, missing destination evidence and review
delays each lower conversion. Reaching 150 from 50 needs **7.2 new distinct chefs per day** over 14 days, or about **8.5 accepted
rows per day** at 85% unique. Check after seven daily deployments for roughly 100 named chefs and progress on each initially
empty kitchen; at day 14 inspect any remaining zero separately and tune queries from its evidence-gap counts. Keep the source
threshold in all scenarios.

For a future cap decision only, **20 → 30 units** buys 20k tokens/day: at 2.5k settled tokens per call, about **8** extra calls
(**4–6** with conservative reservations and fallback overhead) — e.g. three extraction and two review calls could add up to two
published rows when reviewable bundles wait. Marginal growth can be zero if corroboration, qualifying venues or the publication
cap bind. The current design uses the 20-unit cap.
