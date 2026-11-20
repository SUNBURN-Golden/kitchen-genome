CREATE TABLE global_sweep (
  sweep_id TEXT PRIMARY KEY,
  tier INTEGER NOT NULL CHECK (tier BETWEEN 0 AND 3)
);

CREATE TABLE global_scope (
  scope TEXT PRIMARY KEY,
  label TEXT NOT NULL,
  url TEXT,
  locale TEXT NOT NULL,
  countries_json TEXT NOT NULL,
  city TEXT,
  wiki_json TEXT NOT NULL,
  announcement_year INTEGER
);

CREATE TABLE global_tier_pass (
  sweep_id TEXT NOT NULL REFERENCES global_sweep(sweep_id),
  tier INTEGER NOT NULL CHECK (tier BETWEEN 1 AND 3),
  scope TEXT NOT NULL REFERENCES global_scope(scope),
  disposition TEXT NOT NULL DEFAULT 'pending',
  first_pass_at REAL,
  last_success_at REAL,
  PRIMARY KEY (sweep_id, tier, scope)
);

CREATE TABLE global_work (
  job_key TEXT PRIMARY KEY,
  sweep_id TEXT NOT NULL REFERENCES global_sweep(sweep_id),
  tier INTEGER NOT NULL CHECK (tier BETWEEN 1 AND 3),
  kind TEXT NOT NULL CHECK (kind IN ('list','crosscheck','venue','discover','article','extract')),
  scope TEXT REFERENCES global_scope(scope),
  target TEXT NOT NULL,
  host TEXT NOT NULL,
  cursor TEXT NOT NULL,
  priority INTEGER NOT NULL,
  state TEXT NOT NULL DEFAULT 'pending'
    CHECK (state IN ('pending','leased','checkpointed','done','deferred','review')),
  result_state TEXT CHECK (result_state IN ('pending','done','deferred','review')),
  available_at REAL NOT NULL,
  lease_token TEXT,
  lease_expires_at REAL,
  attempts INTEGER NOT NULL DEFAULT 0,
  first_pass_at REAL,
  fetch_id TEXT REFERENCES fetch_record(fetch_id),
  reason TEXT
);
CREATE INDEX global_work_schedule ON global_work(sweep_id, tier DESC, priority DESC, available_at, job_key);
CREATE INDEX global_work_scope ON global_work(sweep_id, tier, scope, kind);
CREATE INDEX global_work_host ON global_work(host, state, lease_expires_at);

CREATE TABLE global_work_origin (
  job_key TEXT NOT NULL REFERENCES global_work(job_key),
  branch_id TEXT NOT NULL REFERENCES branch(branch_id),
  tier INTEGER NOT NULL CHECK (tier BETWEEN 1 AND 3),
  PRIMARY KEY (job_key, branch_id, tier)
);
CREATE INDEX global_origin_branch ON global_work_origin(branch_id);

CREATE TABLE global_work_edge (
  parent_key TEXT NOT NULL REFERENCES global_work(job_key),
  child_key TEXT NOT NULL REFERENCES global_work(job_key),
  PRIMARY KEY (parent_key, child_key)
);

CREATE TABLE global_page (
  url TEXT PRIMARY KEY,
  fetch_id TEXT NOT NULL REFERENCES fetch_record(fetch_id),
  final_url TEXT NOT NULL,
  etag TEXT,
  last_modified TEXT,
  checked_at REAL NOT NULL
);

CREATE TABLE global_announcement (
  scope TEXT NOT NULL REFERENCES global_scope(scope),
  fetch_id TEXT NOT NULL REFERENCES fetch_record(fetch_id),
  guide_year INTEGER NOT NULL,
  PRIMARY KEY (scope, fetch_id)
);

CREATE TABLE michelin_venue_identity (
  identity TEXT PRIMARY KEY,
  branch_id TEXT NOT NULL REFERENCES branch(branch_id)
);

CREATE TABLE michelin_branch_location (
  branch_id TEXT PRIMARY KEY REFERENCES branch(branch_id),
  country_code TEXT NOT NULL,
  name_key TEXT NOT NULL,
  city_key TEXT NOT NULL,
  address_key TEXT NOT NULL,
  address TEXT,
  official INTEGER NOT NULL CHECK (official IN (0,1))
);
CREATE INDEX michelin_location_lookup ON michelin_branch_location(country_code, name_key, city_key);

DROP VIEW current_guide_listing;
CREATE VIEW current_guide_listing AS
SELECT * FROM guide_listing AS listing
WHERE listing.rowid = (
  SELECT candidate.rowid FROM guide_listing AS candidate
  WHERE candidate.selection_id=listing.selection_id AND candidate.branch_id=listing.branch_id
  ORDER BY CASE candidate.source_kind WHEN 'michelin' THEN 0 WHEN 'michelin_announcement' THEN 1
    WHEN 'press_unattributed' THEN 3 WHEN 'press_mirror' THEN 4 ELSE 2 END,
    candidate.observed_at DESC,candidate.rowid DESC LIMIT 1
);

CREATE VIEW guide_roster_observation AS
SELECT listing.*, selection.scope, selection.country_code, selection.guide_year,
  CASE WHEN listing.source_kind='michelin' THEN 'official'
       WHEN selection.guide_year IS NULL THEN 'undated'
       WHEN EXISTS (
         SELECT 1 FROM current_guide_listing AS official
         JOIN guide_selection AS edition USING (selection_id)
         WHERE official.source_kind='michelin' AND official.branch_id=listing.branch_id
           AND edition.country_code=selection.country_code AND edition.guide_year=selection.guide_year
           AND official.stars=listing.stars
       ) THEN 'corroborates'
       WHEN EXISTS (
         SELECT 1 FROM current_guide_listing AS official
         JOIN guide_selection AS edition USING (selection_id)
         WHERE official.source_kind='michelin' AND official.branch_id=listing.branch_id
           AND edition.country_code=selection.country_code AND edition.guide_year=selection.guide_year
       ) THEN 'discrepancy'
       ELSE 'provisional' END AS disposition
FROM guide_listing AS listing JOIN guide_selection AS selection USING (selection_id);
