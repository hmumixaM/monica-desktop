"""Social timeline feed, summaries and event detail."""
import base64
import json
from datetime import datetime
from uuid import UUID

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
)
from sqlalchemy import text
from sqlalchemy.orm import Session

from ..aggregate_cache import cached_endpoint
from ..ai.image_ocr import event_reading
from ..auth import current_user
from ..config import get_settings
from ..db import get_db
from ..event_metadata import for_events as event_metadata
from ..manual_media import attachments
from ..post_text import display_text, media_ready
from ..remote_media import linked_media, merge_media
from ..repositories import (
    get_profile,
)

router = APIRouter()
settings = get_settings()


@router.get("/api/v1/timeline/summary")
@cached_endpoint("timeline-summary", ("posts", "profiles", "links"))
def timeline_summary(profile_id: UUID, db: Session = Depends(get_db), _user=Depends(current_user)):
    if not get_profile(db, profile_id):
        raise HTTPException(status_code=404, detail="profile not found")
    providers: dict[str, dict[str, object]] = {}
    post_rows = list(
        db.execute(
            text("""
              SELECT provider, count(*) AS posts, max(occurred_at) AS latest,
                     COALESCE(sum(COALESCE((co.payload_json->>'like_count')::int, 0)), 0) AS likes,
                     COALESCE(sum(COALESCE((co.payload_json->>'comment_count')::int, 0)), 0) AS comments,
                     COALESCE(sum(jsonb_array_length(COALESCE(co.payload_json->'media', '[]'::jsonb))), 0) AS media
              FROM social_posts p
              LEFT JOIN content_objects co ON co.id = p.content_object_id
              WHERE p.profile_id = :profile_id
              GROUP BY provider
            """),
            {"profile_id": profile_id},
        ).mappings()
    )
    for row in post_rows:
        providers[str(row["provider"])] = {
            "posts": int(row["posts"]),
            "stories": 0,
            "events": int(row["posts"]),
            "media": int(row["media"] or 0),
            "likes": int(row["likes"] or 0),
            "comments": int(row["comments"] or 0),
            "latest": row["latest"],
        }
    story_rows = list(
        db.execute(
            text("""
              SELECT provider, count(*) AS stories, max(occurred_at) AS latest
              FROM social_stories WHERE profile_id = :profile_id GROUP BY provider
            """),
            {"profile_id": profile_id},
        ).mappings()
    )
    for row in story_rows:
        provider = str(row["provider"])
        summary = providers.setdefault(provider, {"posts": 0, "stories": 0, "events": 0, "media": 0, "likes": 0, "comments": 0, "latest": None})
        summary["stories"] = int(row["stories"])
        summary["events"] = int(summary["events"]) + int(row["stories"])
        if not summary.get("latest") or (row["latest"] and row["latest"] > summary["latest"]):
            summary["latest"] = row["latest"]
    manual_count = db.execute(
        text("""
          SELECT count(*) FROM activities a
          JOIN activity_participants ap ON ap.activity_id = a.id
          WHERE ap.profile_id = :profile_id
        """),
        {"profile_id": profile_id},
    ).scalar_one()
    if manual_count:
        latest_manual = db.execute(
            text("""
              SELECT max(a.occurred_at) FROM activities a
              JOIN activity_participants ap ON ap.activity_id = a.id
              WHERE ap.profile_id = :profile_id
            """),
            {"profile_id": profile_id},
        ).scalar_one()
        providers["manual"] = {"posts": 0, "stories": 0, "events": int(manual_count), "media": 0, "likes": 0, "comments": 0, "latest": latest_manual}
    total = sum(int(item["events"]) for item in providers.values())
    return {"profile_id": str(profile_id), "providers": providers, "total_events": total}


