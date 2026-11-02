"""What the home Mac may read and write. Chat images are never offered."""

import tarfile
from pathlib import Path
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import bindparam, text
from sqlalchemy.orm import Session

from ..db import get_db
from .signing import face_device

router = APIRouter(prefix="/api/v1/faces/device", dependencies=[Depends(face_device)])

IMAGE_TYPES = ("image/jpeg", "image/jpg", "image/png", "image/webp")
AVATAR_ROLES = ("wechat_avatar", "wechat_avatar_big", "linkedin_avatar", "instagram_avatar", "manual_photo")
READERS = 4

# One row per image the model may look at, with the person it is an avatar of when known.
ELIGIBLE = """
  SELECT l.media_id,
         bool_or(l.entity_type <> 'social_post') AS avatar,
         (array_agg(COALESCE(CASE WHEN l.entity_type = 'profile' THEN l.entity_id END, sal.profile_id))
            FILTER (WHERE l.entity_type <> 'social_post'))[1] AS owner_profile_id
  FROM media_links l
  JOIN media_assets a ON a.id = l.media_id AND a.media_type IN :image_types
  LEFT JOIN source_account_links sal ON l.entity_type = 'source_account' AND sal.account_id = l.entity_id
  WHERE (l.entity_type IN ('profile', 'source_account') AND l.role IN :avatar_roles)
     OR (l.entity_type = 'social_post' AND l.role ~ '^media-[0-9]+$')
  GROUP BY l.media_id
"""


PARAMS = {"image_types": list(IMAGE_TYPES), "avatar_roles": list(AVATAR_ROLES)}


def _eligible(sql: str):
    return text(sql).bindparams(bindparam("image_types", expanding=True), bindparam("avatar_roles", expanding=True))


@router.get("/work")
def work(limit: Annotated[int, Query(ge=1, le=500)] = 200, db: Session = Depends(get_db)):
    """Unscanned images, avatars first so each run has people to compare photos with.

    The owner is the avatar's person, or for a post photo whoever posted it. Posters are looked up one page
    at a time: looking them up for every scanned photo at once reads too much cold index on the NAS disk.
    """
    rows = db.execute(_eligible(f"""
      WITH eligible AS ({ELIGIBLE}),
      page AS (
        SELECT a.id AS media_id, e.avatar, e.owner_profile_id, a.media_type, a.byte_length, a.created_at
        FROM eligible e JOIN media_assets a ON a.id = e.media_id
        WHERE NOT EXISTS (SELECT 1 FROM face_scans s WHERE s.media_id = a.id
            AND (s.status <> 'retryable_error' OR s.next_attempt_at > now()))
        ORDER BY e.avatar DESC, a.created_at DESC, a.id
        LIMIT :limit)
      SELECT media_id, avatar,
             COALESCE(owner_profile_id, (
               SELECT p.profile_id FROM media_links l JOIN social_posts p ON p.id = l.entity_id
               WHERE l.media_id = page.media_id AND l.entity_type = 'social_post' AND l.role ~ '^media-[0-9]+$'
                 AND p.profile_id IS NOT NULL
               ORDER BY p.occurred_at LIMIT 1)) AS owner_profile_id,
             media_type, byte_length
      FROM page ORDER BY avatar DESC, created_at DESC, media_id"""), {**PARAMS, "limit": limit}).mappings()
    return {"items": [dict(row) for row in rows]}


class MediaBatch(BaseModel):
    ids: Annotated[list[UUID], Field(min_length=1, max_length=100)]


@router.post("/media")
def media(payload: MediaBatch, db: Session = Depends(get_db)):
    """The original files as a tar, one member per media id; ids that are not offered or not on disk are left out.

    One signed request per batch: each request commits a nonce, which takes seconds on the busy NAS disk.
    """
    rows = db.execute(_eligible("""
      SELECT a.id::text AS id, a.object_path FROM media_assets a
      WHERE a.id IN :ids AND a.media_type IN :image_types AND EXISTS (
        SELECT 1 FROM media_links l WHERE l.media_id = a.id
          AND ((l.entity_type IN ('profile', 'source_account') AND l.role IN :avatar_roles)
               OR (l.entity_type = 'social_post' AND l.role ~ '^media-[0-9]+$')))""").bindparams(
        bindparam("ids", expanding=True)), {**PARAMS, "ids": payload.ids}).all()
    db.close()
    found = [(media_id, Path(path)) for media_id, path in rows if Path(path).is_file()]
    return StreamingResponse(_tar(found), media_type="application/x-tar")


def _tar(files: list[tuple[str, Path]]):
    """Stream regular tar members with at most one 256 KiB chunk resident.

    No producer runs ahead of a slow/disconnected client. The open descriptor
    closes when Starlette closes the generator.
    """
    for media_id, path in files:
        with path.open("rb") as source:
            size = __import__("os").fstat(source.fileno()).st_size
            member = tarfile.TarInfo(media_id)
            member.size = size
            yield member.tobuf()
            remaining = size
            while remaining:
                chunk = source.read(min(remaining, 256 * 1024))
                if not chunk:
                    raise OSError("Media file truncated during transfer")
                remaining -= len(chunk)
                yield chunk
            if size % 512:
                yield b"\0" * (512 - size % 512)
    yield b"\0" * 1024


