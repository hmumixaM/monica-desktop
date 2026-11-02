"""Global search across profiles, tags, social posts and stories, and records.

Each kind is limited on its own so a common word in captions cannot crowd out the people who match.
Chat message bodies are searched inside a conversation, not here.
"""

from sqlalchemy import text
from sqlalchemy.orm import Session

SEARCH_SQL = text(r"""
  (SELECT p.id, 'profile' AS entity_type, p.display_name AS title, COALESCE(p.summary, '') AS summary,
          NULL::timestamptz AS occurred_at, p.id AS profile_id, p.display_name AS profile_name,
          p.profile_type AS kind, profile_avatar_media(p.id, p.avatar_media_id) AS avatar_media_id, NULL::text AS provider
   FROM profiles p
   WHERE p.archived_at IS NULL AND (p.display_name ILIKE :like OR p.summary ILIKE :like)
   ORDER BY (p.display_name ILIKE :prefix) DESC, length(p.display_name), p.display_name
   LIMIT :limit)
  UNION ALL
  (SELECT t.id, 'tag', t.name, '', NULL, NULL, NULL, NULL, NULL, NULL
   FROM tags t WHERE t.name ILIKE :like
   ORDER BY length(t.name), t.name
   LIMIT :limit)
  UNION ALL
  (SELECT x.id, 'timeline', COALESCE(owner.display_name, x.provider), left(x.search_body, 240), x.occurred_at,
          x.profile_id, owner.display_name, x.kind,
          CASE WHEN owner.id IS NULL THEN NULL ELSE profile_avatar_media(owner.id, owner.avatar_media_id) END, x.provider
   FROM (SELECT id, profile_id, provider, search_body, occurred_at, 'post' AS kind FROM social_posts WHERE search_body ILIKE :like
         UNION ALL
         SELECT id, profile_id, provider, search_body, occurred_at, 'story' FROM social_stories WHERE search_body ILIKE :like) x
   LEFT JOIN profiles owner ON owner.id = x.profile_id
   ORDER BY x.occurred_at DESC NULLS LAST
   LIMIT :limit)
  UNION ALL
  (SELECT a.id, 'activity', a.title, COALESCE(a.body, ''), a.occurred_at, who.id, who.display_name, a.activity_type, NULL, a.source_type
   FROM activities a
   LEFT JOIN LATERAL (
     SELECT p.id, p.display_name FROM activity_participants ap JOIN profiles p ON p.id = ap.profile_id
     WHERE ap.activity_id = a.id ORDER BY p.display_name LIMIT 1
   ) who ON TRUE
   WHERE a.activity_type NOT IN ('wechat_direct_summary', 'wechat_group_summary')
     AND (a.title ILIKE :like OR a.body ILIKE :like)
   ORDER BY a.occurred_at DESC
   LIMIT :limit)
""")


def escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", r"\%").replace("_", r"\_")


def search(db: Session, query: str, limit: int = 30) -> list[dict]:
    """Up to `limit` matches of each kind; queries shorter than two characters return nothing."""
    needle = query.strip()
    if len(needle) < 2:
        return []
    escaped = escape_like(needle)
    params = {"like": f"%{escaped}%", "prefix": f"{escaped}%", "limit": min(max(limit, 1), 100)}
    return [dict(row) for row in db.execute(SEARCH_SQL, params).mappings()]
