-- Lightweight, rebuildable map metadata. The immutable source payload remains
-- authoritative, but browsing a map never scans message/media payload storage.
CREATE TABLE IF NOT EXISTS social_post_locations (
  post_id UUID PRIMARY KEY REFERENCES social_posts(id) ON DELETE CASCADE,
  content_object_id UUID NOT NULL REFERENCES content_objects(id),
  location JSONB,
  coordinate_format TEXT
);
CREATE OR REPLACE FUNCTION refresh_social_post_location() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  INSERT INTO social_post_locations(post_id,content_object_id,location,coordinate_format)
  SELECT NEW.id,NEW.content_object_id,c.payload_json->'location',
    CASE WHEN NEW.provider='wechat' AND c.payload_json ? 'raw_xml_sha256' THEN 'chatlog_sns_xml' END
  FROM content_objects c WHERE c.id=NEW.content_object_id
  ON CONFLICT(post_id) DO UPDATE SET content_object_id=EXCLUDED.content_object_id,
    location=EXCLUDED.location,coordinate_format=EXCLUDED.coordinate_format;
  RETURN NEW;
END $$;
DROP TRIGGER IF EXISTS social_post_location_refresh ON social_posts;
CREATE TRIGGER social_post_location_refresh AFTER INSERT OR UPDATE OF content_object_id ON social_posts
  FOR EACH ROW EXECUTE FUNCTION refresh_social_post_location();
