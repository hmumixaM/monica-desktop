ALTER TABLE profile_field_revisions ADD COLUMN IF NOT EXISTS revision_seq BIGSERIAL;
-- Content is deduplicated, transitions are not: A -> B -> A is three revisions.
ALTER TABLE social_post_revisions DROP CONSTRAINT IF EXISTS social_post_revisions_post_id_content_hash_key;
ALTER TABLE social_story_revisions DROP CONSTRAINT IF EXISTS social_story_revisions_story_id_content_hash_key;

ALTER TABLE profile_structured_current ADD COLUMN IF NOT EXISTS fact_key TEXT NOT NULL DEFAULT 'legacy';
ALTER TABLE profile_structured_current DROP CONSTRAINT IF EXISTS profile_structured_current_pkey;
ALTER TABLE profile_structured_current ADD PRIMARY KEY (profile_id, fact_type, fact_key);
ALTER TABLE profile_address_revisions ADD COLUMN IF NOT EXISTS fact_key TEXT NOT NULL DEFAULT 'legacy';
ALTER TABLE profile_employment_revisions ADD COLUMN IF NOT EXISTS fact_key TEXT NOT NULL DEFAULT 'legacy';
ALTER TABLE profile_education_revisions ADD COLUMN IF NOT EXISTS fact_key TEXT NOT NULL DEFAULT 'legacy';

CREATE TABLE IF NOT EXISTS profile_redirects (
  old_profile_id UUID PRIMARY KEY REFERENCES profiles(id),
  profile_id UUID NOT NULL REFERENCES profiles(id),
  evidence JSONB NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  CHECK (old_profile_id <> profile_id)
);
CREATE TABLE IF NOT EXISTS repair_snapshots (
  repair_key TEXT NOT NULL,
  entity_type TEXT NOT NULL,
  entity_key TEXT NOT NULL,
  payload JSONB NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY(repair_key, entity_type, entity_key)
);
CREATE INDEX IF NOT EXISTS field_source_history_idx
 ON profile_field_revisions(profile_id, field_key, source_type, source_record_id, revision_seq DESC);

CREATE OR REPLACE FUNCTION check_membership_types() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.group_profile_id = NEW.person_profile_id OR
     NOT EXISTS (SELECT 1 FROM profiles WHERE id=NEW.group_profile_id AND profile_type='group') OR
     NOT EXISTS (SELECT 1 FROM profiles WHERE id=NEW.person_profile_id AND profile_type='person') THEN
    RAISE EXCEPTION 'Membership requires a group and a distinct person';
  END IF;
  RETURN NEW;
END $$;
DROP TRIGGER IF EXISTS membership_types ON group_memberships;
CREATE TRIGGER membership_types BEFORE INSERT OR UPDATE ON group_memberships
FOR EACH ROW EXECUTE FUNCTION check_membership_types();
