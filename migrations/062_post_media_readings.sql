-- What a model read off a post's images: visible text, a short description and a category per
-- image (a video counts by its poster), plus one line for the post as a whole. Kept for later
-- structured analysis; `media_ids` is the media set it was read from, so a changed set is reread.
SET LOCAL lock_timeout = '5s';

CREATE TABLE IF NOT EXISTS social_post_media_readings (
  post_id UUID PRIMARY KEY REFERENCES social_posts(id) ON DELETE CASCADE,
  media_ids UUID[] NOT NULL,
  input_hash CHAR(64) NOT NULL,
  model TEXT NOT NULL,
  summary TEXT NOT NULL,
  items JSONB NOT NULL,
  search_text TEXT NOT NULL,
  generated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Its own version domain: a long reread must not invalidate every post aggregate.
CREATE SEQUENCE IF NOT EXISTS data_version_post_media;
DROP TRIGGER IF EXISTS data_version_insert ON social_post_media_readings;
DROP TRIGGER IF EXISTS data_version_update ON social_post_media_readings;
DROP TRIGGER IF EXISTS data_version_delete ON social_post_media_readings;
CREATE TRIGGER data_version_insert AFTER INSERT ON social_post_media_readings
  REFERENCING NEW TABLE AS changed_rows FOR EACH STATEMENT EXECUTE FUNCTION bump_data_version('post_media');
CREATE TRIGGER data_version_update AFTER UPDATE ON social_post_media_readings
  REFERENCING NEW TABLE AS changed_rows FOR EACH STATEMENT EXECUTE FUNCTION bump_data_version('post_media');
CREATE TRIGGER data_version_delete AFTER DELETE ON social_post_media_readings
  REFERENCING OLD TABLE AS changed_rows FOR EACH STATEMENT EXECUTE FUNCTION bump_data_version('post_media');
