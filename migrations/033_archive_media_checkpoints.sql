-- A collector cache is mutable; retain its fingerprint only after making an
-- immutable content-addressed copy. This is an import checkpoint, not a source
-- identity or a Profile relationship.
CREATE TABLE IF NOT EXISTS archive_media_files (
  provider TEXT NOT NULL,
  source_path TEXT NOT NULL,
  byte_length BIGINT NOT NULL,
  modified_ns BIGINT NOT NULL,
  media_id UUID NOT NULL REFERENCES media_assets(id),
  PRIMARY KEY(provider,source_path)
);
