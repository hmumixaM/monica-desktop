-- One unique covering index serves deduplication and message-ID lookup.
-- On a live NAS, prebuild this index CONCURRENTLY before applying this file.
-- The old uniqueness constraint stays in place until its replacement is ready.
CREATE UNIQUE INDEX IF NOT EXISTS content_objects_content_key
  ON content_objects(content_kind, sha256) INCLUDE (id);
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conrelid='content_objects'::regclass AND conname='content_objects_content_key'
  ) THEN
    ALTER TABLE content_objects ADD CONSTRAINT content_objects_content_key
      UNIQUE USING INDEX content_objects_content_key;
  END IF;
END $$;
ALTER TABLE content_objects DROP CONSTRAINT IF EXISTS content_objects_content_kind_sha256_key;
DROP INDEX IF EXISTS content_messages_hash_id_idx;
