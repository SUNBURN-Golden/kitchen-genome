ALTER TABLE web_host_guard ADD COLUMN retry_after_at REAL;

CREATE TABLE web_access_run (
  run_id TEXT PRIMARY KEY,
  expires_at REAL NOT NULL
);
CREATE INDEX web_access_run_expiry ON web_access_run(expires_at);
CREATE TABLE web_run_block (
  run_id TEXT NOT NULL REFERENCES web_access_run(run_id),
  host TEXT NOT NULL,
  reason TEXT NOT NULL,
  blocked_at REAL NOT NULL,
  PRIMARY KEY (run_id, host)
);
CREATE TABLE web_robots_cache (
  origin TEXT PRIMARY KEY,
  status INTEGER NOT NULL,
  body TEXT NOT NULL,
  fetched_at REAL NOT NULL
);

CREATE TABLE ig_post_watermark (
  handle TEXT PRIMARY KEY,
  shortcode TEXT NOT NULL,
  posted_at TEXT NOT NULL
);

-- Requeue only identified policy denials. Historical provider blocks remain recorded.
WITH legacy_url AS (
  SELECT job_id, replace(replace(lower(substr(url, instr(url, '://') + 3)), '?', '/'), '#', '/') || '/' AS rest
  FROM crawl_job WHERE state IN ('skipped', 'blocked') AND lower(url) GLOB 'http*://*'
), hosts AS (
  SELECT job_id, substr(rest, 1, instr(rest, '/') - 1) AS authority FROM legacy_url
), normalized AS (
  SELECT job_id, CASE WHEN authority LIKE 'www.%' THEN substr(authority,5)
                      WHEN authority LIKE 'm.%' THEN substr(authority,3) ELSE authority END AS host
  FROM hosts
)
UPDATE crawl_job SET state='queued', attempts=0, last_error=NULL, finished_at=NULL, not_before=NULL
WHERE job_id IN (
  SELECT n.job_id FROM normalized n JOIN crawl_job j USING(job_id)
  WHERE
    (n.host='guide.michelin.com' AND j.last_error LIKE 'manual_only:%')
    OR (j.last_error IN ('fail_banned: banned','banned','banned_host(robots/ToS): manual only') AND (
        n.host IN ('guide.michelin.com','dcinside.com','yna.co.kr','naver.me','naver.com')
        OR n.host LIKE '%.dcinside.com' OR n.host LIKE '%.yna.co.kr'
        OR (n.host LIKE '%.naver.com' AND n.host<>'place.naver.com' AND n.host NOT LIKE '%.place.naver.com')))
    OR (n.host IN ('guide.michelin.com','news.google.com') AND j.last_error='fail_robots: robots'
        AND NOT EXISTS (SELECT 1 FROM web_host_guard g WHERE g.host=n.host AND g.paused LIKE 'blocked:%')
        AND NOT EXISTS (SELECT 1 FROM fetch_record f WHERE f.host=n.host AND f.outcome IN ('fail_blocked','fail_login_page')))
);
UPDATE domain_profile SET state='trial', robots_ok=NULL, reason=NULL, evaluated_at=NULL
WHERE host IN ('guide.michelin.com','news.google.com','dcinside.com','yna.co.kr','naver.com','naver.me')
  AND state='blocked' AND reason='permission=0 (banned host, robots disallow, or login wall)'
  AND NOT EXISTS (SELECT 1 FROM web_host_guard g WHERE g.host=domain_profile.host AND g.paused LIKE 'blocked:%')
  AND NOT EXISTS (SELECT 1 FROM fetch_record f WHERE f.host=domain_profile.host AND f.outcome IN ('fail_blocked','fail_login_page'));

UPDATE source SET enabled=1, host='guide.michelin.com'
WHERE source_id='michelin_manual';
UPDATE source_policy SET collect_permission='allowed'
WHERE source_id='michelin_manual' AND collect_permission='manual_only';
UPDATE source SET enabled=0 WHERE source_id='instagram_public';
UPDATE source_policy SET collect_permission='prohibited', collectible_fields='[]'
WHERE source_id='instagram_public';

CREATE TABLE ig_post_checkpoint (
  handle TEXT NOT NULL,
  shortcode TEXT NOT NULL,
  posted_at TEXT NOT NULL,
  PRIMARY KEY (handle, shortcode)
);
