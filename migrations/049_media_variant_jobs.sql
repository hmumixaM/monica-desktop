-- Every newly stored image gets a variant job, whichever importer stored it.
-- (No percent signs: migrations also run through raw driver execution.)
CREATE OR REPLACE FUNCTION enqueue_media_variants() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  INSERT INTO jobs(job_type, payload)
  SELECT 'media.variants', jsonb_build_object('media_id', id)
  FROM changed_rows WHERE starts_with(media_type, 'image/');
  RETURN NULL;
END $$;

DROP TRIGGER IF EXISTS media_variants_insert ON media_assets;
CREATE TRIGGER media_variants_insert AFTER INSERT ON media_assets
  REFERENCING NEW TABLE AS changed_rows FOR EACH STATEMENT EXECUTE FUNCTION enqueue_media_variants();
