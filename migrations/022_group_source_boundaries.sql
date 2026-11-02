-- Keep the erroneous projection and its immutable revisions for import audit.
-- A room cannot own a person's Moments, even when it was once linked to the
-- room owner's identity by the legacy importer.
INSERT INTO repair_snapshots(repair_key,entity_type,entity_key,payload)
SELECT 'group-source-boundaries-v1','profile_field_current',c.profile_id::text||':'||c.field_key,to_jsonb(c)
FROM profile_field_current c
JOIN profiles p ON p.id=c.profile_id
JOIN profile_field_revisions r ON r.id=c.current_revision_id
WHERE c.field_key IN ('wechat.location_history','wechat.moments_summary')
AND (p.profile_type='group' OR EXISTS (
  SELECT 1 FROM identities i WHERE i.provider='wechat' AND i.status NOT IN ('invalid','merged')
  AND i.external_id=regexp_replace(r.source_record_id,'^moments-(locations|node):','')
  AND i.profile_id<>c.profile_id
)) ON CONFLICT DO NOTHING;

DELETE FROM profile_field_current c USING repair_snapshots s
WHERE s.repair_key='group-source-boundaries-v1' AND s.entity_type='profile_field_current'
AND s.entity_key=c.profile_id::text||':'||c.field_key
AND s.payload->>'current_revision_id'=c.current_revision_id::text;

CREATE FUNCTION check_personal_social_field() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF NEW.field_key IN ('wechat.location_history','wechat.moments_summary') AND EXISTS (
   SELECT 1 FROM profiles WHERE id=NEW.profile_id AND profile_type='group'
 ) THEN RAISE EXCEPTION 'A group cannot own personal Moments fields'; END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER personal_social_field BEFORE INSERT OR UPDATE ON profile_field_current
FOR EACH ROW EXECUTE FUNCTION check_personal_social_field();

-- Forwarding a contact card is evidence of a share, not of friendship.
-- Preserve edges and their revision history, but distinguish this evidence
-- from explicit person-to-person relationships in every graph/query.
ALTER TABLE relationship_edges ADD COLUMN evidence_kind TEXT NOT NULL DEFAULT 'relationship'
  CHECK (evidence_kind IN ('relationship','contact_share','legacy_group'));
CREATE FUNCTION classify_relationship_evidence() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE relation_name TEXT;
BEGIN
 SELECT name INTO relation_name FROM relationship_types WHERE id=NEW.relationship_type_id;
 IF relation_name='微信名片推荐' THEN NEW.evidence_kind='contact_share';
 ELSIF EXISTS (SELECT 1 FROM profiles WHERE id IN (NEW.from_profile_id,NEW.to_profile_id) AND profile_type='group')
   OR lower(relation_name) ~ '(group|member|owner|群)' THEN NEW.evidence_kind='legacy_group';
 ELSE NEW.evidence_kind='relationship'; END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER relationship_evidence BEFORE INSERT OR UPDATE OF from_profile_id,to_profile_id,relationship_type_id
ON relationship_edges FOR EACH ROW EXECUTE FUNCTION classify_relationship_evidence();
UPDATE relationship_edges SET relationship_type_id=relationship_type_id;

CREATE INDEX tag_memberships_profile_idx ON tag_memberships(profile_id,tag_id);
