-- Read-only checkpoint before/after any migration. Counts are evidence, not completeness claims.
BEGIN READ ONLY;
SELECT 'active_profiles' AS metric, count(*) AS value FROM profiles WHERE archived_at IS NULL
UNION ALL SELECT 'active_groups', count(*) FROM profiles WHERE profile_type='group' AND archived_at IS NULL
UNION ALL SELECT 'invalid_membership_types', count(*) FROM group_memberships m
  JOIN profiles g ON g.id=m.group_profile_id JOIN profiles p ON p.id=m.person_profile_id
  WHERE g.profile_type<>'group' OR p.profile_type<>'person' OR g.id=p.id
UNION ALL SELECT 'structured_messages', count(*) FROM message_events
UNION ALL SELECT 'media_assets', count(*) FROM media_assets
UNION ALL SELECT 'media_links', count(*) FROM media_links;
SELECT provider, count(*) AS posts, count(occurred_at) AS posts_with_time
FROM social_posts GROUP BY provider ORDER BY provider;
SELECT p.provider,count(DISTINCT p.id) AS posts_with_local_media
FROM social_posts p JOIN media_links l ON l.entity_id=p.id AND l.entity_type IN ('post','social_post')
JOIN media_assets a ON a.id=l.media_id GROUP BY p.provider ORDER BY p.provider;
SELECT source,stream,source_count,projected_count,excluded_count,stage,checked_at
FROM source_reconciliation ORDER BY source,stream;
SELECT count(*) AS source_tables,sum(source_count) AS source_rows,sum(projected_count) AS projected_rows
FROM message_source_cursors;
SELECT count(*) AS searchable_messages FROM message_search;
SELECT count(*) AS conversations_without_profile FROM conversations WHERE profile_id IS NULL;
SELECT CASE WHEN source_url LIKE '%licdn.com%' THEN 'linkedin'
            WHEN source_url LIKE '%cdninstagram.com%' OR source_url LIKE '%fbcdn.net%' THEN 'instagram'
            ELSE 'wechat/other' END AS provider,state,count(*)
FROM media_manifest GROUP BY 1,2 ORDER BY 1,2;
SELECT job_type, status, count(*) FROM jobs GROUP BY job_type,status ORDER BY job_type,status;
COMMIT;
