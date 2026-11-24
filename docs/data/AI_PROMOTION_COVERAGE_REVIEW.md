# AI promotion coverage — build review

Review of `feat/ai-promote-unlinked-coverage` (`342977a`) against `origin/main` (`786616c`),
2026-11-25. Scope: correctness, false links, idempotency, rollback and site guards. The live
`data_store` was opened read-only; every write went to a disposable `/tmp` copy.

## Verdict

The implementation is **safe to merge from a correctness and guard standpoint, but it does not
meet the stated coverage target**: it publishes evidence from **24 of the 117** promotable-looking
reviews (target 40), with **zero guard-list hits**. The shortfall is not a single bug: under the
preserved TEMPORARY / INSTITUTION / CONTEST / source constraints the corpus cannot yield 40
published links (ceiling analysis in F1). Two correctness defects should be fixed before the next
backfill: elided **former** claims are never attributed (F2) and a previously valid support is
withdrawn because its quote occurs twice in a repeated article excerpt (F3).

| Axis | Result |
| --- | --- |
| False links | None found. Every one of the 40 new supports was checked against its verbatim quote. |
| Guard list | 0 new links or held annotations with a guard venue. |
| Idempotency | Second pass: 0 new supports, links, upgrades or publication events; seed bytes, audit bytes and skip rows byte-identical. |
| Rollback | Supports withdrawn, identities pruned, links recomputed and the skip table reindexed; covered by the existing and new tests. |
| Site guards / privacy | Skip reasons and receipts are DB/summary-only; no `ai_promotion_skip`, `promotion_skips`, `binding_receipt`, `body_fingerprint`, `model_meta` in KO/EN pages, RSS or `data.js`. |
| Tests | `TZ=UTC python3 -m pytest tests` → **883 passed, 1 skipped, 317 subtests passed**. |

## Reproduced acceptance run

Pinned seed `data/seed.json` SHA-256
`d3d39f42b59af346b2b1d6c0dab4a0d690f3affaca7b88dd66748fec3db38581`, matching the design baseline.

```bash
TZ=UTC python3 scripts/dry_run_ai_promotion.py \
  --db /workspace/kitchen-genome/data_store/kg_service.db \
  --raw-dir /workspace/kitchen-genome/data_store/raw \
  --now 2026-11-25T00:00:00Z
```

| Metric | Value |
| --- | ---: |
| Unlinked before → after | 225 → 192 |
| Cohort (label + chef + venue) | 117 |
| Cohort matched to retained source | 115 |
| **Cohort reviews contributing public affiliation evidence** | **24 / 117** |
| Cohort annotations attached without an affiliation | 5 |
| New supports / new AI link records / new stints | 40 / 24 / 19 |
| Guard-list hits | 0 |
| Previously attached supports withdrawn | 1 |
| Second pass | unchanged, 0 new anything |

Cohort skip histogram (`cohort_skips`):
`temporary 33, institution 27, contest 6, no_relation 6, chef_unresolved 5, invalid_endpoint 5,
no_clause 4, unsafe_source 2`. Full backlog histogram:
`{"chef_unresolved":5,"contest":6,"institution":27,"invalid_endpoint":5,"no_chef":6,"no_clause":4,"no_relation":6,"no_venue":12,"temporary":33,"unsafe_source":3,"unsupported_label":85}`.

The named examples behave as intended except two, both with documented reasons:

- `강민구 | SEOK` / `세옥` — both opening reviews promoted onto one Paris branch
  (`city=파리, country=FR`), one curated chef identity.
- `샘킴 | 오스테리아 샘킴`, `김희은 | 에그앤플라워`, `박효남 | 콩두 명동` — the reservation-roster
  cells promoted, including the `오스테리아 샘킴(샘킴)` overlap.
- `우정욱 | 가지가지` — `unsafe_source` (port 8888) before the relation is evaluated; the quote
  also calls 가지가지 a `분식 브랜드`.
- `이준 | 도우룸 광화문` — `chef_unresolved`: the cell is a roster pair, but the retained source
  does not disambiguate this 이준 from the established 스와니예 identity.
- Pop-ups (`오닐`, `오닐(ONIL)`, `DOORI`, `주052`), hotel groups, corporations, department stores,
  TV shows and the port-8888 sources all remain unlinked.

