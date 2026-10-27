-- Kitchen Genome service DB — core ontology (SQLite / Cloudflare D1 compatible).
-- Phase 1 (docs/data/ONTOLOGY.md). This DB is SEPARATE from the code repo and from the Pages site D1
-- (migrations/0001_users.sql). It lives under data_store/ (gitignored) or a private D1 database; only
-- schema + de-identified fixtures are committed.
--
-- Hard invariants (enforced here by triggers/CHECKs, and again by the daemon quality stage):
--   * Nothing is ever DELETEd from a fact table. Ends/corrections are new state + history rows.
--   * Every fact row carries a claim; a claim has a status in {verified, source_claim, ai_inference, unverified}.
--   * Evidence rows are immutable (source URL, locator, quote<=200 chars, collected_at, effective_from).
--   * Mentorship exists only as an explicit source assertion; co-employment is the derived view v_overlap.
-- Dates are ISO-8601 text: YYYY | YYYY-MM | YYYY-MM-DD (see *_precision). Intervals are [valid_from, valid_to).

PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS schema_migration (
  version     TEXT PRIMARY KEY,
  checksum    TEXT NOT NULL,
  applied_at  TEXT NOT NULL
);

-- ───────────────────────── sources, permissions, documents, evidence ─────────────────────────
CREATE TABLE source (
  source_id   TEXT PRIMARY KEY,                 -- registry key, e.g. 'instagram_public', 'press_rss'
  name        TEXT NOT NULL,
  kind        TEXT NOT NULL CHECK (kind IN ('official_site','press','rss','sitemap','social','guide','community','manual_clip','outreach','seed')),
  host        TEXT,
  tier        TEXT NOT NULL CHECK (tier IN ('official','press','specialist_blog','event_notice','social_lead','search_lead','human')),
  enabled     INTEGER NOT NULL DEFAULT 1,
  created_at  TEXT NOT NULL
);

CREATE TABLE source_policy (
  source_id          TEXT PRIMARY KEY REFERENCES source(source_id),
  collect_permission TEXT NOT NULL CHECK (collect_permission IN ('allowed','allowed_with_limits','manual_only','prohibited')),
  collectible_fields TEXT NOT NULL DEFAULT '[]',   -- JSON array
  unsupported_scope  TEXT NOT NULL DEFAULT '[]',   -- JSON array; stated, never hidden
  refresh_cadence    TEXT NOT NULL,
  retention_days     INTEGER,                      -- raw document retention; NULL = keep structured facts only
  quote_max_chars    INTEGER NOT NULL DEFAULT 200,
  fulltext_allowed   INTEGER NOT NULL DEFAULT 0,
  pause_on           TEXT NOT NULL DEFAULT '[]',   -- JSON array of stop signals (e.g. checkpoint, 429)
  registry_version   TEXT NOT NULL,
  updated_at         TEXT NOT NULL
);

CREATE TABLE document (
  document_id          TEXT PRIMARY KEY,
  source_id            TEXT NOT NULL REFERENCES source(source_id),
  url                  TEXT NOT NULL,
  canonical_url        TEXT NOT NULL,
  content_hash         TEXT NOT NULL,
  fetched_at           TEXT NOT NULL,
  published_at         TEXT,
  http_status          INTEGER,
  raw_ref              TEXT,                       -- pointer into the RAW STORE (outside git); NULL if not retained
  retention_expires_at TEXT,
  raw_purged_at        TEXT,                       -- raw blob purged; row + evidence quotes stay
  UNIQUE (source_id, canonical_url, content_hash)
);
CREATE INDEX idx_document_url ON document(canonical_url);

CREATE TABLE evidence (
  evidence_id    TEXT PRIMARY KEY,
  document_id    TEXT REFERENCES document(document_id),
  source_id      TEXT NOT NULL REFERENCES source(source_id),
  source_url     TEXT NOT NULL,
  locator_json   TEXT NOT NULL DEFAULT '{}',        -- {paragraph_idx, char_start, char_end} / {post_id} / {legacy_seed_field}
  quote          TEXT CHECK (quote IS NULL OR length(quote) <= 200),
  collected_at   TEXT NOT NULL,                     -- when WE read it
  effective_from TEXT,                              -- when the stated fact applies in the real world (NULL = not stated)
  published_at   TEXT,
  created_at     TEXT NOT NULL
);
CREATE INDEX idx_evidence_source ON evidence(source_url);

