# AI promotion — stage build review (2026-11-22)

Reviewed `feat/ai-review-promote-lineage` at `e915004` against `origin/main`
(merge-base `fd06b23`): 27 files, +1785/−154. The review traced the full path
`review.decide → _finish_delivery → promotion.backfill → site_projection.refresh/graph/compose
→ record_links → crawler/apply counters → page builders`, plus `ai_review_rollback`
and the migration/ownership schema. Review-stage fixes (finding 1 and the selector
tests) are applied on top of `e915004`; the review-fix commit closes the two open
low-severity findings (2 and 3) with regression tests.

## Method

- Full diff read; supporting G3 code (`lineage._binds/_source/_fields`,
  `lineage_identity.resolve_branch/resolve_chef`, `site_publish.apply_ready/reconcile_events`,
  `daily_lock`, `Site.match`) read in its post-branch form.
- Full pytest with the repo's fake transports (no live provider or network calls).
- Independent dry run, not trusting the design doc's recorded numbers: copied the
  branch seed (SHA-256 `82697baebc1df42274b9d3b073f2e91be2112673361b0971a47e81f95b5137cc`,
  13 kitchens / 52 restaurants / 41 unlinked / 50 attached) to `/tmp/kg-promote-verify`,
  backed up the live service DB opened `mode=ro` into memory, applied migration
  `0016`, read the stored articles from the live raw dir, and ran
  `promotion.backfill()` twice inside write transactions. Then built the site in a
  sandbox checkout (enrich_en → build_pages → build_pages_en → build_feeds) and
  checked the publication guards. Script: `/tmp/kg-promote-verify/dryrun.py`.

## Verification results

Dry run (reproduced exactly, before and after the finding-1 fix):

| Measure | Pass 1 | Pass 2 |
| --- | ---: | ---: |
| `promoted` | 7 | 0 |
| `unlinked_remaining` | 37 | 37 |
| Supports / distinct links | 29 / 17 | unchanged |
| New restaurant holders | 4 (소재(SOJAE), 페스타 바이 충후, 르도헤, 하누) | 0 |
| New checking links / upgrades | 7 / 3 (라망 시크레, 밍글스, 이타닉 가든) | 0 / 0 |
| Seed bytes | — | identical |
| Audit log bytes | unchanged | unchanged |
| Owned identities | 6 chef, 4 branch, 4 restaurant, 4 alias | stable on re-run |

