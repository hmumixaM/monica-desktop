ALTER TABLE remote_media_sources ADD COLUMN checked_at TIMESTAMPTZ NOT NULL DEFAULT now();
CREATE TABLE media_source_revisions (
 id BIGSERIAL PRIMARY KEY, source_url TEXT NOT NULL,
 media_id UUID NOT NULL REFERENCES media_assets(id), observed_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
INSERT INTO media_source_revisions(source_url,media_id,observed_at)
 SELECT source_url,media_id,downloaded_at FROM remote_media_sources;
CREATE FUNCTION record_media_source_change() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF TG_OP='INSERT' OR OLD.media_id IS DISTINCT FROM NEW.media_id THEN
  INSERT INTO media_source_revisions(source_url,media_id) VALUES(NEW.source_url,NEW.media_id);
 END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER media_source_history AFTER INSERT OR UPDATE ON remote_media_sources
 FOR EACH ROW EXECUTE FUNCTION record_media_source_change();
CREATE TABLE media_manifest (
 source_url TEXT NOT NULL, entity_type TEXT NOT NULL, entity_id UUID NOT NULL, role TEXT NOT NULL,
 state TEXT NOT NULL DEFAULT 'pending' CHECK(state IN ('pending','downloading','ready','expired','unavailable','failed')),
 media_id UUID REFERENCES media_assets(id), error TEXT, checked_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 PRIMARY KEY(source_url,entity_type,entity_id,role)
);
INSERT INTO media_manifest(source_url,entity_type,entity_id,role,state,media_id)
 SELECT r.source_url,l.entity_type,l.entity_id,l.role,'ready',r.media_id
 FROM remote_media_sources r JOIN media_links l ON l.media_id=r.media_id ON CONFLICT DO NOTHING;
