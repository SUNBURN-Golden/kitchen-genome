-- 0004: structured profile facts read from a restaurant's OWN pages (official site, JSON-LD) and public booking pages.
-- One row = one field value with its evidence. Same epistemic rules as `extraction`: only fact/claim may fill a gap; nothing here is AI-filled.
CREATE TABLE profile_fact (
  fact_id        TEXT PRIMARY KEY,                     -- hash(fetch content_hash, field, value, extractor_version) => idempotent
  fetch_id       TEXT NOT NULL REFERENCES fetch_record(fetch_id),
  canonical_url  TEXT NOT NULL,
  host           TEXT NOT NULL,
  target_id      TEXT NOT NULL REFERENCES target(target_id),
  field          TEXT NOT NULL CHECK (field IN ('opened','chef','address','status','relocation','phone')),
  value          TEXT NOT NULL,
  epistemic      TEXT NOT NULL CHECK (epistemic IN ('fact','claim','inference','unverified')),
  source_kind    TEXT NOT NULL CHECK (source_kind IN ('official_site','official_jsonld','booking_page')),
  site_basis     TEXT NOT NULL,                        -- why we believe the site is the restaurant's own: seed | article_link | probe_verified
  quote          TEXT NOT NULL CHECK (length(quote) <= 200),
  extractor      TEXT NOT NULL,
  extractor_version TEXT NOT NULL,
  published_at   TEXT,
  created_at     TEXT NOT NULL
);
CREATE INDEX idx_profile_fact_target ON profile_fact(target_id, field);

-- official sites per target (discovered or seeded). verified=1 only when the page names the restaurant and looks like a restaurant site.
CREATE TABLE official_site (
  target_id   TEXT NOT NULL REFERENCES target(target_id),
  url         TEXT NOT NULL,
  host        TEXT NOT NULL,
  basis       TEXT NOT NULL CHECK (basis IN ('seed','article_link','probe_verified','booking')),
  verified    INTEGER NOT NULL DEFAULT 0,
  evidence    TEXT,
  found_at    TEXT NOT NULL,
  PRIMARY KEY (target_id, url)
);

-- a gap may be filled by a profile fact instead of a sentence extraction (exactly one of the two is set when resolved)
ALTER TABLE gap ADD COLUMN resolved_by_fact TEXT REFERENCES profile_fact(fact_id);

-- which fetches the profile extractor has already read, at which version (re-scan when the version changes; idempotent)
CREATE TABLE profile_scan (
  fetch_id   TEXT NOT NULL REFERENCES fetch_record(fetch_id),
  version    TEXT NOT NULL,
  facts      INTEGER NOT NULL,
  scanned_at TEXT NOT NULL,
  PRIMARY KEY (fetch_id, version)
);
