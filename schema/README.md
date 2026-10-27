# schema/ — service DB migrations

SQLite / Cloudflare D1–compatible SQL. Applied in filename order by `daemon/kg_daemon/db.py::migrate` (each file's SHA-256 is pinned in `schema_migration`; editing an applied file is an error — add a new numbered file instead).

| File | Contents |
|---|---|
| `0001_core_ontology.sql` | sources, documents, evidence, claims (status layer), chef / restaurant / branch / role / alias, employment, status events, menu events, mentorship assertions, conflicts, correction history, review queue, `v_overlap`, append-only triggers |
| `0002_daemon_runtime.sql` | daemon_run, stage_checkpoint, run_lock, failure_queue, cost_ledger, change_event, candidate_fact, service_output |
| `0003_collection_bot.sql` | target, target_alias, crawl_job, domain_profile, fetch_record, extraction, doc_target, gap, extractor_alert, budget_ledger, field_history (active collection bot) |

Design: [`docs/data/ONTOLOGY.md`](../docs/data/ONTOLOGY.md), [`docs/data/DAEMON.md`](../docs/data/DAEMON.md).

The database file itself (default `data_store/kg_service.db`) and the raw store (`data_store/raw/`) are **git-ignored** and hold the only copy of collected raw material. Never commit them; tests use in-memory DBs and the non-identifying fixtures in `tests/fixtures/`.