-- ───────────────────────── claims (status layer) ─────────────────────────
CREATE TABLE claim (
  claim_id        TEXT PRIMARY KEY,
  assertion_type  TEXT NOT NULL CHECK (assertion_type IN ('employment','employment_end','status_event','mentorship','alias','menu_event','attribute')),
  status          TEXT NOT NULL CHECK (status IN ('verified','source_claim','ai_inference','unverified')),
  created_by      TEXT NOT NULL,                    -- 'migration:seed', 'daemon:extraction', 'human:<name>'
  note            TEXT,
  created_at      TEXT NOT NULL,
  status_changed_at TEXT NOT NULL
);

CREATE TABLE claim_evidence (
  claim_id    TEXT NOT NULL REFERENCES claim(claim_id),
  evidence_id TEXT NOT NULL REFERENCES evidence(evidence_id),
  stance      TEXT NOT NULL DEFAULT 'supports' CHECK (stance IN ('supports','contradicts')),
  PRIMARY KEY (claim_id, evidence_id)
);

CREATE TABLE claim_status_history (
  history_id  INTEGER PRIMARY KEY AUTOINCREMENT,
  claim_id    TEXT NOT NULL REFERENCES claim(claim_id),
  from_status TEXT,
  to_status   TEXT NOT NULL,
  changed_at  TEXT NOT NULL,
  changed_by  TEXT NOT NULL,
  reason      TEXT
);

-- ───────────────────────── entities ─────────────────────────
CREATE TABLE chef (
  chef_id        TEXT PRIMARY KEY,                  -- opaque stable id, never reused
  display_name   TEXT NOT NULL,
  display_name_en TEXT,
  disambiguator  TEXT,                              -- homonym scope, e.g. 'mosu' (legacy chef_entity_id suffix)
  created_at     TEXT NOT NULL
);

CREATE TABLE restaurant (
  restaurant_id  TEXT PRIMARY KEY,
  name           TEXT NOT NULL,
  name_en        TEXT,
  created_at     TEXT NOT NULL
);

CREATE TABLE branch (                               -- Branch/Location: the unit that opens, moves, closes
  branch_id      TEXT PRIMARY KEY,
  restaurant_id  TEXT NOT NULL REFERENCES restaurant(restaurant_id),
  label          TEXT NOT NULL DEFAULT 'main',
  area           TEXT,
  city           TEXT,
  created_at     TEXT NOT NULL
);
CREATE INDEX idx_branch_restaurant ON branch(restaurant_id);

CREATE TABLE role (
  role_id    TEXT PRIMARY KEY,                      -- 'executive_chef','head_chef','sous_chef','pastry_chef','founding_member','owner_chef','trainee','alumnus_unspecified'
  label_ko   TEXT NOT NULL,
  label_en   TEXT NOT NULL
);

CREATE TABLE alias (                                -- alternate names, former names, romanizations, handles
  alias_id    TEXT PRIMARY KEY,
  entity_type TEXT NOT NULL CHECK (entity_type IN ('chef','restaurant','branch')),
  entity_id   TEXT NOT NULL,
  alias       TEXT NOT NULL,
  kind        TEXT NOT NULL CHECK (kind IN ('former_name','romanization','hanja','nickname','handle','legacy_id')),
  lang        TEXT,
  valid_from  TEXT,
  valid_to    TEXT,
  claim_id    TEXT REFERENCES claim(claim_id),
  created_at  TEXT NOT NULL,
  UNIQUE (entity_type, entity_id, alias, kind)
);
CREATE INDEX idx_alias_lookup ON alias(alias);

