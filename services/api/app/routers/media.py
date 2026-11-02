"""Media upload, download and profile photo endpoints."""
import hashlib
import json
import tempfile
from pathlib import Path
from uuid import UUID

from fastapi import (
    APIRouter,
    Body,
    Depends,
    File,
    HTTPException,
    Request,
    Response,
    UploadFile,
)
from fastapi.responses import FileResponse
from sqlalchemy import text
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from ..aggregate_cache import cached_endpoint
from ..auth import current_user
from ..config import get_settings
from ..db import get_db
from ..manual_media import IMAGE_TYPES, VIDEO_TYPES, attachments, inspect_upload, validate_media
from ..media_store import place_file, record_asset
from ..repositories import (
    get_profile,
)
from ..schemas import (
    MediaInitiate,
    MediaSelection,
)

router = APIRouter()
settings = get_settings()


@router.post("/api/v1/imports/media/initiate", status_code=201)
def initiate_media_upload(
    payload: MediaInitiate,
    db: Session = Depends(get_db),
    user=Depends(current_user),
):
    upload_id = db.execute(
        text("""
          INSERT INTO media_uploads(expected_sha256, media_type, expected_byte_length, temp_path, created_by)
          VALUES (:expected_sha256, :media_type, :expected_byte_length, :temp_path, :created_by)
          RETURNING id
        """),
        {
            "expected_sha256": payload.expected_sha256,
            "media_type": payload.media_type,
            "expected_byte_length": payload.expected_byte_length,
            "temp_path": str(settings.media_root / ".staging" / "pending-upload.part"),
            "created_by": user["id"],
        },
    ).scalar_one()
    temp_path = settings.media_root / ".staging" / f"{upload_id}.part"
    db.execute(
        text("UPDATE media_uploads SET temp_path = :path WHERE id = :id"),
        {"path": str(temp_path), "id": upload_id},
    )
    temp_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path.touch()
    db.commit()
    return {"upload_id": str(upload_id), "next_chunk": 0}


@router.get("/api/v1/imports/media/{upload_id}")
def media_upload_status(
    upload_id: UUID, db: Session = Depends(get_db), _user=Depends(current_user)
):
    row = (
        db.execute(
            text("""
              SELECT id, expected_sha256, media_type, expected_byte_length,
                     next_chunk, received_bytes, status, created_at, completed_at
              FROM media_uploads WHERE id = :id
            """),
            {"id": upload_id},
        )
        .mappings()
        .one_or_none()
    )
    if not row:
        raise HTTPException(status_code=404, detail="upload not found")
    return dict(row)


@router.put("/api/v1/imports/media/{upload_id}/chunks/{chunk}")
def upload_media_chunk(
    upload_id: UUID,
    chunk: int,
    payload: bytes = Body(...),
    db: Session = Depends(get_db),
    _user=Depends(current_user),
):
    row = (
        db.execute(
            text("SELECT temp_path, next_chunk, status FROM media_uploads WHERE id = :id"),
            {"id": upload_id},
        )
        .mappings()
        .one_or_none()
    )
    if not row:
        raise HTTPException(status_code=404, detail="upload not found")
    if row["status"] != "uploading":
        raise HTTPException(status_code=409, detail="upload is not active")
    if chunk != row["next_chunk"]:
        raise HTTPException(status_code=409, detail=f"expected chunk {row['next_chunk']}")
    if not payload:
        raise HTTPException(status_code=422, detail="empty upload chunk")
    with Path(row["temp_path"]).open("ab") as stream:
        stream.write(payload)
    totals = (
        db.execute(
            text("""
          UPDATE media_uploads SET next_chunk = next_chunk + 1,
            received_bytes = received_bytes + :byte_length
          WHERE id = :id
          RETURNING next_chunk, received_bytes
        """),
            {"byte_length": len(payload), "id": upload_id},
        )
        .mappings()
        .one()
    )
    db.commit()
    return {"upload_id": str(upload_id), **dict(totals)}


