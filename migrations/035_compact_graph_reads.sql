-- Cover the small set of graph fields so reading a person's circle does not
-- revisit wide interaction and post heap rows for each edge. Live identity
-- resolution and archive/group checks remain in the graph query.
CREATE INDEX IF NOT EXISTS social_interactions_graph_author_cover_idx
  ON social_interactions(author_profile_id,id)
  INCLUDE(post_id,interaction_type,occurred_at,target_external_id,ref_author_external_id)
  WHERE provider='wechat' AND NOT is_deleted;
CREATE INDEX IF NOT EXISTS social_interactions_graph_post_cover_idx
  ON social_interactions(post_id,id)
  INCLUDE(author_profile_id,interaction_type,occurred_at,target_external_id,ref_author_external_id)
  WHERE provider='wechat' AND NOT is_deleted;
CREATE INDEX IF NOT EXISTS social_posts_location_owner_cover_idx
  ON social_posts(id) INCLUDE(profile_id,provider,occurred_at);
