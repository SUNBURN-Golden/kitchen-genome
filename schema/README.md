# schema/ — service DB migrations

SQLite / Cloudflare D1–compatible SQL. Applied in filename order by `daemon/kg_daemon/db.py::migrate` (each file's SHA-256 is pinned in `schema_migration`; editing an applied file is an error — add a new numbered file instead).

| File | Contents |
|---|---|
| `0001_core_ontology.sql` | sources, documents, evidence, claims (status layer), chef / restaurant / branch / role / alias, employment, status events, menu events, mentorship assertions, conflicts, correction history, review queue, `v_overlap`, append-only triggers |
| `0002_daemon_runtime.sql` | daemon_run, stage_checkpoint, run_lock, failure_queue, cost_ledger, change_event, candidate_fact, service_output |
| `0003_collection_bot.sql` | target, target_alias, crawl_job, domain_profile, fetch_record, extraction, doc_target, gap, extractor_alert, budget_ledger, field_history (active collection bot) |
| `0004_profile_facts.sql` | profile_fact / profile_scan (official-site and booking profile extraction) |
| `0005_candidate_fact_types.sql` | additive candidate_fact type CHECK values |
| `0006_deepseek_collection.sql` | ai_request, web_daily_url/fetch, web_host_guard, ig_account_state/handle_day/follow_day, AI provenance columns |
| `0007_aux_robots_counter.sql` | separate daily robots.txt counter on `web_daily_fetch` |
| `0012_web_access_guard.sql` | expiring web access scopes, host block history, Retry-After and robots cache, Instagram post watermarks and checkpoints |

Design: [`docs/data/ONTOLOGY.md`](../docs/data/ONTOLOGY.md), [`docs/data/DAEMON.md`](../docs/data/DAEMON.md).

The database file itself (default `data_store/kg_service.db`) and the raw store (`data_store/raw/`) are **git-ignored** and hold the only copy of collected raw material. Never commit them; tests use in-memory DBs and the non-identifying fixtures in `tests/fixtures/`.
