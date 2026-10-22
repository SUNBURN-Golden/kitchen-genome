-- Kitchen Genome users (D1 'kitchengenome', binding DB). Social login only; we store email + newsletter consent.
CREATE TABLE IF NOT EXISTS users (
  id TEXT PRIMARY KEY,
  provider TEXT NOT NULL CHECK (provider IN ('google','kakao')),
  provider_sub TEXT NOT NULL UNIQUE,
  email TEXT NOT NULL,
  newsletter_opt_in INTEGER NOT NULL DEFAULT 0,
  consent_at TEXT,
  created_at TEXT NOT NULL,
  withdrawn_at TEXT
);
CREATE INDEX IF NOT EXISTS users_email ON users(email);
