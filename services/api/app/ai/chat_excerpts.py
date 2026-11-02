"""Read stored direct and group chat for a person's AI features."""

from datetime import datetime

from sqlalchemy import text

from ..post_text import excerpt

LINE_LENGTH = 200


def recent_speakers(db, since: datetime, min_direct: int, min_group: int) -> list[str]:
    """People with at least `min_direct` direct-chat lines or `min_group` lines of their own in groups since `since`.
    Counts only; no text is read."""
    rows = db.execute(text("""WITH direct AS (
          SELECT c.profile_id, count(*) AS n FROM conversations c
          JOIN message_search s ON s.conversation_id = c.id AND s.occurred_at >= :since
          WHERE c.conversation_type = 'direct' AND btrim(s.body) <> '' GROUP BY c.profile_id),
        senders AS (
          SELECT own.profile_id, m.conversation_id FROM source_account_links own
          JOIN message_events m ON m.sender_account_id = own.account_id AND m.occurred_at >= :since
          UNION ALL
          SELECT m.sender_profile_id, m.conversation_id FROM message_events m
          WHERE m.sender_profile_id IS NOT NULL AND m.sender_account_id IS NULL AND m.occurred_at >= :since),
        spoken AS (
          SELECT senders.profile_id, count(*) AS n FROM senders
          JOIN conversations c ON c.id = senders.conversation_id AND c.conversation_type = 'group' GROUP BY senders.profile_id)
        SELECT p.id::text FROM profiles p
        LEFT JOIN direct ON direct.profile_id = p.id LEFT JOIN spoken ON spoken.profile_id = p.id
        WHERE p.profile_type = 'person' AND p.archived_at IS NULL
          AND (coalesce(direct.n, 0) >= :min_direct OR coalesce(spoken.n, 0) >= :min_group)
        ORDER BY coalesce(direct.n, 0) + coalesce(spoken.n, 0) DESC, p.id"""),
                      {"since": since, "min_direct": min_direct, "min_group": min_group})
    return [row[0] for row in rows]


def direct_lines(db, profile_id: str, since: datetime, limit: int | None, until: datetime | None = None,
                 seen_since: datetime | None = None, full: bool = False) -> list[dict]:
    """Both sides of the direct chat, newest `limit` lines, oldest first."""
    rows = db.execute(text("""SELECT s.message_id::text AS message_id, s.occurred_at, m.ai_seen_at, s.conversation_id::text, s.body,
          coalesce(m.sender_account_id = c.source_account_id, false)
            OR coalesce(CASE WHEN m.sender_account_id IS NOT NULL THEN link.profile_id ELSE m.sender_profile_id END = c.profile_id,
                        false) AS theirs
        FROM conversations c
        JOIN message_search s ON s.conversation_id = c.id
        JOIN message_events m ON m.id = s.message_id AND m.occurred_at = s.occurred_at
        LEFT JOIN source_account_links link ON link.account_id = m.sender_account_id
        WHERE c.profile_id = CAST(:id AS uuid) AND c.conversation_type = 'direct' AND btrim(s.body) <> ''
          AND ((s.occurred_at >= :since AND (CAST(:until AS timestamptz) IS NULL OR s.occurred_at < :until))
            OR (CAST(:seen_since AS timestamptz) IS NOT NULL AND m.ai_seen_at >= :seen_since
              AND m.ai_seen_at < :until))
        ORDER BY s.occurred_at DESC LIMIT :limit"""), {"id": profile_id, "since": since, "until": until,
                                                        "seen_since": seen_since, "limit": limit}).mappings()
    return [{"message_id": row["message_id"], "conversation_id": row["conversation_id"], "occurred_at": row["occurred_at"],
             "ai_seen_at": row["ai_seen_at"],
             "speaker": "TA" if row["theirs"] else "我", "text": row["body"] if full else excerpt(row["body"], LINE_LENGTH)}
            for row in rows][::-1]


def group_lines(db, profile_id: str, since: datetime, limit: int | None, until: datetime | None = None,
                seen_since: datetime | None = None, full: bool = False) -> list[dict]:
    """What the person said in groups, newest `limit` lines, oldest first."""
    rows = db.execute(text("""WITH spoken AS (
          SELECT m.id, m.occurred_at, m.conversation_id FROM source_account_links own
          JOIN message_events m ON m.sender_account_id = own.account_id AND
            ((m.occurred_at >= :since AND (CAST(:until AS timestamptz) IS NULL OR m.occurred_at < :until))
              OR (CAST(:seen_since AS timestamptz) IS NOT NULL AND m.ai_seen_at >= :seen_since AND m.ai_seen_at < :until))
          WHERE own.profile_id = CAST(:id AS uuid)
          UNION ALL
          SELECT m.id, m.occurred_at, m.conversation_id FROM message_events m
          WHERE m.sender_profile_id = CAST(:id AS uuid) AND m.sender_account_id IS NULL AND
            ((m.occurred_at >= :since AND (CAST(:until AS timestamptz) IS NULL OR m.occurred_at < :until))
              OR (CAST(:seen_since AS timestamptz) IS NOT NULL AND m.ai_seen_at >= :seen_since AND m.ai_seen_at < :until)))
        SELECT s.message_id::text AS message_id, s.occurred_at, s.conversation_id::text, g.display_name AS group_name, s.body,
          m.ai_seen_at
        FROM spoken
        JOIN conversations c ON c.id = spoken.conversation_id AND c.conversation_type = 'group'
        JOIN message_search s ON s.message_id = spoken.id AND s.occurred_at = spoken.occurred_at
        JOIN message_events m ON m.id = spoken.id AND m.occurred_at = spoken.occurred_at
        LEFT JOIN profiles g ON g.id = c.profile_id
        WHERE btrim(s.body) <> ''
        ORDER BY s.occurred_at DESC LIMIT :limit"""), {"id": profile_id, "since": since, "until": until,
                                                        "seen_since": seen_since, "limit": limit}).mappings()
    return [{"message_id": row["message_id"], "conversation_id": row["conversation_id"], "occurred_at": row["occurred_at"],
             "ai_seen_at": row["ai_seen_at"],
             "group": row["group_name"] or "群聊", "text": row["body"] if full else excerpt(row["body"], LINE_LENGTH)}
            for row in rows][::-1]