## Findings

### F1 — High — Coverage target missed: 24 / 117, and 40 is not reachable under the guards

Evidence: the table above. The cohort decomposes into 60 guard entries and 57 others:

| Cohort bucket | Count | Disposition |
| --- | ---: | --- |
| Published affiliation evidence | 24 | moved |
| Annotation attached, no affiliation | 5 | moved |
| Temporary (all `오닐`/`DOORI`/`주052` pop-ups) | 33 | must stay unlinked |
| Institution (hotel groups, corporations, TV, research, brands) | 27 | must stay unlinked |
| Contest (award/ranking only; CONTEST gate blocks the new branch) | 6 | blocked |
| No relation (award/ranking/bare possessive/mentorship) | 6 | blocked |
| No clause (venue absent from the quote) | 4 | blocked |
| Unsafe source (`seoul.co.kr:8888`) | 2 | blocked |
| Chef unresolved | 5 | 1 fixable (F2), 4 need antecedent/guards |
| Invalid endpoint | 5 | 2 single-char `산`; 3 generic/identical-name contest |

29 entries move today (24 published + 5 annotation-only). Only **7** of the remaining 28 could
move even with new safe handling (F2's former elision, a coordinated former list, two
short-name/pronoun antecedent cases, two narrow single-character-venue exceptions), for a safe
ceiling of ~36 moved / ~31 published. **Reaching 40 published would require admitting at least 9
entries from the TEMPORARY, INSTITUTION, CONTEST, `no_clause` or port-8888 buckets** — i.e. exactly
the guards the task forbids weakening.

Recommendation: keep the gates. Either accept the documented shortfall (as
`AI_PROMOTION_COVERAGE_DESIGN.md` already does) or take a separate product decision to relax a
named guard; do not tune the binder toward 40.

### F2 — Medium — Subject-elided former/training claims never reach attribution

`daemon/kg_ai/promotion_binding.py:578`:

```python
contextual = (len(sentences) == 1 and rel in ('opening', 'current')
              and _source_attribution(...))
```

The single-sentence/clipped attribution path only runs for `opening` and `current`. The design
(`AI_PROMOTION_COVERAGE_DESIGN.md`, "Subject in the preceding clause") requires that "a former or
training verb retains that relation", and the same attribution proof is available. Consequence:
`rev_e6020979953a` (`이은지 | 정식(Jungsik)`, quote
`2016년 … '정식(Jungsik)'의 총괄 페이스트리 셰프로 합류해 …`) parses to `former` with
`_source_attribution(...) == True` but is reported `chef_unresolved`.

Measured impact: relaxing only this condition in memory promotes that review onto the Jungsik
branch with the same chef identity as the Lysée support, and moves the backlog 192 → 191
(cohort 24 → 25). The other four `chef_unresolved` reviews stay unresolved for independent
reasons: `rev_33e770b48af4`'s preceding sentence mentions `팝업` (correct temporary guard),
`rev_a74b6ce6e825` refers back with `신 셰프`, and `rev_91d4b61fb984` with the pronoun `그는` two
sentences after the name.

Suggested fix: allow all label-valid relations in the contextual branch, e.g.
`rel in allowed`, keeping the existing `_source_attribution` evidence requirement.

### F3 — Medium — A valid existing support is withdrawn on a duplicated article excerpt

`daemon/kg_ai/promotion.py:65`:

```python
if text.count(quote) != 1:
    return None
```

`rev_289b78648e03` (`박정현 | 아토보이`, quote
`박 셰프가 운영하는 캐주얼 한식당 아토보이(Atoboy)`) appears **twice** in the retained
celebinfluencernews listing page: the full article is repeated verbatim, and both occurrences are
named `박정현(JP·Junghyun Park)`. Because `_context()` refuses any non-unique occurrence,
`_short_subject` cannot resolve `박 셰프`, `_parse` returns `chef_unresolved`, and
`_retained_binding` cannot help (the legacy support has no `binding_receipt`). The valid support
is withdrawn (`withdrawn_supports: 1`).

