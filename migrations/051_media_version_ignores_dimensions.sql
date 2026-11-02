-- Rendering variants records image dimensions on every stored image. Those
-- updates must not advance the media data version, or a thumbnail backfill
-- would invalidate every cached aggregate for hours.
CREATE OR REPLACE FUNCTION bump_media_version_on_content_change() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF EXISTS (
    SELECT 1 FROM new_rows n JOIN old_rows o ON o.id = n.id
    WHERE (n.object_path, n.media_type, n.byte_length, n.sha256, n.content_object_id)
      IS DISTINCT FROM (o.object_path, o.media_type, o.byte_length, o.sha256, o.content_object_id)
  ) THEN
    PERFORM nextval('data_version_media');
  END IF;
  RETURN NULL;
END $$;

DROP TRIGGER IF EXISTS data_version_update ON media_assets;
CREATE TRIGGER data_version_update AFTER UPDATE ON media_assets
  REFERENCING OLD TABLE AS old_rows NEW TABLE AS new_rows
  FOR EACH STATEMENT EXECUTE FUNCTION bump_media_version_on_content_change();
