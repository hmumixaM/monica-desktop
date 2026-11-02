-- Current numeric summaries and city names used by global dashboards. This is
-- a small derived index; source fields and every revision remain authoritative.
CREATE TABLE IF NOT EXISTS profile_metric_values (
  profile_id UUID NOT NULL,
  field_key TEXT NOT NULL,
  revision_id UUID NOT NULL REFERENCES profile_field_revisions(id),
  value JSONB NOT NULL,
  PRIMARY KEY(profile_id,field_key),
  FOREIGN KEY(profile_id,field_key) REFERENCES profile_field_current(profile_id,field_key) ON DELETE CASCADE
);
CREATE OR REPLACE FUNCTION is_profile_metric_field(key TEXT) RETURNS boolean LANGUAGE sql IMMUTABLE AS $$
  SELECT key IN ('wechat.direct_stats','wechat.group_stats','wechat.city','wechat.province',
    'wechat.country','wechat.region','linkedin.location','instagram.location')
    OR key ~ '^instagram.accounts.[^.]+.followers_count$'
$$;
CREATE OR REPLACE FUNCTION refresh_profile_metric_value() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF is_profile_metric_field(NEW.field_key) THEN
    INSERT INTO profile_metric_values(profile_id,field_key,revision_id,value)
    SELECT NEW.profile_id,NEW.field_key,NEW.current_revision_id,c.payload_json
      FROM profile_field_revisions r JOIN content_objects c ON c.id=r.content_object_id
      WHERE r.id=NEW.current_revision_id
    ON CONFLICT(profile_id,field_key) DO UPDATE SET revision_id=EXCLUDED.revision_id,value=EXCLUDED.value;
  END IF;
  RETURN NEW;
END $$;
DROP TRIGGER IF EXISTS profile_metric_value_refresh ON profile_field_current;
CREATE TRIGGER profile_metric_value_refresh AFTER INSERT OR UPDATE OF current_revision_id ON profile_field_current
  FOR EACH ROW EXECUTE FUNCTION refresh_profile_metric_value();