This is a regression: the same review and quote were published before this branch. Selecting the
occurrence via the extraction span (971–1002 in this case, which matches the quote directly)
restores the row (`current`, `resolved_chef_text=박정현`). Suggested fix: when
`text.count(quote) > 1`, use `span_start/span_end` to pick the occurrence (or accept repeated
occurrences only when their attribution context is identical); retain the design's rejection of
genuinely conflicting repeated spans.

### F4 — Low — Venue-only sweep publishes `no_relation` annotations as AI news

`daemon/kg_ai/promotion.py:540-557` reattaches eligible unlinked annotations to a single matching
holder even without a graph edge. `no_relation` is in the eligible set, so
`rev_fee510f69783` (`Mingoo Kang | Seok`, quote
`His first European venture followed in 2024 with Sétopa … and Seok in 2026 – both in the French
capital.`) is moved onto the SEOK holder and rendered by `scripts/build_pages.py:42-47` under
"AI 검토 소식", although the design says this review "must stay unlinked as `no_relation`" and the
statement is future-tense, not a completed opening.

It creates no career link, but it publishes a tentative quote on the venue page with an
`opening` label. Suggested fix: exclude `opening`/`career` annotations whose reason is
`no_relation` (or reuse the future/tentative reject cues on the venue-only path), and keep them
in `ai_reviewed_unlinked`. Add a regression test that asserts the holder's `ai_reviewed` list and
the rendered page stay empty for `rev_fee510f69783`.

### F5 — Low — `restaurant_wording` can be satisfied by an award name

`daemon/kg_ai/promotion_binding.py:500` sets `restaurant_wording=bool(OUTLET.search(work))`, and
`OUTLET` includes `다이닝`. For `rev_25620f7264c2`
(`'파인 다이닝 러버스상(Fine Dining Lovers Award)'은 일본 도쿄 솔루나(SOLUNA)의 아이 이치노세
(Ai Ichinose) 셰프가 각각 수상했다.`) the award name satisfies the CONTEST gate's
restaurant-wording requirement, so a new branch is created from an award sentence. The resulting
`current` affiliation is factually supported by the explicit `SOLUNA의 … 셰프` apposition, so this
is not a false link, but the guard is being satisfied by an award title rather than outlet
wording. Suggested fix: strip award/product names before testing `restaurant_wording`, or require
an outlet word immediately adjacent to the venue.

### F6 — Low — “Together/couple” attribution can reach beyond the pair sentence

`daemon/kg_ai/promotion_binding.py:451-464`: once `부부|두 셰프|함께` appears in the quote, the
rule accepts a venue found anywhere in the accumulated sentences from the preceding ~620
characters. In a long multi-restaurant profile this could pair a couple with a venue mentioned in
a neighbouring restaurant's paragraph. No such false link occurs in this cohort (all three uses
were checked: `가겐`, `피오또`, both against their pair sentences), but the window is broader than
the evidence. Suggested fix: require the venue in the pair sentence itself or the immediately
preceding one, or require the pair sentence to contain the leadership/operating verb.

### F7 — Info — Publish-time venue guard ignores the non-outlet tail

`daemon/kg_ai/review.py:251` skips `site.match()` when `promotion._venue_reason(venue)` fires, but
`_venue_reason` only inspects the venue string. A quote such as `Chef X runs Celeste Hotel` still
attaches the annotation to a `Celeste` holder at publication time; `_parse` later rejects it via
the tail check, so no career link is produced, but the annotation is published as an AI news card.
No occurrence in this seed. Suggested fix is to run the same non-outlet tail check in
`publish()`, or leave as-is and document the annotation-only behavior.

## Verification detail

