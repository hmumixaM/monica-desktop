"""Signed import batches, tokens, cursors, jobs and reconciliation."""
import json
from pathlib import Path
from uuid import UUID

from fastapi import (
    APIRouter,
    Depends,
    Header,
    HTTPException,
    Request,
)
from sqlalchemy import text
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from ..auth import current_user
from ..config import get_settings
from ..db import get_db
from ..imports import list_batches, project_batch
from ..schemas import (
    ImportBatchOut,
    ImportBatchRequest,
    ImportServiceTokenCreate,
)
from ..security import (
    hash_service_token,
    new_token,
    verify_import_signature,
)

router = APIRouter()
settings = get_settings()


@router.post("/api/v1/imports/batches", response_model=ImportBatchOut, status_code=201)
async def import_batch(
    payload: ImportBatchRequest,
    request: Request,
    db: Session = Depends(get_db),
    x_import_timestamp: str | None = Header(default=None),
    x_import_nonce: str | None = Header(default=None),
    x_import_signature: str | None = Header(default=None),
    x_import_token: str | None = Header(default=None),
):
    if not (x_import_timestamp and x_import_nonce and x_import_signature):
        raise HTTPException(status_code=401, detail="import signature required")
    signed_body = await request.body()
    if not verify_import_signature(
        x_import_timestamp, x_import_nonce, x_import_signature, signed_body, payload.source
    ):
        raise HTTPException(status_code=401, detail="invalid import signature")
    # The SQLAlchemy session and projection are synchronous. A slow source
    # transaction must not stop the event loop from serving other requests.
    return await run_in_threadpool(_accept_import_batch, db, payload, x_import_nonce, x_import_token)


def _accept_import_batch(db, payload, x_import_nonce, x_import_token):
    active_token_count = db.execute(
        text(
            "SELECT count(*) FROM import_service_tokens WHERE source = :source AND revoked_at IS NULL"
        ),
        {"source": payload.source},
    ).scalar_one()
    if active_token_count:
        if not x_import_token:
            raise HTTPException(status_code=401, detail="import service token required")
        token_row = db.execute(
            text("""
              SELECT id FROM import_service_tokens
              WHERE source = :source AND token_hash = :token_hash AND revoked_at IS NULL
            """),
            {"source": payload.source, "token_hash": hash_service_token(x_import_token)},
        ).scalar_one_or_none()
        if not token_row:
            raise HTTPException(status_code=401, detail="invalid import service token")
        db.execute(
            text("UPDATE import_service_tokens SET last_used_at = now() WHERE id = :id"),
            {"id": token_row},
        )
    db.execute(text("DELETE FROM import_request_nonces WHERE expires_at < now()"))
    accepted_nonce = db.execute(
        text("""
          INSERT INTO import_request_nonces(nonce, expires_at)
          VALUES (:nonce, now() + (:ttl || ' seconds')::interval)
          ON CONFLICT (nonce) DO NOTHING
          RETURNING nonce
        """),
        {"nonce": x_import_nonce, "ttl": settings.import_signature_ttl_seconds},
    ).scalar_one_or_none()
    if not accepted_nonce:
        raise HTTPException(status_code=409, detail="replayed import request")
    result = project_batch(db, payload)
    db.commit()
    return result


@router.get("/api/v1/imports/batches", response_model=list[ImportBatchOut])
def imports(db: Session = Depends(get_db), _user=Depends(current_user)):
    return list_batches(db)


@router.get("/api/v1/imports/reconciliation")
def import_reconciliation(db: Session = Depends(get_db), _user=Depends(current_user)):
    return {
        'sources':[dict(r) for r in db.execute(text('SELECT * FROM source_reconciliation ORDER BY source,stream')).mappings()],
        'media':[dict(r) for r in db.execute(text('SELECT state,count(*) AS count FROM media_manifest GROUP BY state ORDER BY state')).mappings()],
        'jobs':[dict(r) for r in db.execute(text("SELECT job_type,status,count(*) AS count FROM jobs GROUP BY job_type,status ORDER BY job_type,status")).mappings()],
        'messages':dict(db.execute(text("SELECT count(*) AS processed_tables,COALESCE(sum(source_count),0) AS source_rows,COALESCE(sum(projected_count),0) AS processed_rows FROM message_source_cursors")).mappings().one()),
    }


@router.post("/api/v1/imports/tokens", status_code=201)
def create_import_service_token(
    payload: ImportServiceTokenCreate,
    db: Session = Depends(get_db),
    user=Depends(current_user),
):
    raw_token = new_token()
    row = (
        db.execute(
            text("""
              INSERT INTO import_service_tokens(source, label, token_hash)
              VALUES (:source, :label, :token_hash)
              RETURNING id, source, label, created_at
            """),
            {
                "source": payload.source,
                "label": payload.label,
                "token_hash": hash_service_token(raw_token),
            },
        )
        .mappings()
        .one()
    )
    db.execute(
        text(
            "INSERT INTO audit_events(user_id, action, entity_type, entity_id, metadata) VALUES (:user, 'import.token.created', 'import_service_token', :id, CAST(:metadata AS jsonb))"
        ),
        {
            "user": user["id"],
            "id": row["id"],
            "metadata": json.dumps({"source": payload.source, "label": payload.label}),
        },
    )
    db.commit()
    return {**dict(row), "token": raw_token}


