-- Kitchen Genome daemon runtime tables (docs/data/DAEMON.md). Same service DB as 0001.
PRAGMA foreign_keys = ON;

CREATE TABLE daemon_run (
  run_id        TEXT PRIMARY KEY,
  daemon        TEXT NOT NULL,                       -- 'kg-daemon'
  status        TEXT NOT NULL CHECK (status IN ('running','succeeded','failed','paused_cost_cap','paused_source','abandoned')),
  failed_stage  TEXT,
  trigger       TEXT NOT NULL DEFAULT 'manual',
  cost_cap_units REAL NOT NULL,
  cost_used_units REAL NOT NULL DEFAULT 0,
  resumed_from  TEXT REFERENCES daemon_run(run_id),
  started_at    TEXT NOT NULL,
  finished_at   TEXT
);

-- One row per (run, stage). 'done' rows are never re-executed on resume (idempotent resume).
CREATE TABLE stage_checkpoint (
  run_id        TEXT NOT NULL REFERENCES daemon_run(run_id),
  stage         TEXT NOT NULL CHECK (stage IN ('source_discovery','incremental_fetch','change_detection','extraction','entity_resolution','evidence_verification','state_history_update','service_output_recompute','quality_check')),
  seq           INTEGER NOT NULL,
  status        TEXT NOT NULL CHECK (status IN ('pending','running','done','failed','skipped')),
  attempts      INTEGER NOT NULL DEFAULT 0,
  idempotency_key TEXT,
  cursor_json   TEXT NOT NULL DEFAULT '{}',          -- stage-local resume cursor
  output_json   TEXT NOT NULL DEFAULT '{}',          -- small summary handed to later stages
  error         TEXT,
  started_at    TEXT,
  finished_at   TEXT,
  PRIMARY KEY (run_id, stage)
);

-- Single-flight: at most one live holder per name; expired leases can be taken over.
CREATE TABLE run_lock (
  lock_name   TEXT PRIMARY KEY,
  owner       TEXT NOT NULL,
  acquired_at TEXT NOT NULL,
  expires_at  TEXT NOT NULL
);

-- Items that exhausted bounded retries. Not retried automatically beyond max_attempts; a human or
-- `kg-daemon requeue` moves them back.
CREATE TABLE failure_queue (
  failure_id   TEXT PRIMARY KEY,
  run_id       TEXT REFERENCES daemon_run(run_id),
  stage        TEXT NOT NULL,
  item_key     TEXT,
  error        TEXT NOT NULL,
  attempts     INTEGER NOT NULL,
  state        TEXT NOT NULL DEFAULT 'open' CHECK (state IN ('open','requeued','dismissed')),
  created_at   TEXT NOT NULL,
  resolved_at  TEXT
);

CREATE TABLE cost_ledger (
  entry_id    INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id      TEXT NOT NULL REFERENCES daemon_run(run_id),
  stage       TEXT NOT NULL,
  kind        TEXT NOT NULL,                         -- 'http_request','llm_tokens_k','db_write'
  units       REAL NOT NULL,
  at          TEXT NOT NULL
);

-- Change detection output: a document whose content changed (or is new) since the last accepted version.
CREATE TABLE change_event (
  change_id    TEXT PRIMARY KEY,
  document_id  TEXT NOT NULL REFERENCES document(document_id),
  prev_document_id TEXT REFERENCES document(document_id),
  change_kind  TEXT NOT NULL CHECK (change_kind IN ('new','modified','unchanged')),
  run_id       TEXT REFERENCES daemon_run(run_id),
  state        TEXT NOT NULL DEFAULT 'pending' CHECK (state IN ('pending','extracted')),
  created_at   TEXT NOT NULL,
  UNIQUE (document_id)
);

-- Extraction output = CANDIDATE facts only. Nothing here is a fact until verification + state update.
CREATE TABLE candidate_fact (
  candidate_id    TEXT PRIMARY KEY,
  idempotency_key TEXT NOT NULL UNIQUE,
  document_id     TEXT REFERENCES document(document_id),
  source_id       TEXT NOT NULL REFERENCES source(source_id),
  fact_type       TEXT NOT NULL CHECK (fact_type IN ('chef_move','opening','closure','rename','relocation','mentorship','employment')),
  payload_json    TEXT NOT NULL,
  evidence_json   TEXT NOT NULL,                     -- {source_url, locator, quote, collected_at, effective_from}
  extractor       TEXT NOT NULL,                     -- 'rule:<name>' | 'ai:<model-label>'
  asserted_by     TEXT NOT NULL CHECK (asserted_by IN ('source','ai_inference')),
  resolution      TEXT NOT NULL DEFAULT 'pending' CHECK (resolution IN ('pending','resolved','needs_review','rejected')),
  resolved_chef_id TEXT REFERENCES chef(chef_id),
  resolved_branch_id TEXT REFERENCES branch(branch_id),
  verification    TEXT CHECK (verification IN ('verified','source_claim','ai_inference','unverified')),
  applied_claim_id TEXT REFERENCES claim(claim_id),
  created_at      TEXT NOT NULL
);
CREATE INDEX idx_candidate_state ON candidate_fact(resolution, applied_claim_id);

-- Service-facing recomputed outputs (what the site/API reads in later phases; no frontend in Phase 1).
CREATE TABLE service_output (
  output_key   TEXT NOT NULL,                        -- 'current_employment','restaurant_status_asof','alumni_of'
  subject_id   TEXT NOT NULL,
  as_of        TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  min_status   TEXT NOT NULL,                        -- lowest claim status admitted into this output
  computed_at  TEXT NOT NULL,
  run_id       TEXT REFERENCES daemon_run(run_id),
  PRIMARY KEY (output_key, subject_id, as_of)
);

CREATE TRIGGER trg_failure_queue_no_delete BEFORE DELETE ON failure_queue
BEGIN SELECT RAISE(ABORT, 'append-only: failure_queue rows are never deleted'); END;
CREATE TRIGGER trg_candidate_fact_no_delete BEFORE DELETE ON candidate_fact
BEGIN SELECT RAISE(ABORT, 'append-only: candidate_fact rows are never deleted'); END;
