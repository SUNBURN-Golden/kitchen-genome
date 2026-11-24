-- Private diagnostic snapshot of the current unlinked review backlog.
CREATE TABLE ai_promotion_skip (
  unlinked_index INTEGER PRIMARY KEY,
  review_id TEXT,
  reason TEXT NOT NULL,
  extraction_id TEXT,
  quote_key TEXT
);
