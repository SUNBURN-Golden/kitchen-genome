-- Private runtime state only. Outstanding reservations survive process loss.
CREATE TABLE ai_request (
  request_key TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES daemon_run(run_id),
  day TEXT NOT NULL, task TEXT NOT NULL, model TEXT NOT NULL,
  state TEXT NOT NULL, units REAL NOT NULL, reserved_tokens INTEGER NOT NULL,
  prompt_tokens INTEGER, completion_tokens INTEGER, cache_tokens INTEGER,
  result_json TEXT, error TEXT, created_at TEXT NOT NULL
);
CREATE INDEX ai_request_day ON ai_request(day);
CREATE TABLE web_daily_url (day TEXT NOT NULL, url TEXT NOT NULL, PRIMARY KEY(day,url));
CREATE TABLE web_daily_fetch (day TEXT PRIMARY KEY, attempts INTEGER NOT NULL DEFAULT 0);
CREATE TABLE web_host_guard (host TEXT PRIMARY KEY, last_dispatch REAL, paused TEXT, delay_sec REAL NOT NULL DEFAULT 5);
CREATE TABLE ig_account_state (account TEXT PRIMARY KEY, last_dispatch REAL, stopped_day TEXT);
CREATE TABLE ig_handle_day (day TEXT NOT NULL, handle TEXT NOT NULL, captions INTEGER NOT NULL DEFAULT 0,
  complete INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(day,handle));
CREATE TABLE ig_follow_day (day TEXT NOT NULL, handle TEXT NOT NULL, PRIMARY KEY(day,handle));
ALTER TABLE extraction ADD COLUMN asserted_by TEXT NOT NULL DEFAULT 'source';
ALTER TABLE extraction ADD COLUMN pending_only INTEGER NOT NULL DEFAULT 0;
ALTER TABLE extraction ADD COLUMN model_meta_json TEXT;
