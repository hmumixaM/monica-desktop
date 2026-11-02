"""Sessions, login, TOTP and audit endpoints."""
import hashlib
import json
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pyotp
from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Request,
    Response,
)
from sqlalchemy import text
from sqlalchemy.orm import Session

from ..auth import current_user, token_hash
from ..config import get_settings
from ..db import get_db
from ..schemas import (
    BootstrapRequest,
    LoginRequest,
    TotpCode,
)
from ..security import (
    decrypt_secret,
    encrypt_secret,
    hash_password,
    new_csrf_token,
    new_token,
    verify_password,
)

router = APIRouter()
settings = get_settings()
_token_hash = token_hash


def _login_attempt_key(email: str, request: Request) -> str:
    ip = request.client.host if request.client else "unknown"
    return hashlib.sha256(f"{email.strip().lower()}|{ip}".encode()).hexdigest()


def _login_is_blocked(db: Session, attempt_key: str) -> bool:
    row = (
        db.execute(
            text("SELECT blocked_until FROM auth_login_attempts WHERE attempt_key = :key"),
            {"key": attempt_key},
        )
        .mappings()
        .one_or_none()
    )
    return bool(row and row["blocked_until"] and row["blocked_until"] > datetime.now(UTC))


def _record_login_failure(db: Session, request: Request, email: str) -> None:
    now = datetime.now(UTC)
    attempt_key = _login_attempt_key(email, request)
    row = (
        db.execute(
            text("""
              SELECT attempts, window_started_at
              FROM auth_login_attempts WHERE attempt_key = :key
            """),
            {"key": attempt_key},
        )
        .mappings()
        .one_or_none()
    )
    if not row or row["window_started_at"] < now - timedelta(minutes=15):
        attempts = 1
        window_started_at = now
    else:
        attempts = int(row["attempts"]) + 1
        window_started_at = row["window_started_at"]
    blocked_until = now + timedelta(minutes=15) if attempts >= 5 else None
    db.execute(
        text("""
          INSERT INTO auth_login_attempts(attempt_key, attempts, window_started_at, blocked_until)
          VALUES (:key, :attempts, :started, :blocked)
          ON CONFLICT (attempt_key) DO UPDATE SET
            attempts = EXCLUDED.attempts,
            window_started_at = EXCLUDED.window_started_at,
            blocked_until = EXCLUDED.blocked_until
        """),
        {
            "key": attempt_key,
            "attempts": attempts,
            "started": window_started_at,
            "blocked": blocked_until,
        },
    )
    db.execute(
        text("""
          INSERT INTO audit_events(action, metadata)
          VALUES ('auth.login.failed', CAST(:metadata AS jsonb))
        """),
        {
            "metadata": json.dumps(
                {"ip": request.client.host if request.client else None, "attempts": attempts}
            )
        },
    )
    db.commit()


def _clear_login_failures(db: Session, request: Request, email: str) -> None:
    db.execute(
        text("DELETE FROM auth_login_attempts WHERE attempt_key = :key"),
        {"key": _login_attempt_key(email, request)},
    )


@router.post("/api/v1/auth/bootstrap")
def bootstrap(payload: BootstrapRequest, db: Session = Depends(get_db)):
    existing = db.execute(text("SELECT id FROM app_users LIMIT 1")).scalar_one_or_none()
    if existing:
        raise HTTPException(status_code=409, detail="bootstrap already completed")
    user_id = db.execute(
        text("""
            INSERT INTO app_users(email, password_hash) VALUES (:email, :password_hash) RETURNING id
        """),
        {"email": payload.email, "password_hash": hash_password(payload.password)},
    ).scalar_one()
    db.execute(
        text(
            "INSERT INTO audit_events(user_id, action, metadata) VALUES (:user, 'auth.bootstrap', CAST(:metadata AS jsonb))"
        ),
        {"user": user_id, "metadata": json.dumps({"email": payload.email})},
    )
    db.commit()
    return {"id": str(user_id), "email": payload.email}


