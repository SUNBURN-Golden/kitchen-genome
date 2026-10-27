-- 0003: active collection bot. Layers: RAW (files outside git, referenced here) -> EXTRACTION (label + epistemic) -> service data (ontology tables).
-- Nothing here is a fact. `extraction` rows are labelled candidates with evidence spans; facts only enter the ontology through claims.

-- The Phase-1 stage_checkpoint CHECK pins the stage names to the kg-daemon pipeline. The collection bot is a second Daemon with its own
-- stages (plan/fetch/extract/...), so the CHECK is relaxed by rebuilding the table (data preserved).
CREATE TABLE stage_checkpoint_v2 (
  run_id        TEXT NOT NULL REFERENCES daemon_run(run_id),
  stage         TEXT NOT NULL,
  seq           INTEGER NOT NULL,
  status        TEXT NOT NULL CHECK (status IN ('pending','running','done','failed','skipped')),
  attempts      INTEGER NOT NULL DEFAULT 0,
  idempotency_key TEXT,
  cursor_json   TEXT NOT NULL DEFAULT '{}',
  output_json   TEXT NOT NULL DEFAULT '{}',
  error         TEXT,
  started_at    TEXT,
  finished_at   TEXT,
  PRIMARY KEY (run_id, stage)
);
INSERT INTO stage_checkpoint_v2 SELECT run_id,stage,seq,status,attempts,idempotency_key,cursor_json,output_json,error,started_at,finished_at FROM stage_checkpoint;
DROP TABLE stage_checkpoint;
ALTER TABLE stage_checkpoint_v2 RENAME TO stage_checkpoint;

CREATE TABLE target (                                  -- what we look for: the initial list AND everything the bot discovers
  target_id      TEXT PRIMARY KEY,                     -- tgt_<hash(kind,canonical name)>
  kind           TEXT NOT NULL CHECK (kind IN ('chef','restaurant','region','cuisine','group')),
  name_ko        TEXT,
  name_en        TEXT,
  origin         TEXT NOT NULL CHECK (origin IN ('seed','discovered')),
  discovered_via TEXT,                                 -- crawl_job path or url that surfaced it
  discovered_at  TEXT NOT NULL,
  status         TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','candidate','rejected')),
  ref_chef_id    TEXT, ref_restaurant_id TEXT,         -- link into the ontology when resolved
  attrs_json     TEXT NOT NULL DEFAULT '{}'            -- region/cuisine/etc. context
);
CREATE INDEX idx_target_kind ON target(kind, origin);

CREATE TABLE target_alias (
  target_id TEXT NOT NULL REFERENCES target(target_id),
  alias     TEXT NOT NULL,
  lang      TEXT NOT NULL DEFAULT 'ko',
  PRIMARY KEY (target_id, alias)
);
CREATE INDEX idx_target_alias ON target_alias(alias);

CREATE TABLE crawl_job (                               -- the work queue; idempotent by job_id = hash(path, url|query)
  job_id         TEXT PRIMARY KEY,
  path           TEXT NOT NULL CHECK (path IN ('keyword_search','directory','feed','sitemap','entity_followup','official_chain','reverse_gap','pagination','attachment')),
  pool           TEXT NOT NULL CHECK (pool IN ('backfill','discovery','refresh')),
  url            TEXT,
  query          TEXT,
  target_id      TEXT REFERENCES target(target_id),
  gap_id         TEXT,
  depth          INTEGER NOT NULL DEFAULT 0,
  parent_job_id  TEXT,
  relevance      REAL NOT NULL DEFAULT 0.5,
  priority       REAL NOT NULL DEFAULT 0,
  state          TEXT NOT NULL DEFAULT 'queued' CHECK (state IN ('queued','done','failed','skipped','blocked','dropped')),
  attempts       INTEGER NOT NULL DEFAULT 0,
  last_error     TEXT,
  not_before     TEXT,
  created_at     TEXT NOT NULL,
  finished_at    TEXT
);
CREATE INDEX idx_job_state ON crawl_job(state, pool, priority DESC);