`promotion.events()` against an empty ledger emits 7 `new_alumni` (all checking)
+ 3 `upgrade_alumni`, matching the design doc's `added=7 ready=7
new_alumni_checking=7 new_alumni_verified=0 upgraded=3`. Two promoted chefs at
one venue on one day (정한세/밍글스 creation + 강민구/밍글스 upgrade) keep distinct
event IDs — the `(restaurant_id, day_kst)` collision the design flagged does not
occur on the promotion path.

Site guards on the sandbox build: KO and EN `web/r/*`/`web/k/*` directory sets
equal the curated seed ID sets; zero `noindex` across all pages;
`SHOW_VERDICTS=false` and `SHOW_NEWSLETTER=false` intact; no `ai_promotion`,
support ID, `body_fingerprint`, `publisher_group`, `content_hash`, `assertion_id`
or model metadata in `web/data.js` or any static page; all four new venue pages
render 확인 중/Checking + AI 검토/AI-reviewed with no `worksFor`/`alumniOf` and no
`addressCountry` KR fallback.

Tests: **766 passed, 1 skipped, 285 subtests passed** (pre-implementation
baseline 736; branch added 23; review stage added 4; review-fix stage added 3).
The two finding-1 regression tests fail on `e915004` and pass with the fix; the
finding-2/3 regression tests fail without their fixes. The dry-run numbers are
unchanged by the review-fix commit (finding 2 only renames upgrade event IDs,
which are fresh on a first apply; finding 3 is JSON-LD keying).

## Findings

### 1. CONFIRMED — FIXED: native↔reviewed body fingerprints never matched (false-verification risk)

`lineage._finish` stores `career_document.body_fingerprint` as
`digest(shingle)[:12]` over **casefolded** shingles. `site_projection._snapshot`
passed that stored list through `retained_fingerprint`, which digested it
**again** to `[:16]`, while `promotion._original` fingerprinted **raw**
(non-casefolded) shingles to `[:16]`. For the same article text the two sets are
disjoint — measured jaccard 0.0 where 1.0 is correct.

Failure scenario: a native G3 proof and an AI-reviewed proof drawn from an
edited cross-domain repost (no wire credit, different URL, different quote,
different content hash) pass every other independence disqualifier; the
fingerprint overlap check — the only remaining guard — silently returns 0, so
the pair counts as two independent sources and the link upgrades to verified
on a single original article. Exact duplicates were still caught by
`content_hash`/`quote_key`, and same-publisher copies by
`publisher_group`/`origin_group`/registrable domain, so the gap is narrow but
real and defeats the design's "near-reposts … one proof" rule.

Fix: `retained_fingerprint` now packs the pre-hashed 12-hex digests directly
(6-byte chunks in `_shingle_set`), and `promotion._original` supplies the exact
native recipe (`digest(s)[:12]` over `evaluate.shingles(text[:3000].casefold())`).
Native↔native and reviewed↔reviewed comparisons are unchanged; native↔reviewed
now compares like with like. Regression tests:
`test_fingerprint_recipe_matches_native_career_document` (packing must not
re-digest) and `test_native_near_repost_does_not_verify_reviewed_link`
(end-to-end: native ingest + reviewed edited copy on another eligible host
stays 확인 중 with two evidence refs). Dry-run numbers are unchanged by the fix.

### 2. CONFIRMED — LOW — FIXED: a re-upgrade after a downgrade is not counted

`promotion.events()` keys the upgrade event `link_key + ':verify'`. Sequence:
create (checking) → publish → independent source upgrades → publish (verify
event logged) → rollback of the verifying source restores checking
(`record_links` pops `upgraded`) → a new independent review re-upgrades.
The second upgrade satisfies the `elif` gate (the historical create event still
says checking) but its event ID is already in `crawler/applied.json`, so no
event is emitted and `upgraded` undercounts that day's transition. The link
itself publishes correctly at verified; only the daily counter misses it.
Fix: `promotion.events` now suffixes the verify event ID with the upgrade day
(`key + ':verify:' + link['upgraded']`) — same-day retries still dedupe, and no
ledger migration is needed because the branch is unmerged. Regression test:
`test_reupgrade_after_downgrade_counts_again` (create → upgrade → publish →
rollback → next-KST-day independent re-upgrade counts `upgraded=1` again, then
a same-day re-apply dedupes to 0).

### 3. CONFIRMED — LOW — FIXED: duplicate Person node in JSON-LD in one configuration

The curated-scalar fallback in `people_jsonld` searches for a matching
`chef_id` only in `career_affiliations`. When the curated chef matches a
reviewed edge that appears **only** in `origins` (e.g. a reviewed former stint
at the origin kitchen with the destination employment carried solely by the
curated scalar), the fallback keys a second Person by the curated name string
while the first loop already created one keyed by `chef_id`. Both nodes are
evidence-backed — no false fact — but the same human is emitted twice, with
`alumniOf` duplicated. Fix: the curated-scalar fallback in `people_jsonld` now
searches `career_affiliations` then `origins` and keys by `chef_id or chef_name`
(the first loop's keying), so the curated facts merge into the existing Person
instead of creating a twin. Regression test:
`test_people_jsonld_merges_curated_scalar_into_reviewed_origin_person` (KO/EN:
one Person carrying `alumniOf` + `worksFor`; a checking curated scalar still
omits `worksFor`).

### 4. CLOSED: rollback selector test gap

Design acceptance #5 asks for review-ID, extraction-ID and `--since` rollback
exercise; only `--review-id` had promotion coverage (`--extraction-id` appeared
only in the legacy quote-only test). Added
`test_rollback_selectors_remove_promoted_link[extraction|since]`: both selectors
remove the promoted link, tombstone the review, and a subsequent backfill
promotes nothing.

## Areas verified clean

- **False links.** Every created link was spot-checked against its verbatim
  quote: 하누/문경수 (current, 두바이 stated), 르도헤/김창욱 (current, no city
  invented from 해운대), 소재/김민석 (opening, 광주 from the opening locative,
  pop-up prefix stripped), 페스타 바이 충후/이충후 (current, venue-first
  "의 총괄 셰프를 맡고 있는" binder, 서울 stated), 라망 시크레/권소윤 (current,
  award report naming her restaurant), 밍글스/정한세 (former, "전 … 출신"),
  온지음/박성배 (current, "조은희·박성배 셰프가 이끄는" crew binder). Geography
  is taken only from explicit venue-bound locatives; country stays unknown
  everywhere in the fixture. Binding requires the literal chef and venue in one
  clause plus a G3 verb bind or a narrowly tested Korean pattern; INSTITUTION /
  TEMPORARY / contest / planned-opening / wrong-chef guards all have tests and
  held in the dry run (37 unlinked remain, classified in the design doc).
  Homonyms hold (`len(candidates | previous) > 1 → None`), unknown-location
  venues stay publisher-scoped, and cite-only hosts (`celebinfluencernews.com`,
  `wkorea.com`, `wikitree.co.kr`) publish at checking and never verify — the
  three upgrades each rest on ≥2 eligible distinct domains.
- **Idempotency.** Second backfill pass: identical seed bytes, no audit lines,
  `promoted=0`. Ownership counts stable across re-runs on the same DB. Delivery
  replay tested at three interruption points (before seed, after seed, after
  audit); `publish()` dedupes globally by `review_id`; `record_links` preserves
  `created`/`upgraded` dates so daily re-runs emit no transitions.
- **Crash recovery (DB commit → seed write window).** Traced and cleared:
  ownership receipts commit atomically with the entities they own; on retry
  `_chef` re-adopts via the `ai_promotion_identity` exact-ID check, `_branch`
  resolves the existing branch, `_site_links` maps only curated holders (no
  orphan KeyError), and `_stable_site_id` reuses the committed
  `projection_site_id` allocation, recreating the same holder ID. The
  `SAVEPOINT promote_review` rollback keeps failed chef resolutions from
  leaving provisional branches.
- **Rollback.** All three selectors now tested; seed-only rollback tombstones
  in `ai_review_log.jsonl` block re-promotion even while the DB review stays
  approved; `--db` additionally dismisses and marks `rolled_back`, and undelivered
  `delivery.entry` payloads are withdrawn too. Manual edits survive
  (holder-field equality check), shared entities survive (FK-protected pruning
  with savepoints), removing one of two proofs restores checking, and a repeated
  rollback is byte-identical. The `0016` trigger relaxation only exempts
  rows present in `ai_promotion_identity`; append-only protection is intact for
  everything else.
- **Counters.** `added`/`ready` include promoted creations; gate split at
  publication time (two proofs before first apply → one verified addition,
  tested); upgrades increment only `upgraded`; `named_chefs`,
  `verified_total`, `checking_total` reconcile legacy scalar rows against
  projected edges by folded name keys before counting; `alumni_by_kitchen`
  counts projected origins; scoreboard first-transitions reuse link dates.
- **Site output.** Both builders and the SPA render relation words (이전 근무 /
  재직 / 수련 / 개업), per-source AI badges, 확인 중/Checking and mentor names;
  checking edges stay out of `worksFor`/`alumniOf` (edge-level gate in
  `people_jsonld`, row-level gate in the single-Person fallback, whose key now
  also matches origins-only reviewed edges — finding 3); the curated-scalar preservation in
  `people_jsonld` emits only facts the curated row already supported. New
  holders get durable seed rows + `legacy_id` aliases, so the directory guard
  stays green and no orphan `kg-` page escapes the curated set. EN names fall
  back to the Korean name rather than crashing; `en_terms` gained the new
  city/role terms; `enrich_en` romanizes new kitchen holders and blocklists
  `body_fingerprint`.
- **Constraints.** Diff touches no Instagram, proxy/egress, zecbot, robots or
  host-spacing code, no `config/dev_lanes.json`, no budget/cap code (none added),
  and commits no `data/seed.json` change. Tests use injected transports only;
  no key material is printed or logged. The daily pipeline's inherited
  `KG_DAILY_LOCK_FD=9` makes `seed_lock`/`daily_lock` re-entrant for the review
  subprocess (same open file description), and standalone seeds take a local
  lock.

## Final re-review (2026-11-22, stage RE-REVIEW)

Reviewed the full diff vs `origin/main` after the build and review-fix stages.
`origin/main` had advanced by two commits (crawl `e2a06c1`, bare-array AI fix
`3777dc8` #127); the branch itself never touched the #127 files, but the
divergence would have shown a phantom revert of `list_key` in the PR diff.
Merged `origin/main` into the branch (clean auto-merge; verified
`list_key='reviews'` / `list_key='assertions'` and the envelope tests survived).
The diff vs `origin/main` is now exactly the branch's 28 files, +2100/−154,
with no `data/seed.json`, `crawler/pending.json` or `data/ai_review_log.jsonl`
change of our own (the merge carries main's already-committed crawl data).

Re-read the whole promotion path (`promotion.py`, `review._finish_delivery`,
`site_projection._snapshot/graph/compose`, `site_publish.publication_metrics`,
`crawler/apply`, rollback, migration 0016, both page builders, `app.js`).
Verified: `publication_metrics` returns no `added`/`ready`/`upgraded` keys so
the apply-stage increments survive; summary-before-ledger write order is
retry-safe (a crashed ledger write re-emits into a fresh summary, never
double-counts persistently); `_snapshot`'s `needed` filter covers every node
branch because `_traverse` origins are themselves career rows; `_site_links`
is defaultdict-backed (no orphan KeyError); `people_jsonld` fallback keys
match the first loop; `seed_lock` re-uses the inherited `KG_DAILY_LOCK_FD`;
`SHOW_VERDICTS`/`SHOW_NEWSLETTER` stay false and no builder emits `noindex`.
No blockers found.

Full suite on the merged tree (`TZ=UTC`): **773 passed, 1 skipped, 285 subtests
passed** (766 branch + 7 from #127's envelope tests).

Re-ran the independent dry run (`/tmp/kg-promote-verify/dryrun.py`, live DB
opened `mode=ro` into memory, no network/provider calls) against the **merged
current seed**, which now carries the 20261122 crawl's new reviews
(73 unlinked + 101 attached, up from 41 + 50):

| Measure | Pass 1 | Pass 2 |
| --- | ---: | ---: |
| `promoted` | 9 | 0 |
| `unlinked_remaining` | 62 | 62 |
| Supports / distinct links | 42 / 21 | unchanged |
| New restaurant holders | 6 (소재, 페스타 바이 충후, 르도헤, 하누, 7th Door, 베누) | 0 |
| New checking links / upgrades | 9 / 3 (라망 시크레, 밍글스, 이타닉 가든) | 0 / 0 |
| Seed bytes | — | identical |
| Audit log bytes | unchanged | unchanged |

Events against an empty ledger: 9 `new_alumni` (checking) + 3 `upgrade_alumni`
(verified). Sandbox build passed every guard: KO/EN directory sets equal the
curated seed IDs, zero `noindex`/private leaks, flags false, all six new venue
pages render 확인 중/Checking + AI 검토/AI-reviewed with no `worksFor`/`alumniOf`.
Spot-checked the two new venues' quotes: 7th Door/김대천 binds via the
possessive "'7th Door'의 김대천 셰프", 베누/코리 리 via "베누의 코리 리 셰프";
no geography invented (both city/country unknown).

### Additional residual notes (non-blocking)

- `_location` requires the city to sit adjacent to the venue (one optional
  outlet word between). "샌프란시스코의 미쉐린 3스타 레스토랑 베누" states the
  city but the intervening Michelin descriptor defeats the adjacency rule, so
  베누 publishes with unknown city. Conservative under-extraction, never a
  false fact; widening the rule needs its own binding tests.
- `record_links` allocates a fresh link key when more than one prior link is
  compatible and none shares the group's review IDs; and `events()` can emit
  an upgrade whose create event was skipped (holder absent at creation time).
  Both are narrow counter-only edge cases; publication itself stays correct.

## Residual notes

- `publish()` now dedupes by `review_id` alone (design: preserve independent
  proofs). Two reviews of the same article can therefore both appear in the
  AI 검토 소식 list; career evidence refs still dedupe by `(src, quote)`.
- `withdraw()` recomputes from the cached snapshot; a support whose row is
  excluded by the conflict predicate can lose its holder on an unrelated
  withdrawal, but the next backfill recreates the same holder ID from the
  surviving support — convergent, not lost.
- The 37 remaining unlinked entries are inert display annotations; the design
  doc's classification (8 other-label, 4 missing endpoint, 10 temporary or
  institutional, 1 ineligible URL, 9 unbound, 3 contest, 2 homonym) accounts
  for all of them.
