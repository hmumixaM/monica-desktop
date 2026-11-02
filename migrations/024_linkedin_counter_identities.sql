-- Legacy importer wrote mutual-contact counters as LinkedIn account URLs.
-- Real numeric member IDs without this invalid URL signature are unaffected.
INSERT INTO repair_snapshots(repair_key,entity_type,entity_key,payload)
SELECT 'linkedin-counter-identities','identities',id::text,to_jsonb(i)
FROM identities i WHERE provider='linkedin' AND external_id ~ '^[0-9]+$'
  AND profile_url=external_id AND status NOT IN ('invalid','merged')
ON CONFLICT DO NOTHING;
INSERT INTO repair_snapshots(repair_key,entity_type,entity_key,payload)
SELECT 'linkedin-counter-identities','identities',i.id::text,to_jsonb(i)
FROM identities i WHERE i.provider='linkedin' AND i.profile_url=i.external_id
  AND EXISTS(SELECT 1 FROM profile_field_revisions r JOIN content_objects c ON c.id=r.content_object_id
    WHERE r.profile_id=i.profile_id AND r.field_key='monica.custom_fields'
      AND c.payload_json->'LinkedIn Location' @> jsonb_build_array(i.external_id))
ON CONFLICT DO NOTHING;
UPDATE identities SET status='invalid' WHERE provider='linkedin'
  AND id::text IN (SELECT entity_key FROM repair_snapshots WHERE repair_key='linkedin-counter-identities');
INSERT INTO source_account_link_revisions(account_id,old_profile_id,evidence)
SELECT l.account_id,l.profile_id,'{"kind":"invalid-counter-identity","repair":"linkedin-counter-identities"}'::jsonb
FROM source_account_links l JOIN source_accounts a ON a.id=l.account_id
WHERE a.provider='linkedin' AND EXISTS(SELECT 1 FROM identities i WHERE i.source_account_id=a.id AND i.status='invalid')
  AND NOT EXISTS(SELECT 1 FROM identities i WHERE i.source_account_id=a.id AND i.status NOT IN ('invalid','merged'));
DELETE FROM source_account_links l USING source_accounts a WHERE a.id=l.account_id AND a.provider='linkedin'
  AND EXISTS(SELECT 1 FROM identities i WHERE i.source_account_id=a.id AND i.status='invalid')
  AND NOT EXISTS(SELECT 1 FROM identities i WHERE i.source_account_id=a.id AND i.status NOT IN ('invalid','merged'));
