CREATE TABLE career_assertion (
  assertion_id TEXT PRIMARY KEY,
  fetch_id TEXT NOT NULL REFERENCES fetch_record(fetch_id),
  canonical_url TEXT NOT NULL,
  publisher_group TEXT NOT NULL,
  origin_group TEXT NOT NULL,
  independent_eligible INTEGER NOT NULL CHECK (independent_eligible IN (0,1)),
  published_at TEXT,
  content_hash TEXT NOT NULL,
  quote_key TEXT,
  span_start INTEGER,
  span_end INTEGER,
  quote TEXT CHECK (quote IS NULL OR length(quote) BETWEEN 1 AND 200),
  extractor_version TEXT NOT NULL,
  model TEXT,
  request_id TEXT REFERENCES ai_request(request_key),
  language TEXT NOT NULL,
  chef_text TEXT,
  venue_text TEXT,
  relation TEXT CHECK (relation IN ('current','former','training','opening')),
  role_text TEXT,
  role_id TEXT REFERENCES role(role_id),
  from_text TEXT,
  to_text TEXT,
  valid_from TEXT,
  valid_to TEXT,
  state TEXT NOT NULL CHECK (state IN ('source_claim','review')),
  chef_id TEXT REFERENCES chef(chef_id),
  branch_id TEXT REFERENCES branch(branch_id),
  previous_branch_id TEXT REFERENCES branch(branch_id),
  mentor_id TEXT REFERENCES chef(chef_id),
  mentor_text TEXT,
  previous_venue_text TEXT,
  claim_id TEXT REFERENCES claim(claim_id),
  evidence_id TEXT REFERENCES evidence(evidence_id),
  employment_id TEXT REFERENCES employment(employment_id),
  CHECK (state <> 'source_claim' OR (chef_id IS NOT NULL AND branch_id IS NOT NULL
    AND claim_id IS NOT NULL AND evidence_id IS NOT NULL AND quote IS NOT NULL
    AND span_start IS NOT NULL AND span_end IS NOT NULL AND span_start>=0 AND span_end-span_start=length(quote)))
);
CREATE INDEX career_assertion_path ON career_assertion(branch_id, chef_id, relation);
CREATE INDEX career_assertion_chef ON career_assertion(chef_id, branch_id);
CREATE INDEX career_assertion_snapshot ON career_assertion(canonical_url, content_hash, extractor_version);

CREATE TRIGGER career_assertion_no_update BEFORE UPDATE ON career_assertion
BEGIN SELECT RAISE(ABORT, 'career assertions are immutable'); END;
CREATE TRIGGER career_assertion_no_delete BEFORE DELETE ON career_assertion
BEGIN SELECT RAISE(ABORT, 'career assertions are immutable'); END;

CREATE TABLE career_document (
  fetch_id TEXT NOT NULL REFERENCES fetch_record(fetch_id),
  extractor_version TEXT NOT NULL,
  content_hash TEXT NOT NULL,
  language TEXT NOT NULL,
  body_fingerprint TEXT NOT NULL,
  PRIMARY KEY (fetch_id, extractor_version)
);

CREATE TABLE career_official_branch (
  branch_id TEXT NOT NULL REFERENCES branch(branch_id),
  host TEXT NOT NULL,
  fetch_id TEXT NOT NULL REFERENCES fetch_record(fetch_id),
  PRIMARY KEY (host,branch_id)
);

CREATE TABLE career_branch_context (
  branch_id TEXT PRIMARY KEY REFERENCES branch(branch_id),
  publisher_group TEXT NOT NULL
);
CREATE INDEX career_restaurant_name ON restaurant(name COLLATE NOCASE);
CREATE INDEX career_restaurant_name_en ON restaurant(name_en COLLATE NOCASE);
CREATE INDEX career_chef_name ON chef(display_name COLLATE NOCASE);
CREATE INDEX career_chef_name_en ON chef(display_name_en COLLATE NOCASE);

CREATE TABLE chef_name_key (
  name_key TEXT NOT NULL,
  chef_id TEXT NOT NULL REFERENCES chef(chef_id),
  PRIMARY KEY (name_key, chef_id)
);

CREATE TABLE entity_redirect (
  redirect_id TEXT PRIMARY KEY,
  entity_type TEXT NOT NULL CHECK (entity_type IN ('chef','branch')),
  from_id TEXT NOT NULL,
  to_id TEXT NOT NULL,
  evidence_id TEXT NOT NULL REFERENCES evidence(evidence_id),
  reason TEXT NOT NULL,
  merged_at TEXT NOT NULL,
  reversed_at TEXT,
  CHECK (from_id <> to_id)
);
CREATE UNIQUE INDEX entity_redirect_active ON entity_redirect(entity_type, from_id) WHERE reversed_at IS NULL;

CREATE TABLE projection_site_id (
  branch_id TEXT NOT NULL REFERENCES branch(branch_id),
  kind TEXT NOT NULL CHECK (kind IN ('restaurant','kitchen')),
  site_id TEXT NOT NULL,
  PRIMARY KEY (branch_id,kind,site_id),
  UNIQUE (kind,site_id)
);

UPDATE global_work
SET state='pending',result_state=NULL,lease_token=NULL,lease_expires_at=NULL,reason=NULL,available_at=0
WHERE kind='extract' AND reason='awaiting_g3_extractor'
  AND state IN ('deferred','checkpointed','leased');
