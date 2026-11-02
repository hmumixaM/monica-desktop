-- Model overviews of what Monica knows about a person, and suggested additions drawn from
-- their recent chats and posts. A suggestion only changes the profile once it is accepted.
SET LOCAL lock_timeout = '5s';

ALTER TABLE ai_outputs DROP CONSTRAINT IF EXISTS ai_outputs_kind_check;
ALTER TABLE ai_outputs ADD CONSTRAINT ai_outputs_kind_check
  CHECK (kind IN ('community_name', 'reconnect', 'profile_overview', 'profile_updates'));

-- Keyed by a fingerprint of the suggestion's kind and value, so a regenerated suggestion
-- that was already accepted or ignored stays hidden.
CREATE TABLE IF NOT EXISTS profile_suggestion_decisions (
  profile_id UUID NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
  fingerprint CHAR(64) NOT NULL,
  kind TEXT NOT NULL,
  decision TEXT NOT NULL CHECK (decision IN ('accepted', 'ignored')),
  applied JSONB,
  decided_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (profile_id, fingerprint)
);

DROP TRIGGER IF EXISTS data_version_insert ON profile_suggestion_decisions;
DROP TRIGGER IF EXISTS data_version_update ON profile_suggestion_decisions;
DROP TRIGGER IF EXISTS data_version_delete ON profile_suggestion_decisions;
CREATE TRIGGER data_version_insert AFTER INSERT ON profile_suggestion_decisions
  REFERENCING NEW TABLE AS changed_rows FOR EACH STATEMENT EXECUTE FUNCTION bump_data_version('ai');
CREATE TRIGGER data_version_update AFTER UPDATE ON profile_suggestion_decisions
  REFERENCING NEW TABLE AS changed_rows FOR EACH STATEMENT EXECUTE FUNCTION bump_data_version('ai');
CREATE TRIGGER data_version_delete AFTER DELETE ON profile_suggestion_decisions
  REFERENCING OLD TABLE AS changed_rows FOR EACH STATEMENT EXECUTE FUNCTION bump_data_version('ai');
