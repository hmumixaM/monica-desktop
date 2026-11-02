"""Session authentication shared by every API router."""

import hashlib

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy import text
from sqlalchemy.orm import Session

from .db import get_db


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def current_user(request: Request, db: Session = Depends(get_db)):
    token = request.cookies.get("crm_session")
    if not token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="login required")
    user = (
        db.execute(
            text("""
            SELECT u.id, u.email FROM sessions s JOIN app_users u ON u.id = s.user_id
            WHERE s.token_hash = :token_hash AND s.revoked_at IS NULL AND s.expires_at > now()
        """),
            {"token_hash": token_hash(token)},
        )
        .mappings()
        .one_or_none()
    )
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid session")
    return user
