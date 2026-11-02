CREATE TABLE IF NOT EXISTS auth_login_attempts (
  attempt_key CHAR(64) PRIMARY KEY,
  attempts INTEGER NOT NULL DEFAULT 0,
  window_started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  blocked_until TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS auth_login_attempts_blocked_idx
  ON auth_login_attempts(blocked_until);
