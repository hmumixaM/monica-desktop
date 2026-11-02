-- Change counters for cached aggregates. Each domain is a sequence advanced by
-- statement-level triggers only when a statement actually changed rows, so
-- idempotent import replays keep caches valid. Sequences take no row locks:
-- concurrent importers and API edits never wait on (or deadlock over) them.
-- (No percent signs: migrations also run through raw driver execution.)
DO $$
DECLARE
  domain text;
BEGIN
  FOREACH domain IN ARRAY ARRAY['profiles','links','tags','messages','posts','media','contact_events'] LOOP
    EXECUTE 'CREATE SEQUENCE IF NOT EXISTS ' || quote_ident('data_version_' || domain);
  END LOOP;
END $$;

CREATE OR REPLACE FUNCTION bump_data_version() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
  domain text;
BEGIN
  IF TG_OP = 'TRUNCATE' OR EXISTS (SELECT 1 FROM changed_rows) THEN
    FOREACH domain IN ARRAY TG_ARGV LOOP
      PERFORM nextval(quote_ident('data_version_' || domain));
    END LOOP;
  END IF;
  RETURN NULL;
END $$;

CREATE OR REPLACE FUNCTION bump_data_version_truncate() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
  domain text;
BEGIN
  FOREACH domain IN ARRAY TG_ARGV LOOP
    PERFORM nextval(quote_ident('data_version_' || domain));
  END LOOP;
  RETURN NULL;
END $$;

CREATE OR REPLACE FUNCTION data_version(domain text) RETURNS bigint LANGUAGE plpgsql STABLE AS $$
DECLARE
  value bigint;
BEGIN
  EXECUTE 'SELECT CASE WHEN is_called THEN last_value ELSE 0 END FROM ' || quote_ident('data_version_' || domain)
    INTO value;
  RETURN value;
END $$;

DO $$
DECLARE
  target record;
  relation text;
BEGIN
  FOR target IN
    SELECT * FROM (VALUES
      ('profiles','profiles'), ('profile_field_current','profiles'), ('profile_structured_current','profiles'),
      ('profile_birthdays','profiles'), ('relationship_edges','profiles'), ('group_memberships','profiles'),
      ('activities','profiles'), ('activity_participants','profiles'),
      ('identities','links'), ('source_accounts','links'), ('source_account_links','links'),
      ('identity_candidates','links'), ('platform_link_decisions','links'),
      ('tags','tags'), ('tag_memberships','tags'),
      ('message_events','messages'), ('conversations','messages'),
      ('social_posts','posts'), ('social_stories','posts'), ('social_interactions','posts'),
      ('social_post_locations','posts'), ('social_story_locations','posts'),
      ('media_assets','media'), ('media_links','media'),
      ('source_contact_events','contact_events'), ('source_chat_months','contact_events')
    ) AS t(table_name, domain)
    WHERE to_regclass(t.table_name) IS NOT NULL
  LOOP
    relation := quote_ident(target.table_name);
    EXECUTE 'DROP TRIGGER IF EXISTS data_version_insert ON ' || relation;
    EXECUTE 'DROP TRIGGER IF EXISTS data_version_update ON ' || relation;
    EXECUTE 'DROP TRIGGER IF EXISTS data_version_delete ON ' || relation;
    EXECUTE 'DROP TRIGGER IF EXISTS data_version_truncate ON ' || relation;
    EXECUTE 'CREATE TRIGGER data_version_insert AFTER INSERT ON ' || relation
      || ' REFERENCING NEW TABLE AS changed_rows FOR EACH STATEMENT EXECUTE FUNCTION bump_data_version('
      || quote_literal(target.domain) || ')';
    EXECUTE 'CREATE TRIGGER data_version_update AFTER UPDATE ON ' || relation
      || ' REFERENCING NEW TABLE AS changed_rows FOR EACH STATEMENT EXECUTE FUNCTION bump_data_version('
      || quote_literal(target.domain) || ')';
    EXECUTE 'CREATE TRIGGER data_version_delete AFTER DELETE ON ' || relation
      || ' REFERENCING OLD TABLE AS changed_rows FOR EACH STATEMENT EXECUTE FUNCTION bump_data_version('
      || quote_literal(target.domain) || ')';
    EXECUTE 'CREATE TRIGGER data_version_truncate AFTER TRUNCATE ON ' || relation
      || ' FOR EACH STATEMENT EXECUTE FUNCTION bump_data_version_truncate('
      || quote_literal(target.domain) || ')';
  END LOOP;
END $$;
