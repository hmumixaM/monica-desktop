CREATE TABLE IF NOT EXISTS source_runtime (
  source TEXT NOT NULL,
  phase TEXT NOT NULL,
  status TEXT NOT NULL,
  heartbeat_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  started_at TIMESTAMPTZ,
  last_success_at TIMESTAMPTZ,
  next_retry_at TIMESTAMPTZ,
  failures INTEGER NOT NULL DEFAULT 0,
  error TEXT,
  details JSONB NOT NULL DEFAULT '{}',
  PRIMARY KEY(source,phase)
);
