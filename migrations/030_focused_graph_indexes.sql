-- Resolve mentions and replies by explicit account ID without scanning all
-- interactions. NAS deployment may pre-create these concurrently.
CREATE INDEX IF NOT EXISTS social_interactions_mention_target_idx
  ON social_interactions(target_external_id,id)
  WHERE provider='wechat' AND interaction_type='mention' AND NOT is_deleted;
CREATE INDEX IF NOT EXISTS social_interactions_reply_author_idx
  ON social_interactions(ref_author_external_id,id)
  WHERE provider='wechat' AND interaction_type='reply' AND NOT is_deleted;
CREATE INDEX IF NOT EXISTS identities_wechat_graph_name_idx
  ON identities(profile_id,first_seen_at,id) INCLUDE(external_id)
  WHERE provider='wechat' AND status NOT IN ('invalid','merged');