@router.get("/anchors")
def anchors(db: Session = Depends(get_db)):
    """Faces known to belong to someone, and suggestions people turned down.

    An avatar with exactly one clear face counts as its owner's, unless someone has said otherwise.
    """
    known = db.execute(_eligible(f"""
      WITH eligible AS ({ELIGIBLE}),
      avatar_faces AS (
        SELECT f.id AS face_id, e.owner_profile_id AS profile_id, 'avatar' AS kind
        FROM faces f JOIN eligible e ON e.media_id = f.media_id AND e.avatar AND e.owner_profile_id IS NOT NULL
        WHERE f.status = 'pending' AND f.quality >= 0.6 AND f.rejected_profile_id IS DISTINCT FROM e.owner_profile_id
          AND (SELECT count(*) FROM faces o WHERE o.media_id = f.media_id) = 1)
      SELECT face_id, profile_id, kind FROM avatar_faces
      UNION ALL
      SELECT id, profile_id, 'confirmed' FROM faces WHERE status = 'confirmed' AND profile_id IS NOT NULL"""),
        PARAMS).mappings()
    rejected = db.execute(text(
        "SELECT id AS face_id, rejected_profile_id AS profile_id FROM faces WHERE rejected_profile_id IS NOT NULL"
    )).mappings()
    ignored = db.execute(text("SELECT id FROM faces WHERE status = 'ignored'")).scalars()
    return {"anchors": [dict(r) for r in known], "rejected": [dict(r) for r in rejected],
            "ignored": list(ignored)}


class Face(BaseModel):
    id: UUID
    box: Annotated[list[float], Field(min_length=4, max_length=4)]
    quality: float


class Scan(BaseModel):
    media_id: UUID
    faces: list[Face]
    model: str | None = None
    status: Literal["success", "retryable_error", "permanent_error"] = "success"
    error: str | None = None


class Suggestion(BaseModel):
    face_id: UUID
    cluster_key: str | None
    profile_id: UUID | None
    score: float | None


class FacesBatch(BaseModel):
    model: str | None = None
    scans: Annotated[list[Scan], Field(max_length=1000)] = []
    suggestions: Annotated[list[Suggestion], Field(max_length=50000)] = []

    @model_validator(mode='after')
    def require_scan_models(self):
        if any(not (scan.model or self.model) for scan in self.scans):
            raise ValueError('Each scan requires a model, per scan or at batch level')
        return self


@router.post("/batch")
def batch(payload: FacesBatch, db: Session = Depends(get_db)):
    """Store what the model found. Decided faces keep their decision; only pending ones take a new suggestion."""
    faces = [(f.id, scan.media_id, f.box, f.quality) for scan in payload.scans if scan.status == "success" for f in scan.faces]
    if faces:
        db.execute(text("""
          INSERT INTO faces(id, media_id, box, quality, in_post)
          SELECT id, media_id, ARRAY[b0, b1, b2, b3]::real[], quality,
                 EXISTS (SELECT 1 FROM media_links l WHERE l.media_id = t.media_id AND l.entity_type = 'social_post')
          FROM unnest(CAST(:ids AS uuid[]), CAST(:media AS uuid[]), CAST(:b0 AS real[]), CAST(:b1 AS real[]),
                      CAST(:b2 AS real[]), CAST(:b3 AS real[]), CAST(:quality AS real[]))
               AS t(id, media_id, b0, b1, b2, b3, quality)
          ON CONFLICT (id) DO NOTHING"""), {
            "ids": [f[0] for f in faces], "media": [f[1] for f in faces],
            **{f"b{i}": [f[2][i] for f in faces] for i in range(4)}, "quality": [f[3] for f in faces]})
    for scan in payload.scans:
        model = scan.model or payload.model
        status, error = scan.status, scan.error
        # Old clients encode transient failures in the model label.
        if model.rsplit(":", 1)[-1] in {"timeout", "missing", "unreadable"}:
            status, error = "retryable_error", model.rsplit(":", 1)[-1]
        db.execute(text("""INSERT INTO face_scans(media_id, model, faces, status, error, next_attempt_at)
            VALUES (:media, :model, :n, :status, :error,
                    CASE WHEN :status='retryable_error' THEN now()+interval '10 minutes' END)
            ON CONFLICT(media_id) DO UPDATE SET model=excluded.model, faces=excluded.faces,
              status=excluded.status, error=excluded.error, next_attempt_at=excluded.next_attempt_at, scanned_at=now()
            WHERE face_scans.status <> 'success' OR excluded.status='success'"""),
                   {"media": scan.media_id, "model": model, "n": len(scan.faces) if status == "success" else 0,
                    "status": status, "error": error})
    updated = 0
    if payload.suggestions:
        s = payload.suggestions
        updated = db.execute(text("""
          UPDATE faces f SET cluster_key = t.cluster_key,
                 suggested_profile_id = CASE WHEN EXISTS (SELECT 1 FROM profiles p WHERE p.id = t.profile_id)
                                              AND t.profile_id IS DISTINCT FROM f.rejected_profile_id
                                             THEN t.profile_id END,
                 suggestion_score = t.score
          FROM unnest(CAST(:ids AS uuid[]), CAST(:clusters AS text[]), CAST(:profiles AS uuid[]),
                      CAST(:scores AS real[])) AS t(id, cluster_key, profile_id, score)
          WHERE f.id = t.id AND f.status = 'pending'
            AND (f.cluster_key, f.suggested_profile_id, f.suggestion_score)
                IS DISTINCT FROM (t.cluster_key, t.profile_id, t.score)"""), {
            "ids": [x.face_id for x in s], "clusters": [x.cluster_key for x in s],
            "profiles": [x.profile_id for x in s], "scores": [x.score for x in s]}).rowcount
    db.commit()
    return {"faces": len(faces), "scans": len(payload.scans), "suggestions": updated}
