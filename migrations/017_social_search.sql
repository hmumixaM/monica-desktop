-- Search current social text without scanning immutable message/raw payloads.
ALTER TABLE social_posts ADD COLUMN search_body TEXT NOT NULL DEFAULT '';
ALTER TABLE social_stories ADD COLUMN search_body TEXT NOT NULL DEFAULT '';
UPDATE social_posts p SET search_body=COALESCE(c.payload_json->>'caption',c.payload_json->>'text','')
 FROM content_objects c WHERE c.id=p.content_object_id;
UPDATE social_stories p SET search_body=COALESCE(c.payload_json->>'caption',c.payload_json->>'text','')
 FROM content_objects c WHERE c.id=p.content_object_id;
CREATE FUNCTION project_social_search() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 NEW.search_body=COALESCE((SELECT COALESCE(payload_json->>'caption',payload_json->>'text','')
 FROM content_objects WHERE id=NEW.content_object_id),'');
 RETURN NEW;
END $$;
CREATE TRIGGER post_search_body BEFORE INSERT OR UPDATE OF content_object_id ON social_posts
 FOR EACH ROW EXECUTE FUNCTION project_social_search();
CREATE TRIGGER story_search_body BEFORE INSERT OR UPDATE OF content_object_id ON social_stories
 FOR EACH ROW EXECUTE FUNCTION project_social_search();
CREATE INDEX social_posts_search_trgm ON social_posts USING gin(search_body gin_trgm_ops);
CREATE INDEX social_stories_search_trgm ON social_stories USING gin(search_body gin_trgm_ops);