@router.get("/api/v1/imports/tokens")
def list_import_service_tokens(db: Session = Depends(get_db), _user=Depends(current_user)):
    return list(
        db.execute(
            text("""
              SELECT id, source, label, created_at, last_used_at, revoked_at
              FROM import_service_tokens ORDER BY created_at DESC
            """)
        ).mappings()
    )


@router.post("/api/v1/imports/tokens/{token_id}/revoke")
def revoke_import_service_token(
    token_id: UUID,
    db: Session = Depends(get_db),
    user=Depends(current_user),
):
    revoked = db.execute(
        text("""
          UPDATE import_service_tokens SET revoked_at = now()
          WHERE id = :id AND revoked_at IS NULL RETURNING id
        """),
        {"id": token_id},
    ).scalar_one_or_none()
    if not revoked:
        raise HTTPException(status_code=404, detail="import token not found")
    db.execute(
        text(
            "INSERT INTO audit_events(user_id, action, entity_type, entity_id) VALUES (:user, 'import.token.revoked', 'import_service_token', :id)"
        ),
        {"user": user["id"], "id": token_id},
    )
    db.commit()
    return {"status": "revoked"}


@router.get("/api/v1/imports/batches/{batch_id}")
def import_batch_detail(batch_id: str, db: Session = Depends(get_db), _user=Depends(current_user)):
    row = (
        db.execute(
            text("""
              SELECT b.*, co.payload_json AS raw_manifest, co.storage_uri AS raw_manifest_uri
              FROM import_batches b
              LEFT JOIN content_objects co ON co.id = b.raw_manifest_object_id
              WHERE b.batch_id = :batch_id
            """),
            {"batch_id": batch_id},
        )
        .mappings()
        .one_or_none()
    )
    if not row:
        raise HTTPException(status_code=404, detail="batch not found")
    return dict(row)


@router.post("/api/v1/imports/batches/{batch_id}/replay")
def replay_import_batch(batch_id: str, db: Session = Depends(get_db), _user=Depends(current_user)):
    raw_row = (
        db.execute(
            text("""
          SELECT co.payload_json, co.storage_uri
          FROM import_batches b JOIN content_objects co ON co.id = b.raw_manifest_object_id
          WHERE b.batch_id = :batch_id
        """),
            {"batch_id": batch_id},
        )
        .mappings()
        .one_or_none()
    )
    raw = raw_row["payload_json"] if raw_row else None
    if raw is None and raw_row and raw_row["storage_uri"]:
        manifest_path = Path(raw_row["storage_uri"])
        if manifest_path.is_file():
            raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not raw:
        raise HTTPException(status_code=404, detail="replay manifest not found")
    try:
        from ..imports import hydrate_manifest
        request_payload = ImportBatchRequest.model_validate(hydrate_manifest(raw))
    except Exception as exc:
        raise HTTPException(status_code=422, detail="stored manifest is not replayable") from exc
    result = project_batch(db, request_payload)
    db.commit()
    return result


@router.get("/api/v1/imports/cursors")
def import_cursors(db: Session = Depends(get_db), _user=Depends(current_user)):
    return list(
        db.execute(
            text("""
              SELECT source, stream, cursor_value, batch_id, updated_at
              FROM source_cursors ORDER BY source, stream
            """)
        ).mappings()
    )


@router.get("/api/v1/imports/jobs")
def import_jobs(
    status_filter: str | None = None,
    limit: int = 100,
    db: Session = Depends(get_db),
    _user=Depends(current_user),
):
    clauses = ["1 = 1"]
    params: dict[str, object] = {"limit": min(max(limit, 1), 200)}
    if status_filter:
        clauses.append("status = :status")
        params["status"] = status_filter
    return list(
        db.execute(
            text(f"""
              SELECT id, job_type, payload, status, attempts, available_at,
                     locked_at, completed_at, error
              FROM jobs WHERE {" AND ".join(clauses)}
              ORDER BY available_at DESC LIMIT :limit
            """),
            params,
        ).mappings()
    )


@router.get("/api/v1/imports/events")
def import_events(limit: int = 100, db: Session = Depends(get_db), _user=Depends(current_user)):
    return list(
        db.execute(
            text("""
              SELECT id, source, stream, external_id, batch_id, source_cursor,
                     content_hash, observed_at, state, source_updated_at
              FROM source_observations ORDER BY observed_at DESC LIMIT :limit
            """),
            {"limit": min(max(limit, 1), 200)},
        ).mappings()
    )
