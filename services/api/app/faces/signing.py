"""Requests from the face device: the import HMAC for source "faces", plus a revocable faces token."""

from fastapi import Depends, Header, HTTPException, Request
from sqlalchemy import text
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from ..config import get_settings
from ..db import get_db
from ..security import hash_service_token, verify_import_signature

SOURCE = "faces"


def signed_message(method: str, target: str, body: bytes) -> bytes:
    """GETs carry no body, so the method and path with query are signed along with it."""
    return f"{method} {target}\n".encode() + body


def _accept(db, token: str, nonce: str) -> None:
    token_id = db.execute(text("""SELECT id FROM import_service_tokens
        WHERE source = :source AND token_hash = :hash AND revoked_at IS NULL"""),
        {"source": SOURCE, "hash": hash_service_token(token)}).scalar_one_or_none()
    if token_id is None:
        raise HTTPException(401, "invalid faces token")
    # Concurrent downloads would otherwise queue on this row behind each other's slow commits.
    db.execute(text("""UPDATE import_service_tokens SET last_used_at = now()
        WHERE id = :id AND (last_used_at IS NULL OR last_used_at < now() - interval '1 minute')"""), {"id": token_id})
    db.execute(text("DELETE FROM import_request_nonces WHERE expires_at < now()"))
    accepted = db.execute(text("""INSERT INTO import_request_nonces(nonce, expires_at)
        VALUES (:nonce, now() + (:ttl || ' seconds')::interval) ON CONFLICT (nonce) DO NOTHING RETURNING nonce"""),
        {"nonce": nonce, "ttl": get_settings().import_signature_ttl_seconds}).scalar_one_or_none()
    if accepted is None:
        raise HTTPException(409, "replayed request")
    db.commit()


async def face_device(
    request: Request,
    db: Session = Depends(get_db),
    x_import_timestamp: str | None = Header(default=None),
    x_import_nonce: str | None = Header(default=None),
    x_import_signature: str | None = Header(default=None),
    x_import_token: str | None = Header(default=None),
) -> None:
    if not (x_import_timestamp and x_import_nonce and x_import_signature and x_import_token):
        raise HTTPException(401, "signed faces request required")
    target = request.url.path + (f"?{request.url.query}" if request.url.query else "")
    message = signed_message(request.method, target, await request.body())
    if not verify_import_signature(x_import_timestamp, x_import_nonce, x_import_signature, message, SOURCE):
        raise HTTPException(401, "invalid signature")
    await run_in_threadpool(_accept, db, x_import_token, x_import_nonce)