@router.get("/api/v1/timeline/{event_id}")
@cached_endpoint("timeline-detail", ("posts", "media", "profiles", "links", "post_media"))
def timeline_detail(
    event_id: UUID,
    provider: str | None = None,
    db: Session = Depends(get_db),
    _user=Depends(current_user),
):
    """Load one timeline event on demand for the Profile timeline drawer."""
    provider_clause = " AND p.provider = :provider" if provider else ""
    params: dict[str, object] = {"id": event_id}
    if provider:
        params["provider"] = provider
    post = (
        db.execute(
            text(f"""
          SELECT p.id, p.provider, p.external_id, p.profile_id,
                 p.occurred_at, p.source_updated_at, p.last_seen_at, p.status,
                 p.current_content_hash AS content_hash,
                 profiles.display_name AS profile_display_name,
                 co.payload_json AS content
          FROM social_posts p
          LEFT JOIN profiles ON profiles.id = p.profile_id
          LEFT JOIN content_objects co ON co.id = p.content_object_id
          WHERE p.id = :id{provider_clause}
        """),
            params,
        )
        .mappings()
        .one_or_none()
    )
    if post:
        revisions = list(
            db.execute(
                text("""
                  SELECT content_hash, observed_at, source_record_id
                  FROM social_post_revisions
                  WHERE post_id = :id ORDER BY observed_at DESC, id DESC
                """),
                {"id": event_id},
            ).mappings()
        )
        media = list(
            db.execute(
                text("""
                  SELECT m.id, m.media_type, m.byte_length, m.width, m.height,
                         m.duration_ms, m.capture_at, COALESCE(r.source_url, m.source_record_id) AS source_url
                  FROM media_links l JOIN media_assets m ON m.id = l.media_id
                  LEFT JOIN remote_media_sources r ON r.media_id = m.id
                  WHERE l.entity_type IN ('social_post', 'post') AND l.entity_id = :id
                  ORDER BY l.role, m.id
                """),
                {"id": event_id},
            ).mappings()
        )
        interactions = list(
            db.execute(
                text("""
                  SELECT i.id, i.interaction_type, i.external_id,
                         i.author_profile_id, i.author_external_id, i.author_name,
                         i.occurred_at, i.metadata, co.payload_json AS content,
                         COALESCE((SELECT jsonb_agg(image ORDER BY image->>'role') FROM (
                           SELECT jsonb_build_object('id',m.id,'media_type',m.media_type,
                             'width',m.width,'height',m.height,'role',l.role,
                             'source_url',(SELECT min(r.source_url) FROM remote_media_sources r WHERE r.media_id=m.id)) AS image
                           FROM media_links l JOIN media_assets m ON m.id=l.media_id
                           WHERE l.entity_type='social_interaction' AND l.entity_id=i.id
                         ) images),'[]'::jsonb) AS local_media
                  FROM social_interactions i
                  LEFT JOIN content_objects co ON co.id = i.content_object_id
                  WHERE i.post_id = :id AND NOT i.is_deleted
                  ORDER BY i.occurred_at ASC NULLS LAST, i.id
                """),
                {"id": event_id},
            ).mappings()
        )
        interactions = [{**dict(item), "media": merge_media(
            (item["metadata"] or {}).get("media"), item["local_media"])} for item in interactions]
        reading = db.execute(text("""SELECT summary, items, model, generated_at FROM social_post_media_readings
                                     WHERE post_id = :id"""), {"id": event_id}).mappings().one_or_none()
        return {
            "event_type": "post",
            **dict(post),
            "event_metadata": event_metadata(db, [event_id]).get(str(event_id)),
            "revisions": revisions,
            "media": merge_media((post["content"] or {}).get("media"), media),
            "interactions": interactions,
            "media_reading": event_reading(db, event_id, "social_post", reading),
        }

    story_params = dict(params)
    story_provider_clause = " AND s.provider = :provider" if provider else ""
    story = (
        db.execute(
            text(f"""
          SELECT s.id, s.provider, s.external_id, s.profile_id,
                 s.occurred_at, s.expires_at, s.coverage_status,
                 s.current_content_hash AS content_hash,
                 profiles.display_name AS profile_display_name,
                 co.payload_json AS content
          FROM social_stories s
          LEFT JOIN profiles ON profiles.id = s.profile_id
          LEFT JOIN content_objects co ON co.id = s.content_object_id
          WHERE s.id = :id{story_provider_clause}
        """),
            story_params,
        )
        .mappings()
        .one_or_none()
    )
    if story:
        revisions = list(
            db.execute(
                text("""
                  SELECT content_hash, observed_at
                  FROM social_story_revisions
                  WHERE story_id = :id ORDER BY observed_at DESC, id DESC
                """),
                {"id": event_id},
            ).mappings()
        )
        media = list(
            db.execute(
                text("""
                  SELECT m.id, m.media_type, m.byte_length, m.width, m.height,
                         m.duration_ms, m.capture_at, COALESCE(r.source_url, m.source_record_id) AS source_url
                  FROM media_links l JOIN media_assets m ON m.id = l.media_id
                  LEFT JOIN remote_media_sources r ON r.media_id = m.id
                  WHERE l.entity_type = 'social_story' AND l.entity_id = :id
                  ORDER BY l.role, m.id
                """),
                {"id": event_id},
            ).mappings()
        )
        reading = db.execute(text("""SELECT summary, items, model, generated_at FROM social_story_media_readings
                                     WHERE story_id = :id"""), {"id": event_id}).mappings().one_or_none()
        return {"event_type": "story", "event_metadata": event_metadata(db, [event_id]).get(str(event_id)), **dict(story),
                "revisions": revisions, "media": merge_media((story["content"] or {}).get("media"), media),
                "media_reading": event_reading(db, event_id, "social_story", reading)}

    activity = (
        db.execute(
            text("""
          SELECT a.id, 'manual' AS provider, a.id::text AS external_id,
                 a.occurred_at, a.title, a.body AS content, a.activity_type,
                 a.source_type, a.source_record_id, a.created_at
          FROM activities a WHERE a.id = :id
        """),
            {"id": event_id},
        )
        .mappings()
        .one_or_none()
    )
    if activity and (not provider or provider == "manual"):
        participants = list(
            db.execute(
                text("""
                  SELECT p.id, p.display_name, p.profile_type
                  FROM activity_participants ap JOIN profiles p ON p.id = ap.profile_id
                  WHERE ap.activity_id = :id ORDER BY p.display_name
                """),
                {"id": event_id},
            ).mappings()
        )
        return {
            "event_type": "activity",
            **dict(activity),
            "participants": participants,
            "media": attachments(db, "activity", [event_id], visual_only=True).get(str(event_id), []),
        }
    raise HTTPException(status_code=404, detail="timeline event not found")


