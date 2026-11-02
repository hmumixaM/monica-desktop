-- Operational, optional covering index for saved visual context reads.
-- Run in autocommit mode: CREATE INDEX CONCURRENTLY permits normal writes.
-- The partial index covers the complete entity/media projection used to hash
-- recognized post and Story IDs without a heap lookup for every detected face.
CREATE INDEX CONCURRENTLY IF NOT EXISTS media_links_social_media_cover_idx
    ON media_links(entity_type, media_id, entity_id)
    WHERE entity_type IN ('social_post', 'social_story');
