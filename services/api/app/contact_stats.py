"""Recompute message statistics for conversations marked dirty by new messages: monthly
direct-chat counts, and per-sender counts that give a person's last line in a group."""

import json

from sqlalchemy import text

from .config import get_settings
from .db import SessionLocal

PREVIEW_LENGTH = 80

# For a direct conversation the counterpart's account (or, for legacy rows
# without accounts, its linked profile) sent the message; anything else is ours.
RECEIVED = """(e.sender_account_id = c.source_account_id
    OR (e.sender_account_id IS NULL AND e.sender_profile_id IS NOT DISTINCT FROM c.profile_id
        AND c.profile_id IS NOT NULL))"""


def refresh(db, batch_size=50) -> int:
    timezone = get_settings().crm_timezone
    refreshed = 0
    while True:
        ids = db.execute(text("""DELETE FROM conversation_stats_dirty WHERE conversation_id IN (
            SELECT conversation_id FROM conversation_stats_dirty ORDER BY conversation_id
            LIMIT :limit FOR UPDATE SKIP LOCKED) RETURNING conversation_id"""), {"limit": batch_size}).scalars().all()
        if not ids:
            return refreshed
        params = {"ids": ids, "zone": timezone, "preview": PREVIEW_LENGTH}
        db.execute(text("DELETE FROM conversation_message_months WHERE conversation_id = ANY(:ids)"), params)
        db.execute(text(f"""INSERT INTO conversation_message_months(conversation_id,month,received,sent,first_at,last_at)
            SELECT e.conversation_id, date_trunc('month', e.occurred_at AT TIME ZONE :zone)::date,
              count(*) FILTER (WHERE {RECEIVED}), count(*) FILTER (WHERE NOT {RECEIVED}),
              min(e.occurred_at), max(e.occurred_at)
            FROM message_events e JOIN conversations c ON c.id = e.conversation_id
            WHERE e.conversation_id = ANY(:ids) AND c.conversation_type = 'direct'
            GROUP BY 1, 2"""), params)
        db.execute(text("DELETE FROM conversation_sender_stats WHERE conversation_id = ANY(:ids)"), params)
        db.execute(text("""INSERT INTO conversation_sender_stats(conversation_id, sender_account_id, sender_profile_id, messages, last_at)
            SELECT conversation_id, sender_account_id, CASE WHEN sender_account_id IS NULL THEN sender_profile_id END,
              count(*), max(occurred_at)
            FROM message_events WHERE conversation_id = ANY(:ids)
              AND (sender_account_id IS NOT NULL OR sender_profile_id IS NOT NULL)
            GROUP BY 1, 2, 3"""), params)
        db.execute(text("""UPDATE conversations c SET message_count = s.total, last_message_at = s.last_at,
              last_message_preview = p.preview
            FROM unnest(CAST(:ids AS uuid[])) AS target(id)
            CROSS JOIN LATERAL (SELECT count(*) AS total, max(occurred_at) AS last_at
              FROM message_events WHERE conversation_id = target.id) s
            LEFT JOIN LATERAL (SELECT left(btrim(regexp_replace(body, '\\s+', ' ', 'g')), :preview) AS preview
              FROM message_search WHERE conversation_id = target.id AND btrim(body) <> ''
              ORDER BY occurred_at DESC, message_id DESC LIMIT 1) p ON true
            WHERE c.id = target.id
              AND (c.message_count, c.last_message_at, c.last_message_preview)
                IS DISTINCT FROM (s.total, s.last_at, p.preview)"""), params)
        db.commit()
        refreshed += len(ids)
        if refreshed % 500 < batch_size:
            print(json.dumps({"conversation_stats_refreshed": refreshed}), flush=True)


if __name__ == "__main__":
    from .jobs import heavy_io, lower_priority

    lower_priority()
    with heavy_io("contact_stats"), SessionLocal() as session:
        print(json.dumps({"conversation_stats_refreshed": refresh(session)}), flush=True)
