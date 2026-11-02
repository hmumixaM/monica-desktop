SET LOCAL lock_timeout = '5s';

CREATE TABLE IF NOT EXISTS image_ocr_settings (
  id BOOLEAN PRIMARY KEY DEFAULT true CHECK (id),
  enabled BOOLEAN NOT NULL DEFAULT false,
  include_chat BOOLEAN NOT NULL DEFAULT false,
  model TEXT NOT NULL DEFAULT 'gemini-3.5-flash-lite',
  daily_limit INTEGER NOT NULL DEFAULT 5500 CHECK (daily_limit > 0),
  target_days INTEGER NOT NULL DEFAULT 40 CHECK (target_days > 0),
  baseline_images INTEGER NOT NULL DEFAULT 0,
  started_at TIMESTAMPTZ,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
INSERT INTO image_ocr_settings(id) VALUES(true) ON CONFLICT DO NOTHING;

CREATE TABLE IF NOT EXISTS image_ocr_readings (
  media_id UUID PRIMARY KEY REFERENCES media_assets(id) ON DELETE CASCADE,
  status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','running','complete','failed')),
  model TEXT,
  text TEXT,
  attempts INTEGER NOT NULL DEFAULT 0,
  source_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  available_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  lease_token UUID,
  lease_until TIMESTAMPTZ,
  error TEXT,
  generated_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS image_ocr_claim ON image_ocr_readings(status,source_at DESC,media_id);
CREATE INDEX IF NOT EXISTS image_ocr_expired ON image_ocr_readings(lease_until) WHERE status='running';
CREATE INDEX IF NOT EXISTS image_ocr_completed ON image_ocr_readings(generated_at) WHERE status='complete';

CREATE TABLE IF NOT EXISTS image_ocr_daily_usage (
  day DATE PRIMARY KEY,
  attempted INTEGER NOT NULL DEFAULT 0,
  next_available_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE OR REPLACE FUNCTION bump_image_ocr_version() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF EXISTS(SELECT 1 FROM changed_rows WHERE status='complete') THEN
    PERFORM nextval('data_version_post_media');
  END IF;
  RETURN NULL;
END $$;
DROP TRIGGER IF EXISTS data_version_update ON image_ocr_readings;
CREATE TRIGGER data_version_update AFTER UPDATE ON image_ocr_readings
  REFERENCING NEW TABLE AS changed_rows FOR EACH STATEMENT EXECUTE FUNCTION bump_image_ocr_version();
