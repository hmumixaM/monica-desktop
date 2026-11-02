CREATE TABLE IF NOT EXISTS remote_media_sources (
  source_url TEXT PRIMARY KEY,
  media_id UUID NOT NULL REFERENCES media_assets(id),
  downloaded_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS media_links_entity_idx ON media_links(entity_type, entity_id);
CREATE UNIQUE INDEX IF NOT EXISTS jobs_media_download_key_idx
  ON jobs((payload->>'download_key')) WHERE job_type = 'media.download';
