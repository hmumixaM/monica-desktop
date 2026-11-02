"""Live check for source accounts whose projected data cannot be moved safely.

An account can be attached to a Profile only when every projection on its
current Profile is attributed to a source account. Unattributed avatars, tags,
or relationship edges would stay behind or be duplicated when the account moves.
"""
from sqlalchemy import text

BLOCKER_REASONS = {
    "quarantined": "This legacy identity is quarantined and cannot be linked",
    "unowned_media": "The current profile has an avatar or cover without a source account",
    "unowned_tags": "The current profile has platform tags without a source account",
    "unowned_relationships": "The current profile has platform relationships without a source account",
}

_BLOCKERS = text("""
  WITH acc AS (
    SELECT a.id, a.provider, l.profile_id
    FROM source_accounts a LEFT JOIN source_account_links l ON l.account_id=a.id
    WHERE CAST(:ids AS uuid[]) IS NULL OR a.id=ANY(CAST(:ids AS uuid[]))
  ),
  quarantined AS (
    SELECT i.source_account_id AS id FROM identities i
    WHERE i.source_account_id IN (SELECT id FROM acc)
    GROUP BY 1
    HAVING bool_or(i.status='invalid') AND bool_and(i.status IN ('invalid','merged'))
  ),
  media AS (
    SELECT DISTINCT m.entity_id AS profile_id,
      CASE m.role WHEN 'instagram_avatar' THEN 'instagram' WHEN 'linkedin_avatar' THEN 'linkedin' ELSE 'wechat' END AS provider
    FROM media_links m
    WHERE m.entity_type='profile' AND m.source_account_id IS NULL
      AND m.role IN ('cover','wechat_avatar','instagram_avatar','linkedin_avatar')
      AND m.entity_id IN (SELECT profile_id FROM acc)
  ),
  tags AS (
    SELECT DISTINCT t.profile_id, t.source_type AS provider FROM tag_memberships t
    WHERE t.source_account_id IS NULL AND t.profile_id IN (SELECT profile_id FROM acc)
  ),
  edges AS (
    SELECT DISTINCT v.profile_id, e.source_type AS provider
    FROM relationship_edges e
    CROSS JOIN LATERAL (VALUES (e.from_profile_id), (e.to_profile_id)) v(profile_id)
    WHERE e.evidence_kind='relationship' AND (e.from_account_id IS NULL OR e.to_account_id IS NULL)
      AND v.profile_id IN (SELECT profile_id FROM acc)
  )
  SELECT acc.id, 'quarantined' AS reason, 0 AS rank FROM acc JOIN quarantined q ON q.id=acc.id
  UNION ALL SELECT acc.id, 'unowned_media', 1 FROM acc JOIN media USING (profile_id, provider)
  UNION ALL SELECT acc.id, 'unowned_tags', 2 FROM acc JOIN tags USING (profile_id, provider)
  UNION ALL SELECT acc.id, 'unowned_relationships', 3 FROM acc JOIN edges USING (profile_id, provider)
  ORDER BY rank
""")


def link_blockers(db, account_ids=None) -> dict[str, str]:
    """Map each blocked account ID to its most important blocker code.

    ``account_ids=None`` checks every source account in one pass.
    """
    ids = None if account_ids is None else [str(item) for item in account_ids]
    blockers: dict[str, str] = {}
    for row in db.execute(_BLOCKERS, {"ids": ids}):
        blockers.setdefault(str(row.id), row.reason)
    return blockers
