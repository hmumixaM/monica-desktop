"""Reviewing the model's suggestions, and where confirmed people appear."""

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import bindparam, text
from sqlalchemy.orm import Session

from ..aggregate_cache import cached_endpoint
from ..auth import current_user
from ..data_versions import FACES
from ..db import get_db

router = APIRouter(prefix="/api/v1/faces", dependencies=[Depends(current_user)])

# Small or blurry faces make poor review cards and poor anchors.
REVIEWABLE = 0.6
SAMPLES = 18

PERSON = "p.id::text AS profile_id, p.display_name AS name, profile_avatar_media(p.id, p.avatar_media_id) AS avatar_media_id"
FACE = """f.id, f.media_id, f.box, f.quality, f.suggestion_score AS score,
  (SELECT l.entity_id FROM media_links l WHERE l.media_id = f.media_id AND l.entity_type = 'social_post' LIMIT 1) AS post_id"""


@router.get("/summary")
@cached_endpoint("faces-summary", (FACES,))
def summary(db: Session = Depends(get_db)):
    row = db.execute(text("""SELECT
        (SELECT count(*) FROM face_scans) AS scanned,
        (SELECT max(scanned_at) FROM face_scans) AS last_scan_at,
        count(*) AS faces,
        count(*) FILTER (WHERE status = 'pending' AND in_post AND quality >= :q
                           AND (suggested_profile_id IS NOT NULL OR cluster_key IS NOT NULL)) AS to_review,
        count(*) FILTER (WHERE status = 'confirmed') AS confirmed,
        count(DISTINCT profile_id) FILTER (WHERE status = 'confirmed') AS people
      FROM faces"""), {"q": REVIEWABLE}).mappings().one()
    return dict(row)


@router.get("/groups")
def groups(offset: Annotated[int, Query(ge=0)] = 0, limit: Annotated[int, Query(ge=1, le=40)] = 12,
           db: Session = Depends(get_db)):
    """Pending faces grouped by the person they look like, then by unnamed cluster, largest first."""
    # Materialize the small page before the lateral samples and avatar lookups: otherwise PostgreSQL
    # can load photos for thousands of groups before sorting and discarding all but the first page.
    rows = db.execute(text(f"""
      WITH grouped AS (
        SELECT suggested_profile_id AS person,
               CASE WHEN suggested_profile_id IS NULL THEN cluster_key END AS cluster,
               count(*) AS size, max(suggestion_score) AS best
        FROM faces WHERE status = 'pending' AND in_post AND quality >= :q
          AND (suggested_profile_id IS NOT NULL OR cluster_key IS NOT NULL)
        GROUP BY 1, 2),
      page AS MATERIALIZED (
        SELECT COALESCE('p:' || person, 'c:' || cluster) AS key, person, cluster, size, best
        FROM grouped
        ORDER BY (person IS NOT NULL) DESC, size DESC, key
        OFFSET :offset LIMIT :limit)
      SELECT g.key, g.size, g.best, {PERSON}, s.faces
      FROM page g
      LEFT JOIN profiles p ON p.id = g.person
      CROSS JOIN LATERAL (
        SELECT json_agg(row_to_json(x)) AS faces FROM (
          SELECT {FACE} FROM faces f
          WHERE f.status = 'pending' AND f.in_post AND f.quality >= :q
            AND (f.suggested_profile_id = g.person OR (g.person IS NULL AND f.suggested_profile_id IS NULL
                                                       AND f.cluster_key = g.cluster))
          ORDER BY f.suggestion_score DESC NULLS LAST, f.quality DESC, f.id LIMIT :samples) x) s
      ORDER BY (g.person IS NOT NULL) DESC, g.size DESC, g.key"""),
        {"q": REVIEWABLE, "samples": SAMPLES, "offset": offset, "limit": limit + 1}).mappings().all()
    items = [{"key": r["key"], "size": r["size"], "best": r["best"], "faces": r["faces"],
              "person": {"profile_id": r["profile_id"], "name": r["name"], "avatar_media_id": r["avatar_media_id"]}
              if r["profile_id"] else None} for r in rows[:limit]]
    return {"items": items, "next_offset": offset + limit if len(rows) > limit else None}


class Decision(BaseModel):
    face_ids: Annotated[list[UUID], Field(min_length=1, max_length=200)]
    action: Literal["confirm", "not", "ignore"]
    profile_id: UUID | None = None


class FaceState(BaseModel):
    id: UUID
    status: Literal["pending", "confirmed", "ignored"]
    profile_id: UUID | None
    suggested_profile_id: UUID | None
    rejected_profile_id: UUID | None
    decided_at: datetime | None


