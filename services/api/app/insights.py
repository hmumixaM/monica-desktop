"""People who need attention: dormant ties, significant dates, recent posters and recent contacts."""

from collections import defaultdict
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import text
from sqlalchemy.orm import Session

from .aggregate_cache import cached_endpoint
from .auth import current_user
from .config import get_settings
from .data_versions import DOMAINS
from .db import get_db
from .profile_facts import compact, month_series, next_occurrence
from .remote_media import linked_media

router = APIRouter()

DORMANT_THRESHOLDS = (90, 180, 365)
DORMANT_PEOPLE = 60
DATES_AHEAD = 60
DATES_BEHIND = 7
POSTERS_DAYS = 14
POSTERS = 60
RECENT_PEOPLE = 30
RECENT_MONTHS = 12
# A call is a stronger signal of closeness than a single chat message.
CALL_WEIGHT = 10
TOP_ORGANIZATIONS = 10

PERSON = """SELECT p.id, p.display_name, profile_avatar_media(p.id, p.avatar_media_id) AS avatar_media_id,
    s.last_contact_at, s.direct_message_count, s.last_group_at, s.group_message_count"""


def _person(row, **extra):
    return compact({"profile_id": str(row["id"]), "name": row["display_name"],
                    "avatar_media_id": str(row["avatar_media_id"]) if row["avatar_media_id"] else None,
                    "last_contact_at": row["last_contact_at"], "last_group_at": row["last_group_at"],
                    "direct_message_count": int(row["direct_message_count"] or 0),
                    "group_message_count": int(row["group_message_count"] or 0), **extra})


def recent_contacts(db, today, limit=RECENT_PEOPLE):
    rows = db.execute(text(f"""{PERSON} FROM profile_contact_stats s
        JOIN profiles p ON p.id = s.profile_id AND p.profile_type = 'person' AND p.archived_at IS NULL
        ORDER BY s.last_contact_at DESC NULLS LAST LIMIT :limit"""), {"limit": limit}).mappings().all()
    months = defaultdict(list)
    for row in db.execute(text("""SELECT profile_id, month, sent, received FROM profile_message_months
            WHERE profile_id = ANY(:ids) AND month >= (date_trunc('month', CAST(:today AS date)) - interval '11 months')"""),
            {"ids": [row["id"] for row in rows], "today": today}).mappings():
        months[row["profile_id"]].append(row)
    return [_person(row, months=month_series(months[row["id"]], RECENT_MONTHS, today)) for row in rows]


def dormant_contacts(db, days, limit=DORMANT_PEOPLE):
    rows = db.execute(text(f"""WITH calls AS (
          SELECT l.profile_id, count(*) AS calls FROM source_contact_events e
          JOIN source_account_links l ON l.account_id = e.account_id WHERE e.event_kind = 'call' GROUP BY l.profile_id)
        {PERSON}, COALESCE(calls.calls, 0) AS calls
        FROM profile_contact_stats s
        JOIN profiles p ON p.id = s.profile_id AND p.profile_type = 'person' AND p.archived_at IS NULL
        LEFT JOIN calls ON calls.profile_id = s.profile_id
        WHERE s.last_contact_at < now() - make_interval(days => :days)
        ORDER BY s.direct_message_count + :weight * COALESCE(calls.calls, 0) DESC, s.last_contact_at DESC
        LIMIT :limit"""), {"days": days, "weight": CALL_WEIGHT, "limit": limit}).mappings()
    return [_person(row, calls=int(row["calls"]) or None) for row in rows]


def significant_dates(db, today, ahead=DATES_AHEAD, behind=DATES_BEHIND):
    """Yearly dates (birthdays, anniversaries, others) coming up soon or just past."""
    rows = db.execute(text("""SELECT p.id, p.display_name, profile_avatar_media(p.id, p.avatar_media_id) AS avatar_media_id,
          d.kind, d.label, d.year, d.month, d.day, d.date_precision
        FROM profile_dates d JOIN profiles p ON p.id = d.profile_id AND p.archived_at IS NULL
        WHERE d.month IS NOT NULL AND d.day IS NOT NULL""")).mappings()
    upcoming, recent = [], []
    for row in rows:
        entry = {"profile_id": str(row["id"]), "name": row["display_name"], "kind": row["kind"], "label": row["label"],
                 "avatar_media_id": str(row["avatar_media_id"]) if row["avatar_media_id"] else None}
        date = next_occurrence(row["month"], row["day"], today)
        if date <= today + timedelta(days=ahead):
            years = date.year - row["year"] if row["year"] and row["date_precision"] == "exact" and row["kind"] != "other" else None
            upcoming.append(compact({**entry, "date": date, "days_until": (date - today).days,
                                     "years": years if years and years > 0 else None}))
        past = next_occurrence(row["month"], row["day"], today - timedelta(days=behind))
        if past < today:
            recent.append(compact({**entry, "date": past, "days_ago": (today - past).days}))
    return (sorted(upcoming, key=lambda item: (item.get("days_until", 0), item["name"])),
            sorted(recent, key=lambda item: item["days_ago"]))


