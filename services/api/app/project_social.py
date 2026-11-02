"""Shared import services; transaction ownership stays with the caller."""
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from .content import get_or_create_content
from .import_common import _content_value, _parse_datetime, _record_entity
from .schemas import ImportBatchRequest


def _project_social_record(db: Session, record, request: ImportBatchRequest, content: Any) -> None:
    entity_type = _record_entity(record, request, content)
    if entity_type not in {"post", "story"}:
        return
    normalized = _content_value(content)
    provider = record.provider or request.source
    content_kind = "post_body" if entity_type == "post" else "story_body"
    content_id, digest = get_or_create_content(db, value=normalized, content_kind=content_kind)
    occurred_at = _parse_datetime(record.occurred_at or normalized.get("occurred_at"))
    if entity_type == "post":
        existing = (
            db.execute(
                text("""
                  SELECT id, current_content_hash FROM social_posts
                  WHERE provider = :provider AND external_id = :external_id
                """),
                {"provider": provider, "external_id": record.external_id},
            )
            .mappings()
            .one_or_none()
        )
        if existing:
            db.execute(
                text("""
                  UPDATE social_posts SET profile_id = COALESCE(:profile_id, profile_id),
                    content_object_id = :content_id, occurred_at = COALESCE(:occurred_at, occurred_at),
                    source_updated_at = COALESCE(:source_updated_at, source_updated_at),
                    last_seen_at = now(), current_content_hash = :digest
                  WHERE id = :id
                """),
                {
                    "profile_id": record.profile_id,
                    "content_id": content_id,
                    "occurred_at": occurred_at,
                    "source_updated_at": record.source_updated_at,
                    "digest": digest,
                    "id": existing["id"],
                },
            )
            post_id = existing["id"]
            if existing["current_content_hash"] != digest:
                db.execute(
                    text("""
                      INSERT INTO social_post_revisions
                        (post_id, content_object_id, content_hash, observed_at, source_record_id)
                      VALUES (:post_id, :content_id, :digest, :observed_at, :source_record_id)

                    """),
                    {
                        "post_id": existing["id"],
                        "content_id": content_id,
                        "digest": digest,
                        "observed_at": request.observed_at,
                        "source_record_id": record.external_id,
                    },
                )
        else:
            post_id = db.execute(
                text("""
              INSERT INTO social_posts
                (profile_id, provider, external_id, content_object_id, occurred_at,
                 source_updated_at, current_content_hash)
              VALUES (:profile_id, :provider, :external_id, :content_id, :occurred_at,
                      :source_updated_at, :digest)
              RETURNING id
            """),
            {
                "profile_id": record.profile_id,
                "provider": provider,
                "external_id": record.external_id,
                "content_id": content_id,
                "occurred_at": occurred_at,
                "source_updated_at": record.source_updated_at,
                "digest": digest,
            },
            ).scalar_one()
            db.execute(
                text("""
              INSERT INTO social_post_revisions
                (post_id, content_object_id, content_hash, observed_at, source_record_id)
              VALUES (:post_id, :content_id, :digest, :observed_at, :source_record_id)

            """),
                {
                    "post_id": post_id,
                    "content_id": content_id,
                    "digest": digest,
                    "observed_at": request.observed_at,
                    "source_record_id": record.external_id,
                },
            )

        from .interactions import project_interactions
        project_interactions(db,[record],provider)
        return

    existing = (
        db.execute(
            text("""
              SELECT id, current_content_hash FROM social_stories
              WHERE provider = :provider AND external_id = :external_id
            """),
            {"provider": provider, "external_id": record.external_id},
        )
        .mappings()
        .one_or_none()
    )
    coverage_status = str(normalized.get("coverage_status", "partial"))
    expires_at = _parse_datetime(normalized.get("expires_at"))
    if existing:
        db.execute(
            text("""
              UPDATE social_stories SET profile_id = COALESCE(:profile_id, profile_id),
                content_object_id = :content_id, occurred_at = COALESCE(:occurred_at, occurred_at),
                expires_at = COALESCE(:expires_at, expires_at), current_content_hash = :digest,
                coverage_status = :coverage_status
              WHERE id = :id
            """),
            {
                "profile_id": record.profile_id,
                "content_id": content_id,
                "occurred_at": occurred_at,
                "expires_at": expires_at,
                "digest": digest,
                "coverage_status": coverage_status,
                "id": existing["id"],
            },
        )
        if existing["current_content_hash"] != digest:
            db.execute(
                text("""
                  INSERT INTO social_story_revisions
                    (story_id, content_object_id, content_hash, observed_at)
                  VALUES (:story_id, :content_id, :digest, :observed_at)

                """),
                {
                    "story_id": existing["id"],
                    "content_id": content_id,
                    "digest": digest,
                    "observed_at": request.observed_at,
                },
            )
        return
    story_id = db.execute(
        text("""
          INSERT INTO social_stories
            (profile_id, provider, external_id, content_object_id, occurred_at,
             expires_at, current_content_hash, coverage_status)
          VALUES (:profile_id, :provider, :external_id, :content_id, :occurred_at,
                  :expires_at, :digest, :coverage_status)
          RETURNING id
        """),
        {
            "profile_id": record.profile_id,
            "provider": provider,
            "external_id": record.external_id,
            "content_id": content_id,
            "occurred_at": occurred_at,
            "expires_at": expires_at,
            "digest": digest,
            "coverage_status": coverage_status,
        },
    ).scalar_one()
    db.execute(
        text("""
          INSERT INTO social_story_revisions
            (story_id, content_object_id, content_hash, observed_at)
          VALUES (:story_id, :content_id, :digest, :observed_at)

        """),
        {
            "story_id": story_id,
            "content_id": content_id,
            "digest": digest,
            "observed_at": request.observed_at,
        },
    )
