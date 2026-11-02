"""How many people you were in touch with on each day of a year, and who they were on one day.

Counts come from direct chats and calls; no message text is read.
"""

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import text
from sqlalchemy.orm import Session

from .aggregate_cache import cached_endpoint
from .auth import current_user
from .config import get_settings
from .data_versions import DOMAINS
from .db import get_db

router = APIRouter()

CONTACTS = """
  SELECT (e.occurred_at AT TIME ZONE :zone)::date AS day, c.profile_id, 1 AS messages, 0 AS calls
  FROM message_events e JOIN conversations c ON c.id = e.conversation_id
  WHERE c.conversation_type = 'direct' AND c.profile_id IS NOT NULL AND e.occurred_at >= :start AND e.occurred_at < :end
  UNION ALL
  SELECT (e.occurred_at AT TIME ZONE :zone)::date, l.profile_id, 0, 1
  FROM source_contact_events e JOIN source_account_links l ON l.account_id = e.account_id
  WHERE e.event_kind = 'call' AND e.occurred_at >= :start AND e.occurred_at < :end
"""


def bounds(first: date, last: date, zone: str) -> dict:
    tz = ZoneInfo(zone)
    return {"start": datetime.combine(first, time.min, tz), "end": datetime.combine(last + timedelta(days=1), time.min, tz), "zone": zone}


def year_counts(db, year: int, zone: str) -> dict:
    params = bounds(date(year, 1, 1), date(year, 12, 31), zone)
    days = [{"date": row.day, "people": int(row.people)} for row in db.execute(text(f"""
        WITH contacts AS ({CONTACTS})
        SELECT day, count(DISTINCT contacts.profile_id) AS people FROM contacts
        JOIN profiles p ON p.id = contacts.profile_id AND p.archived_at IS NULL AND p.profile_type = 'person'
        GROUP BY day ORDER BY day"""), params)]
    first, last = db.execute(text("SELECT min(month), max(month) FROM conversation_message_months")).one()
    years = list(range(first.year, max(last.year, year) + 1)) if first else [year]
    return {"year": year, "years": years, "days": days, "max": max((day["people"] for day in days), default=0)}


def day_people(db, day: date, zone: str) -> list[dict]:
    return [{"profile_id": str(row.id), "name": row.display_name,
             "avatar_media_id": str(row.avatar_media_id) if row.avatar_media_id else None,
             "messages": int(row.messages), "calls": int(row.calls)}
            for row in db.execute(text(f"""
        WITH contacts AS ({CONTACTS})
        SELECT p.id, p.display_name, profile_avatar_media(p.id, p.avatar_media_id) AS avatar_media_id,
          sum(contacts.messages) AS messages, sum(contacts.calls) AS calls
        FROM contacts JOIN profiles p ON p.id = contacts.profile_id AND p.archived_at IS NULL AND p.profile_type = 'person'
        GROUP BY p.id ORDER BY sum(contacts.calls) DESC, sum(contacts.messages) DESC, p.display_name"""), bounds(day, day, zone))]


@router.get("/api/v1/insights/calendar")
@cached_endpoint("contact-calendar", DOMAINS, ttl=3600, stale_on_change=True)
def contact_calendar(year: int | None = None, db: Session = Depends(get_db), _user=Depends(current_user)):
    zone = get_settings().crm_timezone
    today = datetime.now(ZoneInfo(zone)).date()
    year = year or today.year
    if not 2000 <= year <= today.year:
        raise HTTPException(status_code=422, detail="year is out of range")
    return year_counts(db, year, zone)


@router.get("/api/v1/insights/calendar/day")
def contact_calendar_day(day: date, db: Session = Depends(get_db), _user=Depends(current_user)):
    return {"date": day, "people": day_people(db, day, get_settings().crm_timezone)}