@router.get("/api/v1/auth/csrf")
def csrf_token(response: Response):
    token = new_csrf_token()
    response.set_cookie(
        "crm_csrf",
        token,
        httponly=False,
        secure=settings.secure_cookies,
        samesite="lax",
        max_age=settings.session_ttl_seconds,
    )
    return {"csrf_token": token}


@router.post("/api/v1/auth/login")
def login(
    payload: LoginRequest,
    response: Response,
    request: Request,
    db: Session = Depends(get_db),
):
    attempt_key = _login_attempt_key(payload.email, request)
    if _login_is_blocked(db, attempt_key):
        raise HTTPException(status_code=429, detail="too many login attempts; try again later")
    user = (
        db.execute(
            text("SELECT id, password_hash, totp_secret FROM app_users WHERE email = :email"),
            {"email": payload.email},
        )
        .mappings()
        .one_or_none()
    )
    if not user or not verify_password(user["password_hash"], payload.password):
        _record_login_failure(db, request, payload.email)
        raise HTTPException(status_code=401, detail="invalid credentials")
    if user["totp_secret"]:
        try:
            totp_secret = decrypt_secret(user["totp_secret"])
        except Exception as exc:
            raise HTTPException(status_code=401, detail="TOTP configuration invalid") from exc
        verified = bool(payload.totp_code and pyotp.TOTP(totp_secret).verify(payload.totp_code, valid_window=1))
        if not verified and payload.recovery_code:
            recovered=db.execute(text("""UPDATE auth_recovery_codes SET consumed_at=now()
                WHERE user_id=:user AND code_hash=:digest AND consumed_at IS NULL RETURNING code_hash"""),
                {'user':user['id'],'digest':hashlib.sha256(payload.recovery_code.strip().encode()).hexdigest()}).scalar_one_or_none()
            verified=bool(recovered)
        if not verified:
            _record_login_failure(db, request, payload.email)
            raise HTTPException(status_code=401, detail="TOTP code required")
    token = new_token()
    db.execute(
        text("""
          INSERT INTO sessions(user_id, token_hash, expires_at, user_agent, ip_address)
          VALUES (:user_id, :token_hash, now() + (:ttl || ' seconds')::interval, :agent, :ip)
        """),
        {
            "user_id": user["id"],
            "token_hash": _token_hash(token),
            "ttl": settings.session_ttl_seconds,
            "agent": request.headers.get("user-agent"),
            "ip": request.client.host if request.client else None,
        },
    )
    db.execute(
        text("UPDATE app_users SET last_login_at = now() WHERE id = :id"), {"id": user["id"]}
    )
    db.execute(
        text(
            "INSERT INTO audit_events(user_id, action, metadata) VALUES (:user, 'auth.login', CAST(:metadata AS jsonb))"
        ),
        {
            "user": user["id"],
            "metadata": json.dumps({"ip": request.client.host if request.client else None}),
        },
    )
    _clear_login_failures(db, request, payload.email)
    db.commit()
    response.set_cookie(
        "crm_session",
        token,
        httponly=True,
        secure=settings.secure_cookies,
        samesite="lax",
        max_age=settings.session_ttl_seconds,
    )
    return {"status": "authenticated"}


@router.post("/api/v1/auth/totp/setup")
def totp_setup(db: Session = Depends(get_db), user=Depends(current_user)):
    enabled=db.execute(text('SELECT totp_secret IS NOT NULL FROM app_users WHERE id=:id'),{'id':user['id']}).scalar_one()
    if enabled:
        raise HTTPException(status_code=409,detail='TOTP is already enabled')
    secret = pyotp.random_base32()
    db.execute(
        text("UPDATE app_users SET pending_totp_secret = :secret WHERE id = :id"),
        {"secret": encrypt_secret(secret), "id": user["id"]},
    )
    db.commit()
    uri = pyotp.TOTP(secret).provisioning_uri(name=user["email"], issuer_name=settings.app_name)
    return {"secret": secret, "otpauth_url": uri}