@router.post("/api/v1/imports/media/{upload_id}/complete", status_code=201)
def complete_media_upload(
    upload_id: UUID, db: Session = Depends(get_db), _user=Depends(current_user)
):
    row = (
        db.execute(
            text("SELECT * FROM media_uploads WHERE id = :id AND status = 'uploading'"),
            {"id": upload_id},
        )
        .mappings()
        .one_or_none()
    )
    if not row:
        raise HTTPException(status_code=404, detail="active upload not found")
    temporary_path = Path(row["temp_path"])
    if not temporary_path.is_file():
        raise HTTPException(status_code=404, detail="upload chunks not found")
    digest = hashlib.sha256()
    byte_length = 0
    with temporary_path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
            byte_length += len(chunk)
    sha256 = digest.hexdigest()
    if row["expected_sha256"] and row["expected_sha256"].strip() != sha256:
        raise HTTPException(status_code=422, detail="media sha256 does not match expected value")
    if row["expected_byte_length"] is not None and row["expected_byte_length"] != byte_length:
        raise HTTPException(
            status_code=422, detail="media byte length does not match expected value"
        )
    target = place_file(temporary_path, sha256, settings.media_root)
    media_id = record_asset(db, sha256=sha256, path=target, byte_length=byte_length, media_type=row["media_type"])
    media_row = db.execute(text("""SELECT id, sha256, media_type, byte_length, object_path
        FROM media_assets WHERE id = :id"""), {"id": media_id}).mappings().one()
    db.execute(
        text("UPDATE media_uploads SET status = 'complete', completed_at = now() WHERE id = :id"),
        {"id": upload_id},
    )
    db.commit()
    return dict(media_row)


# Photos keep the `manual_photo` role that face scanning reads; any other upload is a `manual_file`.
PROFILE_ROLES = ['manual_photo', 'manual_file']


@router.get('/api/v1/profiles/{profile_id}/photos')
@cached_endpoint("profile-photos", ("media", "profiles"))
def profile_photos(profile_id: UUID, db: Session=Depends(get_db), _user=Depends(current_user)):
    if not get_profile(db,profile_id):raise HTTPException(status_code=404,detail='profile not found')
    return attachments(db,'profile',[profile_id],PROFILE_ROLES).get(str(profile_id),[])


@router.post('/api/v1/profiles/{profile_id}/photos',status_code=201)
def attach_profile_photos(profile_id: UUID, payload: MediaSelection, db: Session=Depends(get_db), user=Depends(current_user)):
    if not get_profile(db,profile_id):raise HTTPException(status_code=404,detail='profile not found')
    validate_media(db,payload.media_ids)
    db.execute(text("""INSERT INTO media_links(entity_type,entity_id,media_id,role)
        SELECT 'profile',:id,m.id,CASE WHEN m.media_type=ANY(:photos) THEN 'manual_photo' ELSE 'manual_file' END
        FROM media_assets m WHERE m.id=ANY(:ids) ON CONFLICT DO NOTHING"""),
        {'id':profile_id,'ids':list(set(payload.media_ids)),'photos':sorted(IMAGE_TYPES)})
    db.execute(text("INSERT INTO audit_events(user_id,action,entity_type,entity_id,metadata) VALUES(:user,'profile.photos.added','profile',:id,CAST(:data AS jsonb))"),{'user':user['id'],'id':profile_id,'data':json.dumps([str(i) for i in payload.media_ids])})
    db.commit()
    return {'status':'attached'}


@router.delete('/api/v1/profiles/{profile_id}/photos/{media_id}',status_code=204)
def remove_profile_photo(profile_id: UUID, media_id: UUID, db: Session=Depends(get_db), user=Depends(current_user)):
    deleted=db.execute(text("DELETE FROM media_links WHERE entity_type='profile' AND entity_id=:id AND media_id=:media AND role=ANY(:roles) RETURNING media_id"),{'id':profile_id,'media':media_id,'roles':PROFILE_ROLES}).first()
    if not deleted:raise HTTPException(status_code=404,detail='manual photo not found')
    db.execute(text("INSERT INTO audit_events(user_id,action,entity_type,entity_id,metadata) VALUES(:user,'profile.photos.removed','profile',:id,CAST(:data AS jsonb))"),{'user':user['id'],'id':profile_id,'data':json.dumps({'media_id':str(media_id)})})
    db.commit()


