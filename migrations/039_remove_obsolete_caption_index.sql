-- Timeline search uses social_posts/social_stories.search_body since 017.
-- This old GIN expression indexes a NULL caption for every message and field,
-- adding maintenance work to the shared immutable content table on every import.
-- No current query filters on this expression; content addresses stay indexed.
DROP INDEX IF EXISTS content_objects_payload_caption_trgm_idx;
