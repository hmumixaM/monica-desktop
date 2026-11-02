CREATE INDEX IF NOT EXISTS social_posts_profile_timeline_idx
  ON social_posts(profile_id, occurred_at DESC NULLS LAST);
CREATE INDEX IF NOT EXISTS social_posts_provider_timeline_idx
  ON social_posts(provider, occurred_at DESC NULLS LAST);
CREATE INDEX IF NOT EXISTS social_stories_profile_timeline_idx
  ON social_stories(profile_id, occurred_at DESC NULLS LAST);
CREATE INDEX IF NOT EXISTS social_stories_provider_timeline_idx
  ON social_stories(provider, occurred_at DESC NULLS LAST);
CREATE INDEX IF NOT EXISTS activities_timeline_idx
  ON activities(occurred_at DESC);
CREATE INDEX IF NOT EXISTS activity_participants_profile_idx
  ON activity_participants(profile_id, activity_id);
CREATE INDEX IF NOT EXISTS identities_profile_provider_idx
  ON identities(profile_id, provider, status);
