CREATE TABLE IF NOT EXISTS social_interactions (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  provider TEXT NOT NULL,
  post_id UUID NOT NULL REFERENCES social_posts(id) ON DELETE CASCADE,
  interaction_type TEXT NOT NULL CHECK (interaction_type IN ('like', 'comment', 'reply', 'mention')),
  external_id TEXT NOT NULL,
  author_profile_id UUID REFERENCES profiles(id) ON DELETE SET NULL,
  author_external_id TEXT,
  author_name TEXT,
  content_object_id UUID REFERENCES content_objects(id),
  occurred_at TIMESTAMPTZ,
  metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
  current_content_hash CHAR(64) NOT NULL,
  UNIQUE (provider, interaction_type, external_id)
);

CREATE INDEX IF NOT EXISTS social_interactions_post_idx
  ON social_interactions(post_id, interaction_type, occurred_at DESC);
CREATE INDEX IF NOT EXISTS social_interactions_author_idx
  ON social_interactions(author_profile_id, occurred_at DESC);

CREATE INDEX IF NOT EXISTS social_posts_profile_time_idx
  ON social_posts(profile_id, occurred_at DESC);
CREATE INDEX IF NOT EXISTS social_stories_profile_time_idx
  ON social_stories(profile_id, occurred_at DESC);
