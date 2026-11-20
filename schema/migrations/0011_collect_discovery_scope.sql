-- RSS dispatch quotas shared by collection plans and resumed runs, keyed by KST day.
CREATE TABLE discovery_query_dispatch (
  day             TEXT NOT NULL,
  canonical_query TEXT NOT NULL,
  family          TEXT NOT NULL CHECK (family IN ('mother','entity','general')),
  target_id       TEXT REFERENCES target(target_id),
  job_id          TEXT NOT NULL,
  PRIMARY KEY (day, canonical_query)
);
CREATE INDEX idx_discovery_query_rotation ON discovery_query_dispatch(family, target_id, day);
