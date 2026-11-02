-- Human decisions for cross-platform account linking. Heuristic matches are
-- computed on read; this table only stores explicit confirm / reject / skip.
CREATE TABLE IF NOT EXISTS platform_link_decisions (
  source_account_id UUID NOT NULL REFERENCES source_accounts(id) ON DELETE CASCADE,
  target_profile_id UUID NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
  status TEXT NOT NULL CHECK (status IN ('accepted', 'rejected', 'skipped')),
  score SMALLINT,
  pair TEXT NOT NULL,
  evidence JSONB NOT NULL DEFAULT '{}'::jsonb,
  reviewed_by UUID REFERENCES app_users(id) ON DELETE SET NULL,
  reviewed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (source_account_id, target_profile_id)
);
CREATE INDEX IF NOT EXISTS platform_link_decisions_status_idx
  ON platform_link_decisions(status, reviewed_at DESC);
CREATE INDEX IF NOT EXISTS platform_link_decisions_target_idx
  ON platform_link_decisions(target_profile_id);