@router.post("/api/v1/auth/totp/verify")
def totp_verify(payload: TotpCode, db: Session = Depends(get_db), user=Depends(current_user)):
    encrypted = db.execute(
        text("SELECT pending_totp_secret FROM app_users WHERE id = :id"), {"id": user["id"]}
    ).scalar_one_or_none()
    if not encrypted:
        raise HTTPException(status_code=409, detail="TOTP is not configured")
    if not pyotp.TOTP(decrypt_secret(encrypted)).verify(payload.code, valid_window=1):
        raise HTTPException(status_code=422, detail="invalid TOTP code")
    import secrets
    codes=[secrets.token_hex(8) for _ in range(8)]
    db.execute(text('UPDATE app_users SET totp_secret=pending_totp_secret,pending_totp_secret=NULL WHERE id=:id'),{'id':user['id']})
    for code in codes:
        db.execute(text('INSERT INTO auth_recovery_codes(user_id,code_hash) VALUES(:user,:digest)'),
                   {'user':user['id'],'digest':hashlib.sha256(code.encode()).hexdigest()})
    db.commit()
    return {"status": "verified", "recovery_codes":codes}


@router.get("/api/v1/auth/totp/status")
def totp_status(db: Session=Depends(get_db),user=Depends(current_user)):
    return {'enabled':db.execute(text('SELECT totp_secret IS NOT NULL FROM app_users WHERE id=:id'),{'id':user['id']}).scalar_one()}


@router.post("/api/v1/auth/logout")
def logout(response: Response, request: Request, db: Session = Depends(get_db)):
    token = request.cookies.get("crm_session")
    if token:
        user_id = db.execute(
            text("SELECT user_id FROM sessions WHERE token_hash = :hash"),
            {"hash": _token_hash(token)},
        ).scalar_one_or_none()
        db.execute(
            text("UPDATE sessions SET revoked_at = now() WHERE token_hash = :hash"),
            {"hash": _token_hash(token)},
        )
        if user_id:
            db.execute(
                text("INSERT INTO audit_events(user_id, action) VALUES (:user, 'auth.logout')"),
                {"user": user_id},
            )
        db.commit()
    response.delete_cookie("crm_session")
    return {"status": "logged_out"}


@router.get("/api/v1/me")
def me(user=Depends(current_user)):
    return {"id": str(user["id"]), "email": user["email"]}


@router.get("/api/v1/auth/sessions")
def sessions(request: Request, db: Session = Depends(get_db), user=Depends(current_user)):
    current_hash = _token_hash(request.cookies.get("crm_session", ""))
    return list(
        db.execute(
            text("""
              SELECT id, created_at, expires_at, revoked_at, user_agent, ip_address,
                     (token_hash = :current_hash) AS current
              FROM sessions WHERE user_id = :user_id ORDER BY created_at DESC
            """),
            {"user_id": user["id"], "current_hash": current_hash},
        ).mappings()
    )


@router.post("/api/v1/auth/sessions/{session_id}/revoke")
def revoke_session(session_id: UUID, db: Session = Depends(get_db), user=Depends(current_user)):
    revoked = db.execute(
        text("""
          UPDATE sessions SET revoked_at = now()
          WHERE id = :session_id AND user_id = :user_id AND revoked_at IS NULL
          RETURNING id
        """),
        {"session_id": session_id, "user_id": user["id"]},
    ).scalar_one_or_none()
    if not revoked:
        raise HTTPException(status_code=404, detail="session not found")
    db.execute(
        text(
            "INSERT INTO audit_events(user_id, action, entity_type, entity_id) VALUES (:user, 'auth.session.revoked', 'session', :id)"
        ),
        {"user": user["id"], "id": session_id},
    )
    db.commit()
    return {"status": "revoked"}


@router.get("/api/v1/audit/events")
def audit_events(limit: int = 100, db: Session = Depends(get_db), user=Depends(current_user)):
    return list(
        db.execute(
            text("""
              SELECT id, action, entity_type, entity_id, metadata, created_at
              FROM audit_events WHERE user_id = :user_id
              ORDER BY created_at DESC LIMIT :limit
            """),
            {"user_id": user["id"], "limit": min(max(limit, 1), 200)},
        ).mappings()
    )