def recent_posters(db, days=POSTERS_DAYS, limit=POSTERS):
    rows = db.execute(text("""WITH recent AS (
          SELECT id, profile_id, provider, occurred_at, 'post' AS kind FROM social_posts
          WHERE occurred_at >= now() - make_interval(days => :days) AND profile_id IS NOT NULL
          UNION ALL
          SELECT id, profile_id, provider, occurred_at, 'story' FROM social_stories
          WHERE occurred_at >= now() - make_interval(days => :days) AND profile_id IS NOT NULL)
        SELECT p.id, p.display_name, profile_avatar_media(p.id, p.avatar_media_id) AS avatar_media_id,
          count(*) FILTER (WHERE r.kind = 'post') AS posts, count(*) FILTER (WHERE r.kind = 'story') AS stories,
          max(r.occurred_at) AS latest_at, array_agg(DISTINCT r.provider) AS providers,
          (array_agg(r.id ORDER BY r.occurred_at DESC))[1:4] AS latest_ids
        FROM recent r JOIN profiles p ON p.id = r.profile_id AND p.archived_at IS NULL
        GROUP BY p.id ORDER BY latest_at DESC LIMIT :limit"""), {"days": days, "limit": limit}).mappings().all()
    media = linked_media(db, [post_id for row in rows for post_id in row["latest_ids"]])
    posters = []
    for row in rows:
        # A photo reposted to several posts or stories is shown once.
        thumbnails = list(dict.fromkeys(str(item["id"]) for post_id in row["latest_ids"] for item in media.get(str(post_id), [])
                                        if str(item["media_type"]).startswith("image/")))[:3]
        posters.append(compact({"profile_id": str(row["id"]), "name": row["display_name"],
                                "avatar_media_id": str(row["avatar_media_id"]) if row["avatar_media_id"] else None,
                                "posts": int(row["posts"]), "stories": int(row["stories"]), "latest_at": row["latest_at"],
                                "providers": sorted(row["providers"]), "thumbnail_media_ids": thumbnails}))
    return posters


def organizations(db, table, entity, column):
    return [{"id": str(row.id), "name": row.name, "people": int(row.people)} for row in db.execute(text(f"""
        SELECT e.id, e.name, count(DISTINCT c.profile_id) AS people
        FROM profile_structured_current c JOIN {table} r ON r.id = c.current_revision_id
        JOIN {entity} e ON e.id = r.{column}
        JOIN profiles p ON p.id = c.profile_id AND p.archived_at IS NULL AND p.profile_type = 'person'
        GROUP BY e.id, e.name ORDER BY people DESC, e.name LIMIT :limit"""), {"limit": TOP_ORGANIZATIONS})]


def build_insights(db, dormant_days: int) -> dict:
    today = datetime.now(ZoneInfo(get_settings().crm_timezone)).date()
    upcoming, recent = significant_dates(db, today)
    return compact({
        "dormant_days": dormant_days,
        "dormant_contacts": dormant_contacts(db, dormant_days),
        "upcoming_dates": upcoming,
        "recent_dates": recent,
        "recent_posters": recent_posters(db),
        "recent_contacts": recent_contacts(db, today),
        "top_companies": organizations(db, "profile_employment_revisions", "companies", "company_id"),
        "top_schools": organizations(db, "profile_education_revisions", "schools", "school_id"),
    })


@router.get("/api/v1/insights")
@cached_endpoint("insights", DOMAINS, ttl=900, stale_on_change=True)
def insights(dormant_days: int = 90, db: Session = Depends(get_db), _user=Depends(current_user)):
    if dormant_days not in DORMANT_THRESHOLDS:
        raise HTTPException(status_code=422, detail=f"dormant_days must be one of {DORMANT_THRESHOLDS}")
    return build_insights(db, dormant_days)
