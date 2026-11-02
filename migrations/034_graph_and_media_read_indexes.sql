-- Graphs only need the post owner, not wide post rows. The compact index also
-- covers shared-audience queries during concurrent source imports.
CREATE INDEX IF NOT EXISTS social_posts_graph_owner_idx ON social_posts(profile_id,id);
CREATE INDEX IF NOT EXISTS social_interactions_reply_cover_idx
  ON social_interactions(ref_author_external_id,id) INCLUDE(author_profile_id,occurred_at)
  WHERE provider='wechat' AND interaction_type='reply' AND NOT is_deleted;

-- Archive resumption replaces one URL/slot while retaining previous media.
-- Bound the history lookup as the cache grows beyond 100,000 photos.
CREATE INDEX IF NOT EXISTS media_source_revisions_url_media_idx
  ON media_source_revisions(source_url,media_id);
