-- Heavy collector (2026-11-11): priority of a fetched document for the 07:00 extract order, so the fixed DeepSeek budget
-- (KG_AI_DAILY_COST_CAP <= 20) reaches the most promising pages first. Rows without a priority keep the default order weight.
CREATE TABLE IF NOT EXISTS fetch_priority (
  fetch_id   TEXT PRIMARY KEY,
  score      REAL NOT NULL,
  source     TEXT NOT NULL,
  created_at TEXT NOT NULL
);