def first_page(arguments):
    return not any(arguments.get(name) for name in ("before", "after", "cursor"))


@router.get("/api/v1/timeline")
@cached_endpoint("timeline-first-page", ("posts", "media", "profiles", "links"), when=first_page)
def timeline(
    profile_id: UUID | None = None,
    provider: str | None = None,
    before: datetime | None = None,
    after: datetime | None = None,
    limit: int = 50,
    cursor: str | None = None,
    media: str | None = None,
    db: Session = Depends(get_db),
    _user=Depends(current_user),
):
    if media not in (None, "ready"):
        raise HTTPException(status_code=422, detail="media filter must be 'ready'")
    post_clauses = ["1 = 1"]
    story_clauses = ["1 = 1"]
    activity_clauses = ["activities.activity_type NOT IN ('wechat_direct_summary','wechat_group_summary')"]
    params: dict[str, object] = {
        "limit": min(max(limit, 1), 100),
        "profile_id": profile_id,
    }
    if cursor:
        try:
            stamp, event_id = json.loads(base64.urlsafe_b64decode(cursor.encode()))
            params['cursor_id'] = UUID(event_id)
            params['cursor_time'] = datetime.fromisoformat(stamp) if stamp else None
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            raise HTTPException(status_code=422, detail="invalid cursor") from exc
        for table, clauses in [('social_posts',post_clauses),('social_stories',story_clauses),('activities',activity_clauses)]:
            if stamp:
                clauses.append(f"({table}.occurred_at < :cursor_time OR {table}.occurred_at IS NULL OR ({table}.occurred_at=:cursor_time AND {table}.id<:cursor_id))")
            else:
                clauses.append(f"({table}.occurred_at IS NULL AND {table}.id<:cursor_id)")
    if after:
        for table, clauses in [('social_posts',post_clauses),('social_stories',story_clauses),('activities',activity_clauses)]:
            clauses.append(f"{table}.occurred_at >= :after")
        params['after'] = after
    if before:
        post_clauses.append("social_posts.occurred_at < :before")
        story_clauses.append("social_stories.occurred_at < :before")
        activity_clauses.append("activities.occurred_at < :before")
        params["before"] = before
    if profile_id:
        post_clauses.append("social_posts.profile_id = :profile_id")
        story_clauses.append("social_stories.profile_id = :profile_id")
        activity_clauses.append(
            "EXISTS (SELECT 1 FROM activity_participants ap WHERE ap.activity_id = activities.id AND ap.profile_id = :profile_id)"
        )
    if provider:
        post_clauses.append("social_posts.provider = :provider")
        story_clauses.append("social_stories.provider = :provider")
        params["provider"] = provider
        if provider != "manual":
            activity_clauses.append("FALSE")
    if media == "ready":
        # Locally archived bytes are linked media assets; filter in SQL so a
        # page still holds `limit` displayable events.
        post_clauses.append("EXISTS (SELECT 1 FROM media_links l WHERE l.entity_id = social_posts.id AND l.entity_type IN ('social_post','post'))")
        story_clauses.append("EXISTS (SELECT 1 FROM media_links l WHERE l.entity_id = social_stories.id AND l.entity_type = 'social_story')")
        activity_clauses.append("EXISTS (SELECT 1 FROM media_links l WHERE l.entity_id = activities.id AND l.entity_type = 'activity')")
    rows = list(
        db.execute(
            text(f"""
      SELECT social_posts.id, social_posts.provider, social_posts.external_id,
             social_posts.occurred_at, CASE social_posts.provider WHEN 'wechat' THEN '朋友圈' WHEN 'instagram' THEN 'Instagram 动态' ELSE 'LinkedIn 动态' END AS title,
             social_posts.profile_id, (SELECT display_name FROM profiles WHERE id=social_posts.profile_id) AS profile_name,
             (SELECT avatar_media_id FROM profiles WHERE id=social_posts.profile_id) AS avatar_media_id,
             COALESCE(content_objects.payload_json->>'caption',
                      content_objects.payload_json->>'text',
                      content_objects.payload_json->>'title') AS summary,
             social_posts.current_content_hash AS content_hash,
             COALESCE(jsonb_array_length(COALESCE(content_objects.payload_json->'media', '[]'::jsonb)), 0) AS media_count,
             COALESCE(NULLIF(content_objects.payload_json->>'like_count', '')::int, 0) AS like_count,
             COALESCE(NULLIF(content_objects.payload_json->>'comment_count', '')::int, 0) AS comment_count,
             content_objects.payload_json->'location' AS location,
             content_objects.payload_json->'shared_link' AS shared_link,
             COALESCE(content_objects.payload_json->'media', '[]'::jsonb) AS media
      FROM (SELECT * FROM social_posts WHERE {" AND ".join(post_clauses)} ORDER BY occurred_at DESC NULLS LAST,id DESC LIMIT :limit) AS social_posts
      LEFT JOIN content_objects ON content_objects.id = social_posts.content_object_id
      WHERE {" AND ".join(post_clauses)}
      UNION ALL
      SELECT social_stories.id, social_stories.provider, social_stories.external_id,
             social_stories.occurred_at, social_stories.provider || ' story' AS title,
             social_stories.profile_id, (SELECT display_name FROM profiles WHERE id=social_stories.profile_id) AS profile_name,
             (SELECT avatar_media_id FROM profiles WHERE id=social_stories.profile_id) AS avatar_media_id,
             COALESCE(content_objects.payload_json->>'caption',
                      content_objects.payload_json->>'text') AS summary,
             social_stories.current_content_hash AS content_hash,
             COALESCE(jsonb_array_length(COALESCE(content_objects.payload_json->'media', '[]'::jsonb)), 0) AS media_count,
             0 AS like_count,
             0 AS comment_count,
             content_objects.payload_json->'location' AS location,
             content_objects.payload_json->'shared_link' AS shared_link,
             COALESCE(content_objects.payload_json->'media', '[]'::jsonb) AS media
      FROM (SELECT * FROM social_stories WHERE {" AND ".join(story_clauses)} ORDER BY occurred_at DESC NULLS LAST,id DESC LIMIT :limit) AS social_stories
      LEFT JOIN content_objects ON content_objects.id = social_stories.content_object_id
      WHERE {" AND ".join(story_clauses)}
      UNION ALL
      SELECT activities.id, 'manual' AS provider, activities.id::text AS external_id,
             activities.occurred_at, activities.title, NULL::uuid AS profile_id, NULL::text AS profile_name, NULL::uuid AS avatar_media_id, activities.body AS summary,
             ''::text AS content_hash,
             0 AS media_count,
             0 AS like_count,
             0 AS comment_count,
             NULL::jsonb AS location,
             NULL::jsonb AS shared_link,
             '[]'::jsonb AS media
      FROM activities
      WHERE {" AND ".join(activity_clauses)}
      ORDER BY occurred_at DESC NULLS LAST, id DESC LIMIT :limit
    """),
            params,
        ).mappings()
    )

    media_by_event = linked_media(db, [row["id"] for row in rows])
    media_by_event.update(attachments(db, "activity", [row["id"] for row in rows if row["provider"] == "manual"], visual_only=True))
    metadata = event_metadata(db, [row["id"] for row in rows])
    events = []
    for row in rows:
        items = [{**item, "ready": media_ready(item)} for item in merge_media(row["media"], media_by_event.get(str(row["id"]), []))]
        if media == "ready":
            items = [item for item in items if item["ready"]]
        cursor_value = [row["occurred_at"].isoformat() if row["occurred_at"] else None, str(row["id"])]
        events.append({**dict(row), "summary": display_text(row["summary"]), "event_metadata": metadata.get(str(row["id"])),
                       "cursor": base64.urlsafe_b64encode(json.dumps(cursor_value).encode()).decode(), "media": items})
    return events
