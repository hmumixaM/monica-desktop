-- Historical graph aggregates are retained as evidence. Current interaction
-- graphs are calculated from actual likes/comments and current account links.
ALTER TABLE relationship_edges DROP CONSTRAINT relationship_edges_evidence_kind_check;
ALTER TABLE relationship_edges ADD CONSTRAINT relationship_edges_evidence_kind_check
  CHECK(evidence_kind IN ('relationship','contact_share','legacy_group','legacy_interaction'));
CREATE OR REPLACE FUNCTION classify_relationship_evidence() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE relation_name TEXT;
BEGIN
 SELECT name INTO relation_name FROM relationship_types WHERE id=NEW.relationship_type_id;
 IF relation_name='微信名片推荐' THEN NEW.evidence_kind='contact_share';
 ELSIF relation_name='微信朋友圈互动' AND NEW.source_type='wechat' THEN NEW.evidence_kind='legacy_interaction';
 ELSIF EXISTS (SELECT 1 FROM profiles WHERE id IN (NEW.from_profile_id,NEW.to_profile_id) AND profile_type='group')
   OR lower(relation_name) ~ '(group|member|owner|群)' THEN NEW.evidence_kind='legacy_group';
 ELSE NEW.evidence_kind='relationship'; END IF;
 RETURN NEW;
END $$;
UPDATE relationship_edges SET relationship_type_id=relationship_type_id
WHERE source_type='wechat' AND relationship_type_id IN (SELECT id FROM relationship_types WHERE name='微信朋友圈互动');
