-- Seeing an unchanged post again updates its observation time, but must not
-- rewrite the location projection or fetch its large archived payload again.
CREATE OR REPLACE FUNCTION refresh_social_post_location() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF TG_OP='UPDATE' AND NEW.content_object_id IS NOT DISTINCT FROM OLD.content_object_id THEN
    RETURN NEW;
  END IF;
  INSERT INTO social_post_locations(post_id,content_object_id,location,coordinate_format)
  SELECT NEW.id,NEW.content_object_id,c.payload_json->'location',
    CASE WHEN NEW.provider='wechat' AND c.payload_json ? 'raw_xml_sha256' THEN 'chatlog_sns_xml' END
  FROM content_objects c WHERE c.id=NEW.content_object_id
  ON CONFLICT(post_id) DO UPDATE SET content_object_id=EXCLUDED.content_object_id,
    location=EXCLUDED.location,coordinate_format=EXCLUDED.coordinate_format;
  RETURN NEW;
END $$;