-- ───────────────────────── time-bound facts ─────────────────────────
CREATE TABLE employment (
  employment_id   TEXT PRIMARY KEY,
  chef_id         TEXT NOT NULL REFERENCES chef(chef_id),
  branch_id       TEXT REFERENCES branch(branch_id),
  employer_label  TEXT,                              -- when the employer is not (yet) resolved to a branch
  role_id         TEXT REFERENCES role(role_id),
  role_text       TEXT,                              -- source wording, kept verbatim
  tenure_text     TEXT,                              -- e.g. '4년' when only a duration is stated
  valid_from      TEXT,
  valid_from_precision TEXT CHECK (valid_from_precision IN ('year','month','day')),
  valid_to        TEXT,                              -- exclusive end
  valid_to_precision TEXT CHECK (valid_to_precision IN ('year','month','day')),
  -- 'open'    : source says ongoing / no end known as of last_confirmed_at (counts as current)
  -- 'known'   : valid_to is set
  -- 'unknown' : ended-or-unrecorded; NEVER presented as current
  valid_to_kind   TEXT NOT NULL CHECK (valid_to_kind IN ('open','known','unknown')),
  last_confirmed_at TEXT,
  state           TEXT NOT NULL DEFAULT 'active' CHECK (state IN ('active','closed','rejected')),
  claim_id        TEXT NOT NULL REFERENCES claim(claim_id),
  end_claim_id    TEXT REFERENCES claim(claim_id),
  seed_ref        TEXT,                              -- idempotency key for migrated rows
  created_at      TEXT NOT NULL,
  CHECK (branch_id IS NOT NULL OR employer_label IS NOT NULL),
  CHECK (valid_to_kind <> 'known' OR valid_to IS NOT NULL),
  CHECK (valid_to_kind = 'known' OR valid_to IS NULL),
  CHECK (valid_to IS NULL OR valid_from IS NULL OR valid_to >= valid_from)
);
CREATE UNIQUE INDEX idx_employment_seed_ref ON employment(seed_ref) WHERE seed_ref IS NOT NULL;
CREATE INDEX idx_employment_chef ON employment(chef_id);
CREATE INDEX idx_employment_branch ON employment(branch_id);

CREATE TABLE restaurant_status_event (
  event_id       TEXT PRIMARY KEY,
  branch_id      TEXT NOT NULL REFERENCES branch(branch_id),
  kind           TEXT NOT NULL CHECK (kind IN ('open','move','pause','close','reopen','rename')),
  effective_date TEXT,                                -- NULL = date not stated (event still recorded)
  effective_precision TEXT CHECK (effective_precision IN ('year','month','day')),
  to_branch_id   TEXT REFERENCES branch(branch_id),   -- for 'move'
  by_chef_id     TEXT REFERENCES chef(chef_id),       -- for 'open': the chef a source says opened it (not implied by employment)
  detail         TEXT,                                -- verbatim source text when not parseable
  state          TEXT NOT NULL DEFAULT 'active' CHECK (state IN ('active','rejected')),
  claim_id       TEXT NOT NULL REFERENCES claim(claim_id),
  seed_ref       TEXT,
  created_at     TEXT NOT NULL,
  CHECK (kind <> 'move' OR to_branch_id IS NOT NULL OR detail IS NOT NULL)
);
CREATE UNIQUE INDEX idx_status_seed_ref ON restaurant_status_event(seed_ref) WHERE seed_ref IS NOT NULL;
CREATE INDEX idx_status_branch ON restaurant_status_event(branch_id, effective_date);

CREATE TABLE menu_event (
  menu_event_id TEXT PRIMARY KEY,
  branch_id     TEXT NOT NULL REFERENCES branch(branch_id),
  kind          TEXT NOT NULL CHECK (kind IN ('menu','event','collab','popup')),
  title         TEXT NOT NULL,
  starts_on     TEXT,
  ends_on       TEXT,
  claim_id      TEXT NOT NULL REFERENCES claim(claim_id),
  created_at    TEXT NOT NULL
);

CREATE TABLE mentorship_assertion (                 -- ONLY when a source says so (e.g. '사사', 'trained under')
  mentorship_id   TEXT PRIMARY KEY,
  mentee_chef_id  TEXT NOT NULL REFERENCES chef(chef_id),
  mentor_chef_id  TEXT REFERENCES chef(chef_id),
  mentor_label    TEXT,
  claim_id        TEXT NOT NULL REFERENCES claim(claim_id),
  seed_ref        TEXT,
  created_at      TEXT NOT NULL,
  CHECK (mentor_chef_id IS NOT NULL OR mentor_label IS NOT NULL)
);
CREATE UNIQUE INDEX idx_mentorship_seed_ref ON mentorship_assertion(seed_ref) WHERE seed_ref IS NOT NULL;

-- Derived, never stored: two chefs with employments at the same branch whose KNOWN intervals intersect.
-- Open employments extend to '9999'. Rows with unknown valid_from are excluded (cannot be asserted).
-- This is "co-employment", NOT mentorship, and no table links the two concepts.
CREATE VIEW v_overlap AS
SELECT a.employment_id AS employment_a, b.employment_id AS employment_b,
       a.chef_id AS chef_a, b.chef_id AS chef_b, a.branch_id AS branch_id,
       MAX(a.valid_from, b.valid_from) AS overlap_from,
       MIN(COALESCE(a.valid_to, '9999'), COALESCE(b.valid_to, '9999')) AS overlap_to
