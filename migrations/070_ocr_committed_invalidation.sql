-- OCR was added on main after the committed-event migration was written.
-- Preserve sequence invalidation for old APIs during overlap, through the
-- shared function, while committing the new event with the OCR result.
CREATE OR REPLACE FUNCTION bump_image_ocr_version() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF EXISTS(SELECT 1 FROM changed_rows WHERE status='complete') THEN
    PERFORM record_data_change('post_media');
  END IF;
  RETURN NULL;
END $$;
