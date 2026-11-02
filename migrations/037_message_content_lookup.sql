-- During incremental message import, matching existing body hashes only needs
-- content IDs. Keep the large immutable payload heap out of those lookups.
-- The existing unique content-kind/hash constraint still defines deduplication.
CREATE INDEX IF NOT EXISTS content_messages_hash_id_idx
  ON content_objects(sha256) INCLUDE(id) WHERE content_kind='message';
