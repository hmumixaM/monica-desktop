-- Invalidation events become visible with the business transaction. Sequences
-- remain for old releases during rollout, but are no longer read by new ones.
CREATE TABLE data_version_events (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  domain text NOT NULL,
  transaction_id bigint NOT NULL DEFAULT txid_current()
);
CREATE INDEX data_version_events_domain ON data_version_events(domain);
CREATE TABLE data_version_totals(domain text PRIMARY KEY, version bigint NOT NULL DEFAULT 0);
INSERT INTO data_version_totals(domain)
SELECT unnest(ARRAY['profiles','links','tags','messages','posts','media','contact_events','ai','faces','post_media']);
CREATE OR REPLACE FUNCTION record_data_change(changed_domain text) RETURNS void LANGUAGE plpgsql AS $$
BEGIN
  INSERT INTO data_version_events(domain) VALUES(changed_domain) ON CONFLICT DO NOTHING;
  PERFORM nextval(quote_ident('data_version_' || changed_domain));
END $$;
CREATE OR REPLACE FUNCTION bump_data_version() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE domain text;
BEGIN
  IF TG_OP = 'TRUNCATE' OR EXISTS (SELECT 1 FROM changed_rows) THEN
    FOREACH domain IN ARRAY TG_ARGV LOOP
      PERFORM record_data_change(domain);
    END LOOP;
  END IF;
  RETURN NULL;
END $$;
CREATE OR REPLACE FUNCTION bump_data_version_truncate() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE domain text;
BEGIN
  FOREACH domain IN ARRAY TG_ARGV LOOP
    PERFORM record_data_change(domain);
  END LOOP;
  RETURN NULL;
END $$;
CREATE OR REPLACE FUNCTION bump_media_version_on_content_change() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF EXISTS (SELECT 1 FROM new_rows n JOIN old_rows o ON o.id=n.id
    WHERE (n.object_path,n.media_type,n.byte_length,n.sha256,n.content_object_id)
      IS DISTINCT FROM (o.object_path,o.media_type,o.byte_length,o.sha256,o.content_object_id)) THEN
    PERFORM record_data_change('media');
  END IF;
  RETURN NULL;
END $$;
