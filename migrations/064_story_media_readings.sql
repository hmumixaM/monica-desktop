SET LOCAL lock_timeout = '5s';

CREATE TABLE IF NOT EXISTS social_story_media_readings (
  story_id UUID PRIMARY KEY REFERENCES social_stories(id) ON DELETE CASCADE,
  media_ids UUID[] NOT NULL,
  input_hash CHAR(64) NOT NULL,
  model TEXT NOT NULL,
  summary TEXT NOT NULL,
  items JSONB NOT NULL,
  search_text TEXT NOT NULL,
  generated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TRIGGER data_version_insert AFTER INSERT ON social_story_media_readings
  REFERENCING NEW TABLE AS changed_rows FOR EACH STATEMENT EXECUTE FUNCTION bump_data_version('post_media');
CREATE TRIGGER data_version_update AFTER UPDATE ON social_story_media_readings
  REFERENCING OLD TABLE AS old_rows NEW TABLE AS changed_rows FOR EACH STATEMENT EXECUTE FUNCTION bump_data_version('post_media');
CREATE TRIGGER data_version_delete AFTER DELETE ON social_story_media_readings
  REFERENCING OLD TABLE AS changed_rows FOR EACH STATEMENT EXECUTE FUNCTION bump_data_version('post_media');
