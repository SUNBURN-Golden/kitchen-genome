# G2 build report

Date 2026-11-20. Branch `feat/global-g2`, based on `92f369d`. Scope follows the approved G2 design and owner items 1, 3a, and 6.

## Implemented

- The Michelin selection directory supplies discovered country and regional scopes. `sources/michelin_selections.yaml` resolves known locale and regional overlaps and provides the South Korea refresh entry. The roster parser follows scoped tier filters and pagination and records 1–3 Michelin stars, venue identity, country, city, address when available, edition when explicit, and the fetched page and row. Explicit no-list scopes have their own disposition. Locale mirrors share venue identity and homonymous branches retain separate locations.
- MediaWiki revision requests read main-slot wikitext and retain edition columns, revision IDs, and row locators. The announcement lane uses the existing Bing News RSS endpoint and links from the official guide. Official selections take precedence within an edition. A dated cross-check can corroborate a selection, expose a discrepancy, or supply a provisional lead. Same-edition press releases and syndicated results carry their own source labels. New editions preserve prior observations.
- Migration `0014_global_queue.sql` adds the shared SQLite tier cursor, coverage records, per-host leases, work origins, and conditional-fetch metadata. The worldwide first pass proceeds 3★ → 2★ → 1★. Deferred scopes retain their originating tier. Workers commit results and their continuations before acknowledging the lease. Expired leases can be reclaimed and expired checkpoints settle without fetching again. Schema application is atomic across concurrent starts, and raw snapshots are written through an atomic rename.
- The existing heavy blocks drain the frontier and report scope counts by tier and disposition. SIGTERM stops new dispatch and allows bounded in-flight work to checkpoint. The 07:00 collector spends a bounded part of its existing fetch phase on South Korea and due incremental work, including staggered conditional refreshes and announcement-triggered rechecks.
- Venue pages and localized chef queries produce shared article and extraction jobs with their originating branches and tiers. Eligible Korean snapshots use the existing extractor and guarded Qwen-first chain. Foreign extraction jobs carry an explicit G3 deferral and the roster remains in private service state.

## Verification

- The fixture suite covers localized wikitext, two editions, pagination, regional overlap, missing years, press attribution, official precedence, provisional leads, and scope-level discrepancies.
- Queue tests cover both sides of the checkpoint boundary, late lease tokens, concurrent daily/heavy claims, a large busy-host backlog, conditional 304s, and the existing night, midday, optional evening, and 07:00 entry points. Fixture access refusals exercise 403, 429, captcha, and login walls under the shared host guard.
- `TZ=UTC python3 -m pytest -q tests` passed with 695 tests and 285 subtests. IPv4 and IPv6 sockets were denied. Collector inputs used fixture transports and temporary state.
- Scope enumeration, markup coverage, and roster counts were exercised with authored fixtures. Unrecognized layouts and unresolved venue locations are recorded for review.
- `scripts/heavy_crawl.sh` received comment updates only. The crontab, 1,500-second daily timeout, site rendering and publication configuration, and Instagram wiring retain their existing settings.