@router.post("/api/v1/media", status_code=201)
async def upload_media(
    file: UploadFile = File(...),
    media_type: str | None = None,
    db: Session = Depends(get_db),
    _user=Depends(current_user),
):
    staging = settings.media_root / ".staging"
    staging.mkdir(parents=True, exist_ok=True)
    dimensions = (None, None)
    with tempfile.NamedTemporaryFile(dir=staging, delete=False) as temp:
        temporary_path = Path(temp.name)
        digest = hashlib.sha256()
        byte_length = 0
        try:
            while chunk := await file.read(1024 * 1024):
                byte_length += len(chunk)
                if byte_length > 500 * 1024 * 1024:
                    raise HTTPException(status_code=413, detail="File exceeds 500 MB")
                digest.update(chunk)
                temp.write(chunk)
        except BaseException:
            temporary_path.unlink(missing_ok=True)
            raise
    file_name = Path(file.filename or "").name[:255] or None
    try:
        actual_type, dimensions = await run_in_threadpool(inspect_upload, temporary_path, file_name)
    except BaseException:
        temporary_path.unlink(missing_ok=True)
        raise
    sha256 = digest.hexdigest()
    target = place_file(temporary_path, sha256, settings.media_root)
    media_id = record_asset(db, sha256=sha256, path=target, byte_length=byte_length, media_type=actual_type,
                            source_type="manual", width=dimensions[0], height=dimensions[1])
    if file_name:
        db.execute(text("UPDATE media_assets SET file_name = :name WHERE id = :id AND file_name IS NULL"),
                   {"name": file_name, "id": media_id})
    media_row = db.execute(text("""SELECT id, sha256, media_type, byte_length, object_path, file_name
        FROM media_assets WHERE id = :id"""), {"id": media_id}).mappings().one()
    db.commit()
    return dict(media_row)


@router.get("/api/v1/media/{media_id}")
def download_media(media_id: UUID, request: Request, db: Session = Depends(get_db), _user=Depends(current_user)):
    asset = db.execute(
        text("SELECT sha256, object_path, media_type, file_name FROM media_assets WHERE id = :id"), {"id": media_id}
    ).mappings().one_or_none()
    # Authentication and metadata reads are complete. Slow NAS file access,
    # image decoding and response streaming must not occupy a DB connection;
    # a gallery can otherwise exhaust the pool and stall map/profile queries.
    db.close()
    if not asset:
        raise HTTPException(status_code=404, detail="media not found")
    sha = asset["sha256"].strip()
    # Bytes are content-addressed, so a media id's response never changes.
    etag = f'"{sha[:32]}"'
    headers = {"Cache-Control": "private, max-age=31536000, immutable", "ETag": etag,
               "X-Content-Type-Options": "nosniff"}
    if etag in request.headers.get("if-none-match", ""):
        return Response(status_code=304, headers=headers)
    path, mime = Path(asset["object_path"]), asset["media_type"]
    if not path.is_file():
        raise HTTPException(status_code=404, detail="media not found")
    if asset["file_name"] is None or mime in IMAGE_TYPES | VIDEO_TYPES:
        return FileResponse(path, media_type=mime, headers=headers)
    # An uploaded HTML or SVG document opened inline would run in this origin; documents only download.
    return FileResponse(path, media_type=mime, headers=headers, filename=asset["file_name"],
                        content_disposition_type="inline" if mime == "application/pdf" else "attachment")


@router.get("/api/v1/media/{media_id}/ocr")
def image_ocr_result(media_id: UUID, db: Session = Depends(get_db), _user=Depends(current_user)):
    row = db.execute(text("SELECT status,summary,description,category,text,model,generated_at,error FROM image_ocr_readings WHERE media_id=:id"),
                     {'id': media_id}).mappings().one_or_none()
    if not row:
        raise HTTPException(404, "Image is not in the OCR backfill")
    return dict(row)