**False links.** All 40 new supports were matched to their quotes, branches and chef identities.
The `Mingoo Kang`/`강민구` spellings resolve to one curated chef (`chf_55abec499d07`); `SEOK` and
`세옥` resolve to one Paris branch; the Seoul `이준` used for 스와니예 is the existing curated
`Jun Lee` identity, not a duplicate. `Nobu` is correctly a former stint (the quote says "before
working at Nobu in Miami and the Bahamas"). No promoted pair says anything other than a
label-compatible relation.

**Guard preservation.** All 33 `temporary` cohort entries are `오닐`/`오닐(ONIL)`/`DOORI`/`주052`
pop-ups. All 27 `institution` entries are hotel groups, corporations, hotels-as-employer, TV
programs, research institutes, `라 리스트`, `오덴세` or `더본코리아`. The positive hotel-restaurant
controls kept working (`봉래헌`, `호빈`). `EXCLUDED_POPUPS`, `INSTITUTION`, `POPUP`, `TEMPORARY`,
`CONTEST`, `REJECT` and `FORMER_TITLE` are all applied on the new paths; the new Korean/English
relation matchers only run after `_reject_reason` and `_venue_reason`.

**Idempotency.** The replay's second pass writes the same seed bytes, audit bytes and skip rows and
adds no supports, links, upgrades or publication events. Re-running the venue-only sweep is a
no-op because the annotation is already on its holder (`_move` short-circuits when
`before is holder`).

**Rollback.** `reconcile_withdrawal_skips()` rewrites the private table after a rollback, preserving
unchanged reasons by `(review_id, extraction_id, quote_key)` fingerprint and full-entry JSON and
marking newly withdrawn entries `tombstoned`. `promotion.withdraw()` recomputes holders, supports
and links from the cached snapshot. The new tests cover a rollback log entry, a malformed review
ID, a duplicate review ID and the empty-backlog path.

**Site guards.** `grep` finds no consumer of `ai_promotion_skip` / `promotion_skips` /
`binding_receipt` in the page, feed or `data.js` builders. The rendered-output test asserts those
strings, plus `body_fingerprint` and `model_meta`, are absent from KO/EN pages, RSS and `data.js`,
and that `noindex` is absent. `SHOW_VERDICTS`, `SHOW_NEWSLETTER`, Instagram/proxy/egress and the
zecbot server are untouched.

**Tests.** 82 new/parameterized coverage tests in `tests/test_ai_promotion.py` exercise literal
bindings, unbound pairs, guard venues, elided bilingual openings, short names, source attribution,
source policy, span mapping, unsafe sources, the skip snapshot and rollback. Migration expectations
were extended to `0017_ai_promotion_skip.sql` in all five pinned-schema tests.

## Recommendation

1. Fix F2 (one-line change) and F3 (occurrence selection) before the next backfill; both are
   evidence-preserving and restore one cohort review and one existing support respectively.
2. Decide F4 explicitly: either keep `no_relation` annotations off holder pages or update the
   design wording that says `rev_fee510f69783` stays unlinked.
3. Do not pursue 40 by relaxing TEMPORARY/INSTITUTION/CONTEST/unsafe-source guards. Report the
   actual result (24/117, 0 guard hits) and the ceiling in the PR body.

## Resolution (2026-11-25)

All findings are addressed on `feat/ai-promote-unlinked-coverage`; the build results in
`AI_PROMOTION_COVERAGE_DESIGN.md` and the PR body carry the replayed numbers.

| Finding | Resolution |
| --- | --- |
| F1 | Guards retained. The replay reports 24/117, 0 guard-list hits, and the ceiling analysis; the 40 target is not reachable without weakening a named guard. |
| F2 | The contextual branch now accepts every label-valid relation. `rev_e6020979953a` publishes (`chef_unresolved` 5 → 4). |
| F3 | `_context()` selects the extraction's occurrence when the article is repeated; repeated text is accepted only when all occurrences share the same local context. `rev_289b78648e03` is no longer withdrawn (`withdrawn_supports: 0`). |
| F4 | The venue-only sweep excludes `opening`/`career` annotations whose reason is `no_relation`; `rev_fee510f69783` stays unlinked and off the holder page. |
| F5 | `restaurant_wording` strips award titles and only counts outlet wording near the venue; `rev_25620f7264c2` is blocked by CONTEST rather than creating a branch. |
| F6 | Couple/`함께` attribution requires an operating/leadership verb in the pair sentence and the venue in that sentence or the immediately preceding one. |
| F7 | `review.publish()` applies the same non-outlet tail guard as `_parse()`. |

Replayed result: unlinked 225 → 194; 40 supports; 18 new stints; 24 new AI link records;
`withdrawn_supports: 0`; second pass unchanged. `TZ=UTC python3 -m pytest tests` →
**892 passed, 1 skipped, 317 subtests passed**.
