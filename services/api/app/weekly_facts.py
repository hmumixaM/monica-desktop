"""What happened in one week, from counts and dates only: no message text is read."""

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import text

from .insights import significant_dates

TOP_CONTACTS = 8
LISTED = 12
DORMANT_DAYS = 90
DORMANT_FLOOR = 100


def _rows(db, sql: str, params: dict) -> list[dict]:
    return [{key: str(value) if key.endswith("_id") and value is not None else value for key, value in row.items()}
            for row in db.execute(text(sql), params).mappings()]


def week_facts(db, week_start: date, zone: str) -> dict:
    """The week from Monday `week_start` 00:00 local time, and the dates in the week after it."""
    start = datetime.combine(week_start, time(), ZoneInfo(zone))
    end = start + timedelta(days=7)
    window = {"start": start, "end": end}
    top = _rows(db, """WITH counts AS (
          SELECT c.profile_id, count(*) AS messages FROM message_events e
          JOIN conversations c ON c.id = e.conversation_id AND c.conversation_type = 'direct' AND c.profile_id IS NOT NULL
          WHERE e.occurred_at >= :start AND e.occurred_at < :end GROUP BY c.profile_id),
        calls AS (
          SELECT l.profile_id, count(*) AS calls FROM source_contact_events e JOIN source_account_links l ON l.account_id = e.account_id
          WHERE e.event_kind = 'call' AND e.occurred_at >= :start AND e.occurred_at < :end GROUP BY l.profile_id)
        SELECT p.id AS profile_id, p.display_name AS name, profile_avatar_media(p.id, p.avatar_media_id) AS avatar_media_id,
          COALESCE(counts.messages, 0) AS messages, COALESCE(calls.calls, 0) AS calls
        FROM counts FULL JOIN calls USING (profile_id)
        JOIN profiles p ON p.id = profile_id AND p.profile_type = 'person' AND p.archived_at IS NULL
        ORDER BY COALESCE(counts.messages, 0) + 10 * COALESCE(calls.calls, 0) DESC LIMIT :limit""",
                {**window, "limit": TOP_CONTACTS})
    totals = db.execute(text("""SELECT
          (SELECT count(*) FROM message_events WHERE occurred_at >= :start AND occurred_at < :end) AS messages,
          (SELECT count(DISTINCT c.profile_id) FROM message_events e JOIN conversations c ON c.id = e.conversation_id
            WHERE c.conversation_type = 'direct' AND e.occurred_at >= :start AND e.occurred_at < :end) AS people,
          (SELECT count(*) FROM social_posts WHERE occurred_at >= :start AND occurred_at < :end) AS posts"""), window).mappings().one()
    new_people = _rows(db, """SELECT id AS profile_id, display_name AS name FROM profiles
        WHERE profile_type = 'person' AND archived_at IS NULL AND created_at >= :start AND created_at < :end
        ORDER BY created_at DESC LIMIT :limit""", {**window, "limit": LISTED})
    new_links = _rows(db, """SELECT p.id AS profile_id, p.display_name AS name, a.provider FROM source_account_links l
        JOIN source_accounts a ON a.id = l.account_id JOIN profiles p ON p.id = l.profile_id AND p.archived_at IS NULL
        WHERE l.linked_at >= :start AND l.linked_at < :end ORDER BY l.linked_at DESC LIMIT :limit""", {**window, "limit": LISTED})
    dormant = _rows(db, """SELECT p.id AS profile_id, p.display_name AS name, s.last_contact_at, s.direct_message_count AS messages
        FROM profile_contact_stats s JOIN profiles p ON p.id = s.profile_id AND p.profile_type = 'person' AND p.archived_at IS NULL
        WHERE s.last_contact_at >= :start - make_interval(days => :days) AND s.last_contact_at < :end - make_interval(days => :days)
          AND s.direct_message_count >= :floor ORDER BY s.direct_message_count DESC LIMIT :limit""",
                    {**window, "days": DORMANT_DAYS, "floor": DORMANT_FLOOR, "limit": LISTED})
    upcoming, _ = significant_dates(db, end.date(), ahead=6)
    return {
        "week_start": week_start.isoformat(),
        "totals": {key: int(value) for key, value in totals.items()},
        "top_contacts": [{**row, "messages": int(row["messages"]), "calls": int(row["calls"])} for row in top],
        "new_people": new_people,
        "new_links": new_links,
        "upcoming_dates": upcoming,
        "newly_dormant": [{**row, "messages": int(row["messages"])} for row in dormant],
    }
