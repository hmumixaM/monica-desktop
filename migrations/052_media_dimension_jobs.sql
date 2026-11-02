-- Images are served as originals; new images only need their dimensions recorded.
-- (No percent signs: migrations also run through raw driver execution.)
CREATE OR REPLACE FUNCTION enqueue_media_variants() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  INSERT INTO jobs(job_type, payload)
  SELECT 'media.dimensions', jsonb_build_object('media_id', id)
  FROM changed_rows WHERE starts_with(media_type, 'image/');
  RETURN NULL;
END $$;

UPDATE jobs SET job_type = 'media.dimensions' WHERE job_type = 'media.variants';
