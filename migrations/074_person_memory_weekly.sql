SET LOCAL lock_timeout = '5s';

ALTER TABLE ai_outputs DROP CONSTRAINT IF EXISTS ai_outputs_kind_check;
ALTER TABLE ai_outputs ADD CONSTRAINT ai_outputs_kind_check CHECK (kind IN (
  'community_name', 'reconnect', 'profile_overview', 'profile_updates',
  'profile_memory', 'profile_memory_chunk', 'profile_week_chunk'
));

CREATE TABLE IF NOT EXISTS person_weekly_updates (
  profile_id UUID NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
  week_start DATE NOT NULL,
  input_hash CHAR(64) NOT NULL,
  model TEXT NOT NULL,
  output JSONB NOT NULL,
  generated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (profile_id, week_start)
);
CREATE INDEX IF NOT EXISTS person_weekly_updates_week ON person_weekly_updates(week_start DESC, generated_at DESC);

-- Existing rows stay NULL; an old archive imported after this migration is a new
-- discovery, while the migration itself does not turn the archive into this week's news.
ALTER TABLE message_events ADD COLUMN IF NOT EXISTS ai_seen_at TIMESTAMPTZ;
ALTER TABLE social_posts ADD COLUMN IF NOT EXISTS ai_seen_at TIMESTAMPTZ;
ALTER TABLE social_stories ADD COLUMN IF NOT EXISTS ai_seen_at TIMESTAMPTZ;
ALTER TABLE profiles ADD COLUMN IF NOT EXISTS ai_changed_at TIMESTAMPTZ;
CREATE OR REPLACE FUNCTION ai_source_seen_at() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF TG_OP = 'INSERT' THEN
    NEW.ai_seen_at := now();
  ELSIF NEW.current_content_hash IS DISTINCT FROM OLD.current_content_hash
     OR NEW.profile_id IS DISTINCT FROM OLD.profile_id THEN
    NEW.ai_seen_at := now();
  END IF;
  RETURN NEW;
END $$;
CREATE OR REPLACE FUNCTION ai_message_seen_at() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  NEW.ai_seen_at := now();
  RETURN NEW;
END $$;
CREATE TRIGGER ai_message_seen BEFORE INSERT ON message_events
  FOR EACH ROW EXECUTE FUNCTION ai_message_seen_at();
CREATE TRIGGER ai_social_post_seen BEFORE INSERT OR UPDATE OF current_content_hash, profile_id ON social_posts
  FOR EACH ROW EXECUTE FUNCTION ai_source_seen_at();
CREATE TRIGGER ai_social_story_seen BEFORE INSERT OR UPDATE OF current_content_hash, profile_id ON social_stories
  FOR EACH ROW EXECUTE FUNCTION ai_source_seen_at();
CREATE OR REPLACE FUNCTION ai_profile_changed_at() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.display_name IS DISTINCT FROM OLD.display_name OR NEW.summary IS DISTINCT FROM OLD.summary THEN
    NEW.ai_changed_at := now();
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER ai_profile_changed BEFORE UPDATE OF display_name, summary ON profiles
  FOR EACH ROW EXECUTE FUNCTION ai_profile_changed_at();
CREATE INDEX IF NOT EXISTS message_events_ai_seen ON message_events(ai_seen_at) WHERE ai_seen_at IS NOT NULL;
CREATE INDEX IF NOT EXISTS social_posts_ai_seen ON social_posts(ai_seen_at) WHERE ai_seen_at IS NOT NULL;
CREATE INDEX IF NOT EXISTS social_stories_ai_seen ON social_stories(ai_seen_at) WHERE ai_seen_at IS NOT NULL;

CREATE OR REPLACE FUNCTION job_subject(kind text, body jsonb) RETURNS text LANGUAGE sql IMMUTABLE AS $$
  SELECT CASE
    WHEN kind='ai.person_week' THEN body->>'subject_key'
    WHEN left(kind,3)='ai.' THEN coalesce(body->>'profile_id',body->>'post_id',body->>'story_id',body->>'period','all')
    WHEN kind='media.dimensions' THEN body->>'media_id'
    WHEN kind='media.download' THEN encode(sha256(convert_to(jsonb_build_array(body->>'entity_type',body->>'entity_id',body->>'role',body->>'url')::text,'UTF8')),'hex')
    WHEN kind='metrics.refresh' THEN body->>'batch_id'
  END
$$;

CREATE TRIGGER data_version_insert AFTER INSERT ON person_weekly_updates
  REFERENCING NEW TABLE AS changed_rows FOR EACH STATEMENT EXECUTE FUNCTION bump_data_version('ai');
CREATE TRIGGER data_version_update AFTER UPDATE ON person_weekly_updates
  REFERENCING OLD TABLE AS old_rows NEW TABLE AS changed_rows FOR EACH STATEMENT EXECUTE FUNCTION bump_data_version('ai');
CREATE TRIGGER data_version_delete AFTER DELETE ON person_weekly_updates
  REFERENCING OLD TABLE AS changed_rows FOR EACH STATEMENT EXECUTE FUNCTION bump_data_version('ai');
