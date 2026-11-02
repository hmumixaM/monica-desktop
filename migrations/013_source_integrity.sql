-- Source ownership and transition history are separate from display names.
CREATE TABLE source_crosswalk (
 source TEXT NOT NULL, account_scope TEXT NOT NULL DEFAULT 'default',
 object_kind TEXT NOT NULL, external_id TEXT NOT NULL,
 profile_id UUID NOT NULL REFERENCES profiles(id),
 confirmation_status TEXT NOT NULL CHECK (confirmation_status IN ('confirmed','source_only','pending')),
 evidence JSONB NOT NULL, updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 PRIMARY KEY(source,account_scope,object_kind,external_id)
);
CREATE TABLE source_crosswalk_revisions (
 id BIGSERIAL PRIMARY KEY, source TEXT NOT NULL, account_scope TEXT NOT NULL,
 object_kind TEXT NOT NULL, external_id TEXT NOT NULL, profile_id UUID NOT NULL REFERENCES profiles(id),
 confirmation_status TEXT NOT NULL, evidence JSONB NOT NULL, observed_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE FUNCTION record_crosswalk_change() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF TG_OP='INSERT' OR (OLD.profile_id,OLD.confirmation_status,OLD.evidence) IS DISTINCT FROM (NEW.profile_id,NEW.confirmation_status,NEW.evidence) THEN
 INSERT INTO source_crosswalk_revisions(source,account_scope,object_kind,external_id,profile_id,confirmation_status,evidence)
 VALUES(NEW.source,NEW.account_scope,NEW.object_kind,NEW.external_id,NEW.profile_id,NEW.confirmation_status,NEW.evidence);
 END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER crosswalk_history AFTER INSERT OR UPDATE ON source_crosswalk FOR EACH ROW EXECUTE FUNCTION record_crosswalk_change();

CREATE TABLE group_membership_revisions (
 id BIGSERIAL PRIMARY KEY, group_profile_id UUID NOT NULL REFERENCES profiles(id),
 person_profile_id UUID NOT NULL REFERENCES profiles(id), role TEXT,
 joined_at TIMESTAMPTZ, left_at TIMESTAMPTZ, source_record_id TEXT,
 operation TEXT NOT NULL, observed_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE FUNCTION record_membership_change() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF TG_OP='DELETE' THEN
 INSERT INTO group_membership_revisions(group_profile_id,person_profile_id,role,joined_at,left_at,source_record_id,operation)
 VALUES(OLD.group_profile_id,OLD.person_profile_id,OLD.role,OLD.joined_at,OLD.left_at,OLD.source_record_id,'removed');
 RETURN OLD;
 END IF;
 IF TG_OP='INSERT' OR (OLD.role,OLD.joined_at,OLD.left_at) IS DISTINCT FROM (NEW.role,NEW.joined_at,NEW.left_at) THEN
 INSERT INTO group_membership_revisions(group_profile_id,person_profile_id,role,joined_at,left_at,source_record_id,operation)
 VALUES(NEW.group_profile_id,NEW.person_profile_id,NEW.role,NEW.joined_at,NEW.left_at,NEW.source_record_id,lower(TG_OP));
 END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER membership_history AFTER INSERT OR UPDATE OR DELETE ON group_memberships FOR EACH ROW EXECUTE FUNCTION record_membership_change();
CREATE FUNCTION protect_member_profile_type() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF NEW.profile_type<>OLD.profile_type AND EXISTS(SELECT 1 FROM group_memberships WHERE group_profile_id=NEW.id OR person_profile_id=NEW.id) THEN
 RAISE EXCEPTION 'Remove or repair memberships before changing profile type';
 END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER member_profile_type BEFORE UPDATE OF profile_type ON profiles FOR EACH ROW EXECUTE FUNCTION protect_member_profile_type();

ALTER TABLE profile_address_revisions ADD COLUMN revision_seq BIGSERIAL;
ALTER TABLE profile_employment_revisions ADD COLUMN revision_seq BIGSERIAL;
ALTER TABLE profile_education_revisions ADD COLUMN revision_seq BIGSERIAL;
CREATE INDEX structured_employment_source ON profile_employment_revisions(profile_id,fact_key,revision_seq DESC);
CREATE INDEX structured_education_source ON profile_education_revisions(profile_id,fact_key,revision_seq DESC);
CREATE INDEX structured_address_source ON profile_address_revisions(profile_id,fact_key,revision_seq DESC);
CREATE TABLE source_reconciliation (
 source TEXT NOT NULL, stream TEXT NOT NULL, source_digest TEXT NOT NULL,
 source_count BIGINT NOT NULL, projected_count BIGINT NOT NULL, excluded_count BIGINT NOT NULL DEFAULT 0,
 missing_ids JSONB NOT NULL DEFAULT '[]', stage TEXT NOT NULL, details JSONB NOT NULL DEFAULT '{}',
 checked_at TIMESTAMPTZ NOT NULL DEFAULT now(), PRIMARY KEY(source,stream)
);