FROM employment a JOIN employment b
  ON a.branch_id = b.branch_id AND a.employment_id < b.employment_id AND a.chef_id <> b.chef_id
WHERE a.state <> 'rejected' AND b.state <> 'rejected'
  AND a.valid_from IS NOT NULL AND b.valid_from IS NOT NULL
  AND a.valid_to_kind <> 'unknown' AND b.valid_to_kind <> 'unknown'
  AND MAX(a.valid_from, b.valid_from) < MIN(COALESCE(a.valid_to, '9999'), COALESCE(b.valid_to, '9999'));

-- ───────────────────────── conflicts, corrections, review ─────────────────────────
CREATE TABLE conflict (
  conflict_id   TEXT PRIMARY KEY,
  subject_type  TEXT NOT NULL,
  subject_id    TEXT NOT NULL,
  field         TEXT NOT NULL,
  claim_a       TEXT NOT NULL REFERENCES claim(claim_id),
  claim_b       TEXT NOT NULL REFERENCES claim(claim_id),
  state         TEXT NOT NULL DEFAULT 'open' CHECK (state IN ('open','resolved','dismissed')),
  resolution_note TEXT,
  opened_at     TEXT NOT NULL,
  resolved_at   TEXT,
  CHECK (claim_a <> claim_b)
);

CREATE TABLE correction_history (
  correction_id INTEGER PRIMARY KEY AUTOINCREMENT,
  table_name    TEXT NOT NULL,
  row_id        TEXT NOT NULL,
  field         TEXT NOT NULL,
  old_value     TEXT,
  new_value     TEXT,
  reason        TEXT,
  evidence_id   TEXT REFERENCES evidence(evidence_id),
  corrected_by  TEXT NOT NULL DEFAULT 'system',
  corrected_at  TEXT NOT NULL
);

CREATE TABLE review_queue (
  review_id    TEXT PRIMARY KEY,
  reason       TEXT NOT NULL,                        -- 'conflict','homonym','unresolved_employer','unnamed_chef','ai_inference', ...
  subject_type TEXT NOT NULL,
  subject_id   TEXT,
  payload_json TEXT NOT NULL DEFAULT '{}',
  state        TEXT NOT NULL DEFAULT 'open' CHECK (state IN ('open','resolved','dismissed')),
  dedupe_key   TEXT UNIQUE,
  created_at   TEXT NOT NULL,
  resolved_at  TEXT
);

-- ───────────────────────── append-only guards ─────────────────────────
CREATE TRIGGER trg_evidence_immutable BEFORE UPDATE ON evidence
BEGIN SELECT RAISE(ABORT, 'evidence is immutable; add a new evidence row'); END;

CREATE TRIGGER trg_mentorship_not_ai BEFORE INSERT ON mentorship_assertion
WHEN (SELECT status FROM claim WHERE claim_id = NEW.claim_id) IS NULL
  OR (SELECT status FROM claim WHERE claim_id = NEW.claim_id) = 'ai_inference'
BEGIN SELECT RAISE(ABORT, 'mentorship must be asserted by a source, not inferred'); END;

-- Employment edits (closing, correcting) are logged automatically.
CREATE TRIGGER trg_employment_audit AFTER UPDATE ON employment
BEGIN
  INSERT INTO correction_history(table_name,row_id,field,old_value,new_value,reason,corrected_by,corrected_at)
  SELECT 'employment', NEW.employment_id, f, o, n, 'auto-audit', 'trigger', strftime('%Y-%m-%dT%H:%M:%SZ','now')
  FROM (
    SELECT 'valid_from' f, OLD.valid_from o, NEW.valid_from n WHERE OLD.valid_from IS NOT NEW.valid_from
    UNION ALL SELECT 'valid_to', OLD.valid_to, NEW.valid_to WHERE OLD.valid_to IS NOT NEW.valid_to
    UNION ALL SELECT 'valid_to_kind', OLD.valid_to_kind, NEW.valid_to_kind WHERE OLD.valid_to_kind IS NOT NEW.valid_to_kind
    UNION ALL SELECT 'state', OLD.state, NEW.state WHERE OLD.state IS NOT NEW.state
    UNION ALL SELECT 'branch_id', OLD.branch_id, NEW.branch_id WHERE OLD.branch_id IS NOT NEW.branch_id
  );
END;

