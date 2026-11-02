"""One person's relationship by month: private messages each way, calls, and their Moments likes and comments.

Only counts are read; no message or comment text.
"""

from uuid import UUID

from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.orm import Session

from .aggregate_cache import cached_endpoint
from .auth import current_user
from .db import get_db

router = APIRouter()

MONTHS = text("""
  WITH zone AS (
    SELECT coalesce(min(m.timezone), 'UTC') AS name
    FROM source_chat_months m JOIN source_account_links l ON l.account_id = m.account_id
    WHERE l.profile_id = :profile
  ), messages AS (
    SELECT to_char(m.month, 'YYYY-MM') AS month, sum(m.sent)::int AS sent, sum(m.received)::int AS received
    FROM source_chat_months m JOIN source_account_links l ON l.account_id = m.account_id
    WHERE l.profile_id = :profile GROUP BY 1
  ), calls AS (
    SELECT to_char(e.occurred_at AT TIME ZONE (SELECT name FROM zone), 'YYYY-MM') AS month, count(*)::int AS calls
    FROM source_contact_events e JOIN source_account_links l ON l.account_id = e.account_id
    WHERE l.profile_id = :profile AND e.event_kind = 'call' AND e.occurred_at IS NOT NULL GROUP BY 1
  ), interactions AS (
    SELECT to_char(i.occurred_at AT TIME ZONE (SELECT name FROM zone), 'YYYY-MM') AS month, count(*)::int AS interactions
    FROM social_interactions i JOIN social_posts p ON p.id = i.post_id
    WHERE i.author_profile_id = :profile AND i.interaction_type IN ('like', 'comment', 'reply') AND i.occurred_at IS NOT NULL
      AND p.profile_id IS DISTINCT FROM i.author_profile_id
    GROUP BY 1
  )
  SELECT month, coalesce(sent, 0) AS sent, coalesce(received, 0) AS received,
         coalesce(calls, 0) AS calls, coalesce(interactions, 0) AS interactions, (SELECT name FROM zone) AS timezone
  FROM messages FULL JOIN calls USING (month) FULL JOIN interactions USING (month)
  ORDER BY month
""")


def profile_ribbon(db, profile_id) -> dict:
    rows = [dict(row) for row in db.execute(MONTHS, {"profile": profile_id}).mappings()]
    timezone = rows[0]["timezone"] if rows else "UTC"
    return {"timezone": timezone, "months": [{key: row[key] for key in ("month", "sent", "received", "calls", "interactions")} for row in rows]}


@router.get("/api/v1/profiles/{profile_id}/ribbon")
@cached_endpoint("ribbon", ("messages", "contact_events", "links", "posts"))
def ribbon(profile_id: UUID, db: Session = Depends(get_db), _user=Depends(current_user)):
    return profile_ribbon(db, profile_id)