CREATE TABLE domain_profile (                          -- source evaluator output; promotion grows the registry
  host           TEXT PRIMARY KEY,
  state          TEXT NOT NULL DEFAULT 'trial' CHECK (state IN ('trial','promoted','deprioritized','blocked')),
  in_initial_registry INTEGER NOT NULL DEFAULT 0,
  tier_guess     TEXT NOT NULL DEFAULT 'search_lead',
  robots_ok      INTEGER,
  crawl_delay    REAL,
  trial_fetches  INTEGER NOT NULL DEFAULT 0,
  trial_ok       INTEGER NOT NULL DEFAULT 0,
  s_relevance    REAL, s_novelty REAL, s_extractability REAL, s_originality REAL, s_permission REAL,
  score          REAL,
  yield_extractions INTEGER NOT NULL DEFAULT 0,
  fail_streak    INTEGER NOT NULL DEFAULT 0,
  reason         TEXT,
  first_seen_at  TEXT NOT NULL,
  evaluated_at   TEXT
);

CREATE TABLE fetch_record (                            -- one row per fetch attempt; raw_ref points OUTSIDE git
  fetch_id       TEXT PRIMARY KEY,
  job_id         TEXT,
  url            TEXT NOT NULL,
  canonical_url  TEXT NOT NULL,
  host           TEXT NOT NULL,
  method         TEXT NOT NULL CHECK (method IN ('feed','api','html','jsonld','browser','pdf','ocr','sitemap')),
  http_status    INTEGER,
  outcome        TEXT NOT NULL CHECK (outcome IN ('ok','fail_login_page','fail_empty','fail_blocked','fail_robots','fail_http','fail_network','fail_type','fail_banned')),
  content_type   TEXT,
  bytes          INTEGER,
  content_hash   TEXT,
  raw_ref        TEXT,
  text_ref       TEXT,                                 -- extracted plain text (also outside git)
  text_chars     INTEGER,
  page_no        INTEGER,
  parent_fetch_id TEXT,
  fetcher_version TEXT NOT NULL,
  extractor_version TEXT NOT NULL,
  published_at   TEXT,
  fetched_at     TEXT NOT NULL,
  extracted_at   TEXT,                                 -- NULL = raw stored but not yet labelled (extract stage); re-extracted when extractor_version changes
  error          TEXT
);
CREATE INDEX idx_fetch_url ON fetch_record(canonical_url, fetched_at);
CREATE INDEX idx_fetch_host ON fetch_record(host, outcome);

CREATE TABLE extraction (                              -- LABEL layer. label = what is said; epistemic = how much we may believe it
  extraction_id  TEXT PRIMARY KEY,                     -- hash(fetch content_hash, span, label, extractor_version) => idempotent
  fetch_id       TEXT NOT NULL REFERENCES fetch_record(fetch_id),
  canonical_url  TEXT NOT NULL,
  host           TEXT NOT NULL,
  label          TEXT NOT NULL CHECK (label IN ('career','employment_claim','opening','relocation','closure','rename','menu','collab','mentorship_claim')),
  epistemic      TEXT NOT NULL CHECK (epistemic IN ('fact','claim','inference','unverified')),
  chef_text      TEXT,
  restaurant_text TEXT,
  org_text       TEXT,                                 -- the OTHER restaurant/group in a career statement
  role_text      TEXT,
  date_text      TEXT,
  tenure_from    TEXT, tenure_to TEXT,
  span_start     INTEGER NOT NULL,
  span_end       INTEGER NOT NULL,
  quote          TEXT NOT NULL CHECK (length(quote) <= 200),
  extractor      TEXT NOT NULL,                        -- 'rule:<name>' | 'model:<label>'
  extractor_version TEXT NOT NULL,
  ambiguity      TEXT,                                 -- why a model/human is needed; NULL = rules were sure
  resolved_chef_target TEXT, resolved_restaurant_target TEXT, resolved_org_target TEXT,
  resolve_note   TEXT,
  published_at   TEXT,
  created_at     TEXT NOT NULL
);
CREATE INDEX idx_extraction_label ON extraction(label, epistemic);
CREATE INDEX idx_extraction_url ON extraction(canonical_url);

