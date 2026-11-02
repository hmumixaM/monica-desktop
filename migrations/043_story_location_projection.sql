CREATE TABLE IF NOT EXISTS social_story_locations (
  story_id UUID PRIMARY KEY REFERENCES social_stories(id) ON DELETE CASCADE,
  content_object_id UUID NOT NULL REFERENCES content_objects(id),
  locations JSONB NOT NULL DEFAULT '[]'::jsonb
);
CREATE OR REPLACE FUNCTION refresh_social_story_location() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF TG_OP='UPDATE' AND NEW.content_object_id IS NOT DISTINCT FROM OLD.content_object_id THEN RETURN NEW; END IF;
  INSERT INTO social_story_locations(story_id,content_object_id,locations)
  SELECT NEW.id,NEW.content_object_id,COALESCE(c.payload_json->'locations','[]'::jsonb)
  FROM content_objects c WHERE c.id=NEW.content_object_id
  ON CONFLICT(story_id) DO UPDATE SET content_object_id=EXCLUDED.content_object_id,locations=EXCLUDED.locations;
  RETURN NEW;
END $$;
DROP TRIGGER IF EXISTS social_story_location_refresh ON social_stories;
CREATE TRIGGER social_story_location_refresh AFTER INSERT OR UPDATE OF content_object_id ON social_stories
  FOR EACH ROW EXECUTE FUNCTION refresh_social_story_location();
INSERT INTO social_story_locations(story_id,content_object_id,locations)
SELECT s.id,s.content_object_id,COALESCE(c.payload_json->'locations','[]'::jsonb)
FROM social_stories s JOIN content_objects c ON c.id=s.content_object_id
ON CONFLICT(story_id) DO NOTHING;
CREATE OR REPLACE VIEW social_location_events AS
SELECT p.id,p.profile_id,p.provider,p.occurred_at,l.location,l.coordinate_format,left(p.search_body,120) AS summary,'post'::text AS event_kind
FROM social_posts p JOIN social_post_locations l ON l.post_id=p.id
UNION ALL
SELECT s.id,s.profile_id,s.provider,s.occurred_at,place.location,'instagram_place_tag'::text,
       'Instagram Story · 地点标签'::text,'story'::text
FROM social_stories s JOIN social_story_locations l ON l.story_id=s.id
CROSS JOIN LATERAL jsonb_array_elements(l.locations) AS place(location);
