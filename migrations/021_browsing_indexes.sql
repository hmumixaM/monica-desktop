-- Removing hash uniqueness to retain A -> B -> A must retain history lookup indexes.
CREATE INDEX IF NOT EXISTS social_post_revisions_history_idx ON social_post_revisions(post_id,observed_at DESC,id DESC);
CREATE INDEX IF NOT EXISTS social_story_revisions_history_idx ON social_story_revisions(story_id,observed_at DESC,id DESC);
CREATE INDEX IF NOT EXISTS remote_media_sources_media_idx ON remote_media_sources(media_id);
CREATE INDEX IF NOT EXISTS group_memberships_person_current_idx ON group_memberships(person_profile_id,group_profile_id) WHERE left_at IS NULL;
CREATE INDEX IF NOT EXISTS conversations_profile_idx ON conversations(profile_id,id);
