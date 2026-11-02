-- Browsing statistics must not de-TOAST every post body or comment payload.
-- These are rebuildable counters; source objects and revisions stay immutable.
ALTER TABLE social_posts ADD COLUMN IF NOT EXISTS like_count BIGINT NOT NULL DEFAULT 0;
ALTER TABLE social_posts ADD COLUMN IF NOT EXISTS comment_count BIGINT NOT NULL DEFAULT 0;

CREATE OR REPLACE FUNCTION social_post_counters() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE body jsonb;
BEGIN
  SELECT payload_json INTO body FROM content_objects WHERE id=NEW.content_object_id;
  NEW.like_count := CASE WHEN body->>'like_count' ~ '^\d+$' THEN (body->>'like_count')::bigint ELSE 0 END;
  NEW.comment_count := CASE WHEN body->>'comment_count' ~ '^\d+$' THEN (body->>'comment_count')::bigint ELSE 0 END;
  RETURN NEW;
END $$;
DROP TRIGGER IF EXISTS social_post_counters_refresh ON social_posts;
CREATE TRIGGER social_post_counters_refresh BEFORE INSERT OR UPDATE OF content_object_id ON social_posts
  FOR EACH ROW EXECUTE FUNCTION social_post_counters();

UPDATE social_posts p SET
  like_count=CASE WHEN o.payload_json->>'like_count' ~ '^\d+$' THEN (o.payload_json->>'like_count')::bigint ELSE 0 END,
  comment_count=CASE WHEN o.payload_json->>'comment_count' ~ '^\d+$' THEN (o.payload_json->>'comment_count')::bigint ELSE 0 END
FROM content_objects o WHERE o.id=p.content_object_id;
CREATE INDEX IF NOT EXISTS social_posts_summary_idx ON social_posts(provider,profile_id)
  INCLUDE(occurred_at,like_count,comment_count);

-- Graph browsing uses only compact routing facts, not comment JSON bodies.
ALTER TABLE social_interactions ADD COLUMN IF NOT EXISTS ref_author_external_id TEXT;
ALTER TABLE social_interactions ADD COLUMN IF NOT EXISTS target_external_id TEXT;
ALTER TABLE social_interactions ADD COLUMN IF NOT EXISTS is_deleted BOOLEAN NOT NULL DEFAULT FALSE;
CREATE OR REPLACE FUNCTION social_interaction_routing() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  NEW.ref_author_external_id := NULLIF(NEW.metadata->>'ref_author_external_id','');
  NEW.target_external_id := NULLIF(NEW.metadata->>'target_external_id','');
  NEW.is_deleted := COALESCE(NEW.metadata->>'is_deleted','false')='true';
  RETURN NEW;
END $$;
DROP TRIGGER IF EXISTS social_interaction_routing_refresh ON social_interactions;
CREATE TRIGGER social_interaction_routing_refresh BEFORE INSERT OR UPDATE OF metadata ON social_interactions
  FOR EACH ROW EXECUTE FUNCTION social_interaction_routing();
UPDATE social_interactions SET ref_author_external_id=NULLIF(metadata->>'ref_author_external_id',''),
  target_external_id=NULLIF(metadata->>'target_external_id',''),is_deleted=COALESCE(metadata->>'is_deleted','false')='true';
CREATE INDEX IF NOT EXISTS social_interactions_graph_idx
  ON social_interactions(provider,author_profile_id,post_id,interaction_type)
  INCLUDE(occurred_at,ref_author_external_id,target_external_id,is_deleted);
