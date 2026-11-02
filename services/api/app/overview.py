"""Profile page first paint in one cached request.

Every block is omitted when it has no data (no zeros, no empty lists), so the
page renders exactly the sections that exist. Tab pages keep their detailed
endpoints.
"""

from datetime import datetime
from uuid import UUID
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import text
from sqlalchemy.orm import Session

from .aggregate_cache import cached_endpoint
from .auth import current_user
from .config import get_settings
from .data_versions import DOMAINS
from .db import get_db
from .post_text import excerpt
from .profile_facts import (
    city,
    compact,
    current_job,
    experiences,
    field_texts,
    headline,
    month_series,
)
from .remote_media import linked_media
from .repositories import get_profile
from .significant_dates import birthday_of, list_dates

router = APIRouter()

MESSAGE_MONTHS = 24
TOP_RELATIONSHIPS = 8
TOP_GROUPS = 8
TOP_PLACES = 5
RECENT_POSTS = 9
POST_EXCERPT = 120


def _header(db, profile, pid):
    fields = field_texts(db, profile_id=pid).get(str(pid), {})
    facts = experiences(db, pid).get(str(pid), [])
    job = current_job(facts) or {}
    avatars = [{"provider": row.role.removesuffix("_avatar"), "media_id": str(row.media_id)}
               for row in db.execute(text("""SELECT DISTINCT ON (l.role) l.role, l.media_id
                   FROM media_links l JOIN media_assets m ON m.id = l.media_id
                   WHERE l.entity_type = 'profile' AND l.entity_id = :id
                     AND l.role IN ('wechat_avatar','linkedin_avatar','instagram_avatar')
                   ORDER BY l.role, m.created_at DESC"""), {"id": pid})]
    sources = sorted(set(db.execute(text("""SELECT a.provider FROM source_account_links l
            JOIN source_accounts a ON a.id = l.account_id WHERE l.profile_id = :id
          UNION SELECT provider FROM identities WHERE profile_id = :id AND status NOT IN ('invalid','merged')
            AND provider IN ('wechat','instagram','linkedin','monica')"""), {"id": pid}).scalars()))
    avatar = db.execute(text("SELECT profile_avatar_media(:id, :chosen)"),
                        {"id": pid, "chosen": profile["avatar_media_id"]}).scalar_one()
    birthday = birthday_of(list_dates(db, pid)) if profile["profile_type"] == "person" else None
    header = compact({
        "id": str(pid), "type": profile["profile_type"], "name": profile["display_name"],
        "summary": profile["summary"], "note": fields.get("wechat.remark"),
        "company": job.get("company"), "title": job.get("title"),
        "headline": headline(fields, facts) if profile["profile_type"] == "person" else None,
        "city": city(fields), "birthday": birthday,
        "avatar_media_id": str(avatar) if avatar else None, "avatars": avatars, "sources": sources,
    })
    return header, facts


def _messages(db, pid, today):
    stats = db.execute(text("SELECT * FROM profile_contact_stats WHERE profile_id = :id"), {"id": pid}).mappings().one_or_none()
    if not stats:
        return None
    rows = db.execute(text("""SELECT month, sent, received FROM profile_message_months
        WHERE profile_id = :id AND month >= (date_trunc('month', CAST(:today AS date)) - interval '23 months')"""),
        {"id": pid, "today": today}).mappings().all()
    return {"months": month_series(rows, MESSAGE_MONTHS, today), "total": int(stats["direct_message_count"]),
            "sent": int(stats["sent_count"]), "received": int(stats["received_count"]),
            "first_contact_at": stats["first_contact_at"], "last_contact_at": stats["last_contact_at"],
            "last_group_at": stats["last_group_at"], "group_total": int(stats["group_message_count"])}


def _contact_events(db, pid):
    from .contact_events import summary

    events = summary(db, pid)
    return compact({"calls": events["calls"] if events["calls"]["total"] else None,
                    "money": events["money"] if events["money"]["total"] else None})


def _relationships(db, pid):
    from .query_services import profile_relationships

    distinct = {}
    for row in profile_relationships(profile_id=pid, db=db):
        if str(row["id"]).startswith("membership:"):
            continue
        key = (str(row["related_profile_id"]), row["relationship_type"])
        if key in distinct:
            # The same relationship recorded from both sides is one relationship.
            if distinct[key]["direction"] != row["direction"]:
                distinct[key]["direction"] = "both"
            continue
        distinct[key] = compact({"profile_id": row["related_profile_id"], "name": row["related_display_name"],
                                 "relationship_type": row["relationship_type"], "direction": row["direction"],
                                 "avatar_media_id": row.get("avatar_media_id")})
    return list(distinct.values())[:TOP_RELATIONSHIPS]