STATE = "id, status, profile_id, suggested_profile_id, rejected_profile_id, decided_at"


@router.post("/decide")
def decide(payload: Decision, db: Session = Depends(get_db)):
    """Returns each face's earlier state so the browser can undo."""
    if payload.action == "confirm" and payload.profile_id is None:
        raise HTTPException(422, "confirm needs a person")
    ids = {"ids": payload.face_ids}
    before = db.execute(text(f"SELECT {STATE} FROM faces WHERE id IN :ids FOR UPDATE")
                        .bindparams(bindparam("ids", expanding=True)), ids).mappings().all()
    change = {
        "confirm": "status = 'confirmed', profile_id = :profile",
        # "Not this person" leaves the face for the next run to place elsewhere.
        "not": "rejected_profile_id = COALESCE(:profile, suggested_profile_id, rejected_profile_id),"
               " suggested_profile_id = NULL, cluster_key = NULL, profile_id = NULL, status = 'pending'",
        "ignore": "status = 'ignored', profile_id = NULL",
    }[payload.action]
    db.execute(text(f"UPDATE faces SET {change}, decided_at = now() WHERE id IN :ids")
               .bindparams(bindparam("ids", expanding=True)), {**ids, "profile": payload.profile_id})
    db.commit()
    return {"previous": [FaceState.model_validate(dict(r)).model_dump(mode="json") for r in before]}


@router.post("/restore")
def restore(states: Annotated[list[FaceState], Field(min_length=1, max_length=200)], db: Session = Depends(get_db)):
    db.execute(text("""UPDATE faces SET status = :status, profile_id = :profile_id,
        suggested_profile_id = :suggested_profile_id, rejected_profile_id = :rejected_profile_id,
        decided_at = :decided_at WHERE id = :id"""), [s.model_dump() for s in states])
    db.commit()
    return {"restored": len(states)}


@router.get("/in-media")
def in_media(ids: Annotated[str, Query(max_length=4000)], db: Session = Depends(get_db)):
    """Named faces in a post's photos: confirmed people, and pending suggestions marked as such."""
    media = [UUID(value) for value in ids.split(",") if value]
    rows = db.execute(text(f"""
      SELECT f.id, f.media_id, f.box, f.status, f.suggestion_score AS score, {PERSON}
      FROM faces f JOIN profiles p ON p.id = CASE WHEN f.status = 'confirmed' THEN f.profile_id
                                                  ELSE f.suggested_profile_id END
      WHERE f.media_id IN :media AND f.status <> 'ignored' AND f.quality >= :q
      ORDER BY f.media_id, f.box[1]""").bindparams(bindparam("media", expanding=True)),
        {"media": media, "q": REVIEWABLE}).mappings()
    return {"faces": [dict(r) for r in rows]}


@router.get("/people/{profile_id}")
def appearances(profile_id: UUID, offset: Annotated[int, Query(ge=0)] = 0,
                limit: Annotated[int, Query(ge=1, le=60)] = 24, db: Session = Depends(get_db)):
    """Photos a person was confirmed in, newest post first, with who else is in them."""
    rows = db.execute(text("""
      SELECT DISTINCT ON (sp.occurred_at, f.media_id) f.id, f.media_id, f.box, sp.id AS post_id, sp.occurred_at,
             sp.provider, author.display_name AS author,
             (SELECT json_agg(json_build_object('profile_id', o.profile_id, 'name', op.display_name))
              FROM faces o JOIN profiles op ON op.id = o.profile_id
              WHERE o.media_id = f.media_id AND o.status = 'confirmed' AND o.profile_id <> :pid) AS others
      FROM faces f
      JOIN media_links l ON l.media_id = f.media_id AND l.entity_type = 'social_post'
      JOIN social_posts sp ON sp.id = l.entity_id
      LEFT JOIN profiles author ON author.id = sp.profile_id
      WHERE f.profile_id = :pid AND f.status = 'confirmed'
      ORDER BY sp.occurred_at DESC, f.media_id
      OFFSET :offset LIMIT :limit"""), {"pid": profile_id, "offset": offset, "limit": limit + 1}).mappings().all()
    total = db.execute(text("""SELECT count(DISTINCT media_id) FROM faces
        WHERE profile_id = :pid AND status = 'confirmed'"""), {"pid": profile_id}).scalar_one()
    return {"total": total, "items": [dict(r) for r in rows[:limit]],
            "next_offset": offset + limit if len(rows) > limit else None}
