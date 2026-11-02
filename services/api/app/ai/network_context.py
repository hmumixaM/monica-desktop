"""A bounded, focused social graph for models: aggregate counts, never comment/chat bodies."""

from datetime import UTC, datetime, timedelta

from sqlalchemy import text

LIMIT = 8
WINDOW_DAYS = 365


def network_context(db, profile_id: str, *, today=None) -> dict:
    end = datetime.combine(today, datetime.min.time(), UTC) if today else datetime.now(UTC)
    since = end - timedelta(days=WINDOW_DAYS)
    # Each branch uses an existing focus index. Do not build or lay out the full graph for a summary.
    rows = db.execute(text("""WITH accounts AS MATERIALIZED (
        SELECT external_id FROM identities WHERE profile_id=CAST(:id AS uuid)
          AND provider='wechat' AND status NOT IN ('invalid','merged')),
      relevant AS MATERIALIZED (
        SELECT s.id,s.post_id,s.author_profile_id,s.interaction_type,s.occurred_at,s.target_external_id,s.ref_author_external_id
        FROM social_interactions s WHERE s.author_profile_id=CAST(:id AS uuid)
          AND s.provider='wechat' AND NOT s.is_deleted AND s.occurred_at>=:since
        UNION
        SELECT s.id,s.post_id,s.author_profile_id,s.interaction_type,s.occurred_at,s.target_external_id,s.ref_author_external_id
        FROM social_posts p JOIN social_interactions s ON s.post_id=p.id
        WHERE p.profile_id=CAST(:id AS uuid) AND s.provider='wechat' AND NOT s.is_deleted AND s.occurred_at>=:since
        UNION
        SELECT s.id,s.post_id,s.author_profile_id,s.interaction_type,s.occurred_at,s.target_external_id,s.ref_author_external_id
        FROM accounts a JOIN social_interactions s ON s.target_external_id=a.external_id
        WHERE s.provider='wechat' AND s.interaction_type='mention' AND NOT s.is_deleted AND s.occurred_at>=:since
        UNION
        SELECT s.id,s.post_id,s.author_profile_id,s.interaction_type,s.occurred_at,s.target_external_id,s.ref_author_external_id
        FROM accounts a JOIN social_interactions s ON s.ref_author_external_id=a.external_id
        WHERE s.provider='wechat' AND s.interaction_type='reply' AND NOT s.is_deleted AND s.occurred_at>=:since),
      edges AS (
        SELECT s.id,s.author_profile_id AS a,
          CASE WHEN s.interaction_type='mention' THEN recipient.profile_id ELSE p.profile_id END AS b,
          s.interaction_type AS kind,s.occurred_at
        FROM relevant s JOIN social_posts p ON p.id=s.post_id
        LEFT JOIN identities recipient ON recipient.provider='wechat' AND recipient.external_id=s.target_external_id
          AND recipient.status NOT IN ('invalid','merged')
        UNION
        SELECT s.id,s.author_profile_id,recipient.profile_id,'reply',s.occurred_at
        FROM relevant s JOIN identities recipient ON recipient.provider='wechat'
          AND recipient.external_id=s.ref_author_external_id AND recipient.status NOT IN ('invalid','merged')
        WHERE s.interaction_type='reply'),
      focused AS (
        SELECT DISTINCT id,a,b,kind,occurred_at FROM edges
        WHERE a<>b AND (a=CAST(:id AS uuid) OR b=CAST(:id AS uuid))),
      counts AS (
        SELECT CASE WHEN a=CAST(:id AS uuid) THEN b ELSE a END AS peer,
          count(*) AS interactions,count(*) FILTER(WHERE a=CAST(:id AS uuid)) AS outgoing,
          count(*) FILTER(WHERE b=CAST(:id AS uuid)) AS incoming,
          count(*) FILTER(WHERE kind='like') AS likes,
          count(*) FILTER(WHERE kind IN ('comment','reply')) AS comments_and_replies,
          count(*) FILTER(WHERE kind='mention') AS mentions,max(occurred_at) AS latest
        FROM focused GROUP BY 1)
      SELECT p.id::text AS profile_id,p.display_name AS name,c.interactions,c.outgoing,c.incoming,c.likes,
        c.comments_and_replies,c.mentions,c.latest
      FROM counts c JOIN profiles p ON p.id=c.peer AND p.profile_type='person' AND p.archived_at IS NULL
      ORDER BY c.interactions DESC,c.latest DESC,p.id LIMIT :limit"""),
                      {"id": profile_id, "since": since, "limit": LIMIT}).mappings()
    peers = [{**{key: row[key] for key in ("profile_id", "name", "interactions", "outgoing", "incoming", "likes", "comments_and_replies", "mentions")},
              "last_interaction": row["latest"].date().isoformat()} for row in rows]
    relationships = db.execute(text("""SELECT p.id::text AS profile_id,p.display_name AS name,t.name AS relationship,
        CASE WHEN e.from_profile_id=CAST(:id AS uuid) THEN 'outgoing' ELSE 'incoming' END AS direction
        FROM relationship_edges e JOIN relationship_types t ON t.id=e.relationship_type_id
        JOIN profiles p ON p.id=CASE WHEN e.from_profile_id=CAST(:id AS uuid) THEN e.to_profile_id ELSE e.from_profile_id END
        WHERE (e.from_profile_id=CAST(:id AS uuid) OR e.to_profile_id=CAST(:id AS uuid))
          AND e.evidence_kind='relationship' AND (e.valid_from IS NULL OR e.valid_from<=now())
          AND (e.valid_to IS NULL OR e.valid_to>now())
          AND p.id<>CAST(:id AS uuid) AND p.profile_type='person' AND p.archived_at IS NULL
        ORDER BY e.created_at DESC,e.id LIMIT :limit"""), {"id": profile_id, "limit": LIMIT}).mappings()
    return {"scope": "微信朋友圈点赞、评论、回复和提及；不含聊天正文。同群不算互动。",
            "window_days": WINDOW_DAYS, "since": since.date().isoformat(), "top_peers": peers,
            "recorded_relationships": [dict(row) for row in relationships]}
