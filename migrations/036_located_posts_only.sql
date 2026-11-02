-- Chatlog emits a zero/zero string placeholder for most posts with no place.
-- Exclude that proven sentinel before reading/decoding location rows. A single
-- zero axis is a valid coordinate and must remain (equator / prime meridian).
CREATE INDEX IF NOT EXISTS social_post_locations_present_idx
  ON social_post_locations(post_id)
  WHERE location IS NOT NULL AND location<>'null'::jsonb
    AND NOT location @> '{"latitude":"0","longitude":"0"}'::jsonb;
