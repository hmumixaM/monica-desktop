-- Old Monica labels such as LinkedIn About/Location were once treated as IDs.
-- Keep evidence and field history; quarantine only values with no account syntax.
INSERT INTO repair_snapshots(repair_key,entity_type,entity_key,payload)
SELECT 'social-identity-v2','identities',i.id::text,to_jsonb(i) FROM identities i
WHERE provider IN ('linkedin','instagram') AND status NOT IN ('invalid','merged')
AND external_id !~ '^[A-Za-z0-9_.-]+$'
AND external_id !~ '^username:[A-Za-z0-9_.]+$'
AND external_id !~ '^urn:li:(fsd_profile|member):[A-Za-z0-9_-]+$'
AND COALESCE(profile_url,external_id) !~* '^https://([a-z]+\.)?linkedin\.com/in/[^/?#[:space:]]+/?([?#].*)?$'
AND COALESCE(profile_url,external_id) !~* '^https://(www\.)?instagram\.com/[A-Za-z0-9_.]+/?([?#].*)?$'
ON CONFLICT DO NOTHING;
UPDATE identities i SET status='invalid' FROM repair_snapshots s
WHERE s.repair_key='social-identity-v2' AND s.entity_type='identities' AND s.entity_key=i.id::text;
-- The old one-per-type pointer coexisted with the newer stable fact pointer.
-- Remove only a byte-identical legacy current pointer, preserving all revisions.
INSERT INTO repair_snapshots(repair_key,entity_type,entity_key,payload)
SELECT 'legacy-current-v2','profile_structured_current',c.profile_id::text||':'||c.fact_type||':'||c.fact_key,to_jsonb(c)
FROM profile_structured_current c WHERE c.fact_key='legacy' AND EXISTS(
 SELECT 1 FROM profile_structured_current n WHERE n.profile_id=c.profile_id AND n.fact_type=c.fact_type
 AND n.fact_key<>'legacy' AND n.current_source_type=c.current_source_type AND n.current_content_hash=c.current_content_hash)
ON CONFLICT DO NOTHING;
DELETE FROM profile_structured_current c WHERE c.fact_key='legacy' AND EXISTS(
 SELECT 1 FROM profile_structured_current n WHERE n.profile_id=c.profile_id AND n.fact_type=c.fact_type
 AND n.fact_key<>'legacy' AND n.current_source_type=c.current_source_type AND n.current_content_hash=c.current_content_hash);
