ALTER TABLE branch ADD COLUMN country_code TEXT CHECK (country_code GLOB '[A-Z][A-Z]');

CREATE TABLE chef_location (
  claim_id TEXT PRIMARY KEY REFERENCES claim(claim_id),
  chef_id TEXT NOT NULL REFERENCES chef(chef_id),
  country_code TEXT CHECK (country_code GLOB '[A-Z][A-Z]'),
  city TEXT,
  valid_from TEXT,
  valid_to TEXT,
  basis TEXT NOT NULL,
  employment_id TEXT REFERENCES employment(employment_id),
  CHECK (country_code IS NOT NULL OR city IS NOT NULL)
);

CREATE TABLE target_entity (
  target_id TEXT PRIMARY KEY REFERENCES target(target_id),
  chef_id TEXT REFERENCES chef(chef_id),
  restaurant_id TEXT REFERENCES restaurant(restaurant_id),
  branch_id TEXT REFERENCES branch(branch_id),
  CHECK ((chef_id IS NOT NULL AND restaurant_id IS NULL AND branch_id IS NULL)
      OR (chef_id IS NULL AND restaurant_id IS NOT NULL))
);

CREATE TABLE guide_selection (
  selection_id TEXT PRIMARY KEY,
  scope TEXT NOT NULL,
  country_code TEXT NOT NULL CHECK (country_code GLOB '[A-Z][A-Z]'),
  edition_key TEXT NOT NULL,
  guide_year INTEGER CHECK (guide_year BETWEEN 1900 AND 9999),
  observed_at TEXT NOT NULL,
  UNIQUE (scope, country_code, edition_key)
);

CREATE TABLE guide_listing (
  selection_id TEXT NOT NULL REFERENCES guide_selection(selection_id),
  branch_id TEXT NOT NULL REFERENCES branch(branch_id),
  stars INTEGER NOT NULL CHECK (typeof(stars)='integer' AND stars BETWEEN 1 AND 3),
  tier INTEGER GENERATED ALWAYS AS (stars) VIRTUAL,
  source_kind TEXT NOT NULL,
  source_url TEXT NOT NULL,
  fetch_id TEXT REFERENCES fetch_record(fetch_id),
  locator TEXT NOT NULL,
  observed_at TEXT NOT NULL,
  observation_fingerprint TEXT NOT NULL,
  PRIMARY KEY (selection_id, branch_id, source_kind, source_url, observation_fingerprint)
);
CREATE INDEX guide_listing_current ON guide_listing(selection_id, branch_id, observed_at DESC);
CREATE TRIGGER guide_listing_no_update BEFORE UPDATE ON guide_listing
BEGIN SELECT RAISE(ABORT, 'guide observations are append-only'); END;
CREATE TRIGGER guide_listing_no_delete BEFORE DELETE ON guide_listing
BEGIN SELECT RAISE(ABORT, 'guide observations are append-only'); END;

CREATE TABLE guard_lane (
  scope TEXT NOT NULL,
  lane TEXT NOT NULL,
  signature TEXT,
  failures INTEGER NOT NULL DEFAULT 0,
  fingerprint TEXT,
  stalled INTEGER NOT NULL DEFAULT 0,
  last_committed TEXT,
  reason TEXT,
  PRIMARY KEY (scope, lane)
);
CREATE TABLE guard_item (
  lane TEXT NOT NULL,
  item_key TEXT NOT NULL,
  attempts INTEGER NOT NULL DEFAULT 0,
  reason TEXT,
  PRIMARY KEY (lane, item_key)
);
ALTER TABLE ai_request ADD COLUMN provider TEXT;
ALTER TABLE ai_request ADD COLUMN cache_hit_reads INTEGER NOT NULL DEFAULT 0;

CREATE TABLE outcome_first (
  kind TEXT NOT NULL CHECK (kind IN ('resolved_chef','verified_link')),
  entity_id TEXT NOT NULL,
  first_at TEXT NOT NULL,
  PRIMARY KEY (kind, entity_id)
);

CREATE TABLE web_traffic_day (
  day TEXT NOT NULL,
  route TEXT NOT NULL,
  requests INTEGER NOT NULL DEFAULT 0,
  bytes_in INTEGER NOT NULL DEFAULT 0,
  bytes_out INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (day, route)
);

CREATE TABLE ai_cache_day (
  day TEXT NOT NULL,
  request_key TEXT NOT NULL REFERENCES ai_request(request_key),
  hits INTEGER NOT NULL,
  PRIMARY KEY (day, request_key)
);

CREATE VIEW current_guide_listing AS
SELECT * FROM guide_listing AS listing
WHERE listing.rowid = (
  SELECT candidate.rowid FROM guide_listing AS candidate
  WHERE candidate.selection_id=listing.selection_id AND candidate.branch_id=listing.branch_id
  ORDER BY candidate.observed_at DESC,candidate.observation_fingerprint DESC,
           candidate.source_kind,candidate.source_url LIMIT 1
);

UPDATE branch SET country_code = CASE
  WHEN city IN ('서울','울산','부산','인천','대구','대전','광주','제주') THEN 'KR'
  WHEN city = '뉴욕' THEN 'US'
END WHERE country_code IS NULL;

INSERT OR IGNORE INTO target_entity(target_id,chef_id)
SELECT t.target_id, MIN(c.chef_id)
FROM target t JOIN chef c ON c.display_name=t.name_ko
WHERE t.kind='chef' GROUP BY t.target_id HAVING COUNT(*)=1;

INSERT OR IGNORE INTO target_entity(target_id,restaurant_id,branch_id)
SELECT t.target_id, MIN(b.restaurant_id), MIN(b.branch_id)
FROM target t JOIN alias a ON a.entity_type='restaurant' AND a.kind='legacy_id'
  AND a.alias=json_extract(t.attrs_json,'$.seed_id')
JOIN branch b ON b.restaurant_id=a.entity_id AND b.label='main'
WHERE t.kind='restaurant' GROUP BY t.target_id HAVING COUNT(*)=1;

CREATE TABLE model_document_sample (
  fetch_id TEXT NOT NULL REFERENCES fetch_record(fetch_id),
  version TEXT NOT NULL,
  disposition TEXT NOT NULL CHECK (disposition IN ('completed','prefilter_miss','failed')),
  PRIMARY KEY (fetch_id, version)
);

CREATE TRIGGER guide_listing_ignore_replay BEFORE INSERT ON guide_listing
WHEN EXISTS (SELECT 1 FROM guide_listing WHERE selection_id=NEW.selection_id
  AND branch_id=NEW.branch_id AND source_kind=NEW.source_kind AND source_url=NEW.source_url
  AND observation_fingerprint=NEW.observation_fingerprint)
BEGIN SELECT RAISE(IGNORE); END;
