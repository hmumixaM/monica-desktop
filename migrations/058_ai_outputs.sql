-- Model outputs, one per subject. The input hash records what the model saw, so an unchanged
-- input is never sent again.
CREATE TABLE IF NOT EXISTS ai_outputs (
  kind TEXT NOT NULL CHECK (kind IN ('community_name', 'reconnect')),
  subject TEXT NOT NULL,
  input_hash CHAR(64) NOT NULL,
  model TEXT NOT NULL,
  output JSONB NOT NULL,
  generated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (kind, subject)
);

-- Community names typed by hand; they win over model names.
CREATE TABLE IF NOT EXISTS community_names (
  community_id TEXT PRIMARY KEY,
  label TEXT NOT NULL CHECK (length(btrim(label)) BETWEEN 1 AND 40),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- What was done with a reconnect suggestion. "done" holds until a newer contact is imported,
-- "snoozed" until `until`, "dismissed" for good.
CREATE TABLE IF NOT EXISTS reconnect_states (
  profile_id UUID PRIMARY KEY REFERENCES profiles(id) ON DELETE CASCADE,
  state TEXT NOT NULL CHECK (state IN ('done', 'snoozed', 'dismissed')),
  until TIMESTAMPTZ,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  CHECK ((state = 'snoozed') = (until IS NOT NULL))
);

-- One digest per week: the SQL facts, and the model's narrative when it was generated.
CREATE TABLE IF NOT EXISTS weekly_digests (
  week_start DATE PRIMARY KEY,
  facts JSONB NOT NULL,
  narrative JSONB,
  model TEXT,
  generated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Recurring worker jobs enqueue once per period; the primary key is the dedupe.
CREATE TABLE IF NOT EXISTS scheduled_runs (
  job_type TEXT NOT NULL,
  period TEXT NOT NULL,
  job_id UUID,
  enqueued_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (job_type, period)
);

-- Cached aggregates that show model output depend on the "ai" data version.
CREATE SEQUENCE IF NOT EXISTS data_version_ai;
DO $$
DECLARE
  relation text;
BEGIN
  FOREACH relation IN ARRAY ARRAY['ai_outputs', 'community_names', 'reconnect_states', 'weekly_digests'] LOOP
    EXECUTE 'DROP TRIGGER IF EXISTS data_version_insert ON ' || relation;
    EXECUTE 'DROP TRIGGER IF EXISTS data_version_update ON ' || relation;
    EXECUTE 'DROP TRIGGER IF EXISTS data_version_delete ON ' || relation;
    EXECUTE 'CREATE TRIGGER data_version_insert AFTER INSERT ON ' || relation
      || ' REFERENCING NEW TABLE AS changed_rows FOR EACH STATEMENT EXECUTE FUNCTION bump_data_version(''ai'')';
    EXECUTE 'CREATE TRIGGER data_version_update AFTER UPDATE ON ' || relation
      || ' REFERENCING NEW TABLE AS changed_rows FOR EACH STATEMENT EXECUTE FUNCTION bump_data_version(''ai'')';
    EXECUTE 'CREATE TRIGGER data_version_delete AFTER DELETE ON ' || relation
      || ' REFERENCING OLD TABLE AS changed_rows FOR EACH STATEMENT EXECUTE FUNCTION bump_data_version(''ai'')';
  END LOOP;
END $$;
