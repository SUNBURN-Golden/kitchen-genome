-- Additive private scheduler/accounting state; no published seed migration.
ALTER TABLE ai_request ADD COLUMN workload TEXT NOT NULL DEFAULT 'fresh' CHECK(workload IN ('fresh','recheck'));
CREATE INDEX ai_request_workload_day ON ai_request(day,workload);
ALTER TABLE extraction ADD COLUMN workload TEXT NOT NULL DEFAULT 'fresh' CHECK(workload IN ('fresh','recheck'));
CREATE TABLE article_extraction_fetch (
 extraction_id TEXT NOT NULL REFERENCES extraction(extraction_id), fetch_id TEXT NOT NULL REFERENCES fetch_record(fetch_id),
 PRIMARY KEY(extraction_id,fetch_id)
);
CREATE TABLE restaurant_recheck (
 restaurant_id TEXT PRIMARY KEY, target_id TEXT NOT NULL REFERENCES target(target_id),
 last_attempted_at TEXT, last_checked_at TEXT, next_due_at TEXT NOT NULL,
 active_attempt_id TEXT, failures INTEGER NOT NULL DEFAULT 0, last_outcome TEXT
);
CREATE TABLE restaurant_recheck_attempt (
 attempt_id TEXT PRIMARY KEY, restaurant_id TEXT NOT NULL REFERENCES restaurant_recheck(restaurant_id),
 due_period TEXT NOT NULL, day TEXT NOT NULL, queued_at TEXT NOT NULL, completed_at TEXT,
 state TEXT NOT NULL DEFAULT 'queued', reason TEXT,
 UNIQUE(restaurant_id,due_period)
);
CREATE TABLE restaurant_recheck_job (
 attempt_id TEXT NOT NULL REFERENCES restaurant_recheck_attempt(attempt_id),
 job_id TEXT NOT NULL REFERENCES crawl_job(job_id), fetch_id TEXT REFERENCES fetch_record(fetch_id),
 extraction_id TEXT REFERENCES extraction(extraction_id),
 dedicated_root INTEGER NOT NULL DEFAULT 0 CHECK(dedicated_root IN (0,1)),
 UNIQUE(attempt_id,job_id,fetch_id,extraction_id)
);
CREATE INDEX recheck_job_lookup ON restaurant_recheck_job(job_id);
CREATE TABLE ai_review_hold (
 attempt_id TEXT PRIMARY KEY REFERENCES restaurant_recheck_attempt(attempt_id), day TEXT NOT NULL,
 units REAL NOT NULL, state TEXT NOT NULL DEFAULT 'held', request_key TEXT
);
