-- WeChat never ran `platform_migrate verify --activate`, so accounts outside the contact sync kept
-- migrated_at NULL although their projections are fully attributed. Linking now checks ownership
-- live (app/link_ownership.py); mark every WeChat account that passes the same checks as migrated.
WITH acc AS (
  SELECT a.id, l.profile_id
  FROM source_accounts a LEFT JOIN source_account_links l ON l.account_id=a.id
  WHERE a.provider='wechat' AND a.migrated_at IS NULL
),
blocked AS (
  SELECT i.source_account_id AS id FROM identities i
  WHERE i.source_account_id IN (SELECT id FROM acc)
  GROUP BY 1
  HAVING bool_or(i.status='invalid') AND bool_and(i.status IN ('invalid','merged'))
  UNION
  SELECT acc.id FROM acc JOIN media_links m ON m.entity_type='profile' AND m.entity_id=acc.profile_id
  WHERE m.source_account_id IS NULL AND m.role IN ('cover','wechat_avatar')
  UNION
  SELECT acc.id FROM acc JOIN tag_memberships t ON t.profile_id=acc.profile_id
  WHERE t.source_type='wechat' AND t.source_account_id IS NULL
  UNION
  SELECT acc.id FROM acc JOIN (
    SELECT v.profile_id FROM relationship_edges e
    CROSS JOIN LATERAL (VALUES (e.from_profile_id), (e.to_profile_id)) v(profile_id)
    WHERE e.source_type='wechat' AND e.evidence_kind='relationship'
      AND (e.from_account_id IS NULL OR e.to_account_id IS NULL)
  ) edge USING (profile_id)
)
UPDATE source_accounts a SET migrated_at=now()
FROM acc WHERE a.id=acc.id AND acc.id NOT IN (SELECT id FROM blocked);
