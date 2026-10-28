-- 0005: candidate facts for temporary closures (pause), menus and collab/pop-up events coming from the live collector.
-- SQLite cannot alter a CHECK, so the table is rebuilt (no table references candidate_fact).
CREATE TABLE candidate_fact_v5 (
  candidate_id    TEXT PRIMARY KEY,
  idempotency_key TEXT NOT NULL UNIQUE,
  document_id     TEXT REFERENCES document(document_id),
  source_id       TEXT NOT NULL REFERENCES source(source_id),
  fact_type       TEXT NOT NULL CHECK (fact_type IN ('chef_move','opening','closure','pause','rename','relocation','mentorship','employment','menu','collab')),
  payload_json    TEXT NOT NULL,
  evidence_json   TEXT NOT NULL,
  extractor       TEXT NOT NULL,
  asserted_by     TEXT NOT NULL CHECK (asserted_by IN ('source','ai_inference')),
  resolution      TEXT NOT NULL DEFAULT 'pending' CHECK (resolution IN ('pending','resolved','needs_review','rejected')),
  resolved_chef_id TEXT REFERENCES chef(chef_id),
  resolved_branch_id TEXT REFERENCES branch(branch_id),
  verification    TEXT CHECK (verification IN ('verified','source_claim','ai_inference','unverified')),
  applied_claim_id TEXT REFERENCES claim(claim_id),
  created_at      TEXT NOT NULL
);
INSERT INTO candidate_fact_v5 SELECT * FROM candidate_fact;
DROP TABLE candidate_fact;
ALTER TABLE candidate_fact_v5 RENAME TO candidate_fact;
CREATE INDEX idx_candidate_state ON candidate_fact(resolution, applied_claim_id);