CREATE TABLE doc_target (                              -- which targets a fetched document mentions (gap classification needs 'we have a source, it just was not extracted')
  fetch_id  TEXT NOT NULL REFERENCES fetch_record(fetch_id),
  target_id TEXT NOT NULL REFERENCES target(target_id),
  mentions  INTEGER NOT NULL,
  PRIMARY KEY (fetch_id, target_id)
);
CREATE INDEX idx_doc_target ON doc_target(target_id);

CREATE TABLE gap (                                     -- an empty required field becomes a job; AI never fills it
  gap_id         TEXT PRIMARY KEY,                     -- hash(target, field)
  target_id      TEXT NOT NULL REFERENCES target(target_id),
  field          TEXT NOT NULL,
  klass          TEXT NOT NULL DEFAULT 'unclassified' CHECK (klass IN ('unclassified','not_extracted','absent_in_source','inaccessible','conflict','resolved')),
  klass_reason   TEXT,
  state          TEXT NOT NULL DEFAULT 'open' CHECK (state IN ('open','resolved')),
  opened_at      TEXT NOT NULL,
  resolved_at    TEXT,
  resolved_by_extraction TEXT REFERENCES extraction(extraction_id),
  resolved_value TEXT,
  attempts       INTEGER NOT NULL DEFAULT 0,
  last_job_at    TEXT
);
CREATE INDEX idx_gap_state ON gap(state, klass);

CREATE TABLE extractor_alert (                         -- broken-extractor detection
  alert_id       TEXT PRIMARY KEY,
  host           TEXT NOT NULL,
  canonical_url  TEXT,
  kind           TEXT NOT NULL CHECK (kind IN ('yield_drop','text_collapse','structure_change','login_wall')),
  detail         TEXT NOT NULL,
  baseline_json  TEXT, observed_json TEXT,
  state          TEXT NOT NULL DEFAULT 'open' CHECK (state IN ('open','acknowledged')),
  created_at     TEXT NOT NULL
);

CREATE TABLE budget_ledger (                           -- separate budgets per pool; discovery has a reserved share
  entry_id  INTEGER PRIMARY KEY AUTOINCREMENT,
  cycle_id  TEXT NOT NULL,
  pool      TEXT NOT NULL CHECK (pool IN ('backfill','discovery','refresh')),
  kind      TEXT NOT NULL,                             -- 'http','ocr_page','browser'
  units     REAL NOT NULL,
  job_id    TEXT,
  at        TEXT NOT NULL
);

CREATE TABLE field_history (                           -- observed value changes (old-vs-new), detected not overwritten
  history_id     INTEGER PRIMARY KEY AUTOINCREMENT,
  subject_id     TEXT NOT NULL,                        -- target_id
  field          TEXT NOT NULL,                        -- 'affiliation','status','opened'
  old_value      TEXT, new_value TEXT,
  old_extraction TEXT, new_extraction TEXT,
  published_at   TEXT,                                 -- when the source says it happened/was posted
  detected_at    TEXT NOT NULL,
  latency_hours  REAL
);

CREATE TRIGGER trg_extraction_no_delete BEFORE DELETE ON extraction
BEGIN SELECT RAISE(ABORT, 'append-only: extraction rows are never deleted'); END;
CREATE TRIGGER trg_fetch_record_no_delete BEFORE DELETE ON fetch_record
BEGIN SELECT RAISE(ABORT, 'append-only: fetch_record rows are never deleted'); END;
CREATE TRIGGER trg_field_history_no_delete BEFORE DELETE ON field_history
BEGIN SELECT RAISE(ABORT, 'append-only: field_history rows are never deleted'); END;