def _groups(db, pid):
    return [compact(dict(row)) for row in db.execute(text("""SELECT g.id AS profile_id, g.display_name AS name,
          profile_avatar_media(g.id, g.avatar_media_id) AS avatar_media_id, gm.role,
          c.message_count, c.last_message_at
        FROM group_memberships gm
        JOIN profiles g ON g.id = gm.group_profile_id AND g.profile_type = 'group' AND g.archived_at IS NULL
        LEFT JOIN conversations c ON c.profile_id = g.id AND c.conversation_type = 'group'
        WHERE gm.person_profile_id = :id AND gm.left_at IS NULL AND gm.group_profile_id <> gm.person_profile_id
        ORDER BY c.last_message_at DESC NULLS LAST, c.message_count DESC NULLS LAST, g.display_name
        LIMIT :limit"""), {"id": pid, "limit": TOP_GROUPS}).mappings()]


def _post_stats(db, pid):
    return {row.provider: {"posts": int(row.posts), "last_posted_at": row.last_posted_at}
            for row in db.execute(text("""SELECT provider, count(*) AS posts, max(occurred_at) AS last_posted_at
                FROM social_posts WHERE profile_id = :id GROUP BY provider"""), {"id": pid})}


def _places(db, pid):
    from .routers.locations import profile_locations

    points = profile_locations(profile_id=pid, db=db, _user=None).get("points") or []
    ranked = sorted(points, key=lambda point: int(point.get("count") or 0), reverse=True)[:TOP_PLACES]
    return [compact({"label": p.get("label"), "latitude": p.get("latitude"), "longitude": p.get("longitude"),
                     "count": p.get("count")}) for p in ranked]


def _recent_posts(db, pid):
    rows = db.execute(text("""SELECT p.id, p.provider, p.occurred_at,
          COALESCE(o.payload_json->>'caption', o.payload_json->>'text', p.search_body) AS body
        FROM social_posts p LEFT JOIN content_objects o ON o.id = p.content_object_id
        WHERE p.profile_id = :id ORDER BY p.occurred_at DESC NULLS LAST, p.id DESC LIMIT :limit"""),
        {"id": pid, "limit": RECENT_POSTS}).mappings().all()
    media = linked_media(db, [row["id"] for row in rows])
    posts = []
    for row in rows:
        images = [m for m in media.get(str(row["id"]), []) if str(m["media_type"]).startswith("image/")]
        cover = images[0] if images else None
        posts.append(compact({
            "id": str(row["id"]), "provider": row["provider"], "occurred_at": row["occurred_at"],
            "text": excerpt(row["body"], POST_EXCERPT),
            "thumbnail_media_id": str(cover["id"]) if cover else None,
            "width": cover["width"] if cover else None, "height": cover["height"] if cover else None,
            "media_count": len(media.get(str(row["id"]), [])),
        }))
    return posts


def build_overview(db, profile_id: UUID) -> dict:
    profile = get_profile(db, profile_id)
    if not profile:
        raise HTTPException(status_code=404, detail="profile not found")
    today = datetime.now(ZoneInfo(get_settings().crm_timezone)).date()
    header, facts = _header(db, profile, profile_id)
    person = profile["profile_type"] == "person"
    tags = [{"id": str(row.id), "name": row.name} for row in db.execute(text("""SELECT t.id, t.name
        FROM tag_memberships m JOIN tags t ON t.id = m.tag_id WHERE m.profile_id = :id ORDER BY t.name"""),
        {"id": profile_id})]
    linkedin = [{k: v for k, v in fact.items() if k != "source"} for fact in facts if fact["source"] == "linkedin"]
    return compact({
        "header": header,
        "messages": _messages(db, profile_id, today) if person else None,
        "contact_events": _contact_events(db, profile_id) if person else None,
        "relationships": _relationships(db, profile_id) if person else None,
        "groups": _groups(db, profile_id) if person else None,
        "tags": tags,
        "linkedin_experience": linkedin,
        "post_stats": _post_stats(db, profile_id),
        "places": _places(db, profile_id),
        "recent_posts": _recent_posts(db, profile_id),
    })


@router.get("/api/v1/profiles/{profile_id}/overview")
@cached_endpoint("profile-overview", DOMAINS)
def profile_overview(profile_id: UUID, db: Session = Depends(get_db), _user=Depends(current_user)):
    return build_overview(db, profile_id)