CREATE TRIGGER trg_claim_status_audit AFTER UPDATE OF status ON claim
WHEN OLD.status <> NEW.status
BEGIN
  INSERT INTO claim_status_history(claim_id,from_status,to_status,changed_at,changed_by,reason)
  VALUES (NEW.claim_id, OLD.status, NEW.status, NEW.status_changed_at, 'trigger', 'auto-audit');
END;

-- No DELETE on any fact/provenance table. Raw blobs are purged outside the DB (document.raw_purged_at).
CREATE TRIGGER trg_chef_no_delete BEFORE DELETE ON chef
BEGIN SELECT RAISE(ABORT, 'append-only: chef rows are never deleted'); END;
CREATE TRIGGER trg_restaurant_no_delete BEFORE DELETE ON restaurant
BEGIN SELECT RAISE(ABORT, 'append-only: restaurant rows are never deleted'); END;
CREATE TRIGGER trg_branch_no_delete BEFORE DELETE ON branch
BEGIN SELECT RAISE(ABORT, 'append-only: branch rows are never deleted'); END;
CREATE TRIGGER trg_role_no_delete BEFORE DELETE ON role
BEGIN SELECT RAISE(ABORT, 'append-only: role rows are never deleted'); END;
CREATE TRIGGER trg_alias_no_delete BEFORE DELETE ON alias
BEGIN SELECT RAISE(ABORT, 'append-only: alias rows are never deleted'); END;
CREATE TRIGGER trg_employment_no_delete BEFORE DELETE ON employment
BEGIN SELECT RAISE(ABORT, 'append-only: employment rows are never deleted'); END;
CREATE TRIGGER trg_restaurant_status_event_no_delete BEFORE DELETE ON restaurant_status_event
BEGIN SELECT RAISE(ABORT, 'append-only: restaurant_status_event rows are never deleted'); END;
CREATE TRIGGER trg_menu_event_no_delete BEFORE DELETE ON menu_event
BEGIN SELECT RAISE(ABORT, 'append-only: menu_event rows are never deleted'); END;
CREATE TRIGGER trg_mentorship_assertion_no_delete BEFORE DELETE ON mentorship_assertion
BEGIN SELECT RAISE(ABORT, 'append-only: mentorship_assertion rows are never deleted'); END;
CREATE TRIGGER trg_claim_no_delete BEFORE DELETE ON claim
BEGIN SELECT RAISE(ABORT, 'append-only: claim rows are never deleted'); END;
CREATE TRIGGER trg_claim_evidence_no_delete BEFORE DELETE ON claim_evidence
BEGIN SELECT RAISE(ABORT, 'append-only: claim_evidence rows are never deleted'); END;
CREATE TRIGGER trg_claim_status_history_no_delete BEFORE DELETE ON claim_status_history
BEGIN SELECT RAISE(ABORT, 'append-only: claim_status_history rows are never deleted'); END;
CREATE TRIGGER trg_conflict_no_delete BEFORE DELETE ON conflict
BEGIN SELECT RAISE(ABORT, 'append-only: conflict rows are never deleted'); END;
CREATE TRIGGER trg_correction_history_no_delete BEFORE DELETE ON correction_history
BEGIN SELECT RAISE(ABORT, 'append-only: correction_history rows are never deleted'); END;
CREATE TRIGGER trg_evidence_no_delete BEFORE DELETE ON evidence
BEGIN SELECT RAISE(ABORT, 'append-only: evidence rows are never deleted'); END;
CREATE TRIGGER trg_document_no_delete BEFORE DELETE ON document
BEGIN SELECT RAISE(ABORT, 'append-only: document rows are never deleted'); END;
CREATE TRIGGER trg_source_no_delete BEFORE DELETE ON source
BEGIN SELECT RAISE(ABORT, 'append-only: source rows are never deleted'); END;
CREATE TRIGGER trg_source_policy_no_delete BEFORE DELETE ON source_policy
BEGIN SELECT RAISE(ABORT, 'append-only: source_policy rows are never deleted'); END;

INSERT INTO role(role_id,label_ko,label_en) VALUES
 ('executive_chef','총괄 셰프','Executive chef'),
 ('head_chef','헤드 셰프','Head chef'),
 ('sous_chef','수셰프','Sous chef'),
 ('pastry_chef','파티시에','Pastry chef'),
 ('founding_member','창립·개업 멤버','Founding member'),
 ('owner_chef','오너 셰프','Owner chef'),
 ('trainee','실습','Trainee'),
 ('alumnus_unspecified','출신(직책 미기재)','Alumnus (role not stated)');
