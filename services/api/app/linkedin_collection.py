"""Human-confirmed LinkedIn accounts and a read-only collector target feed."""

import hmac
import json
import re
from urllib.parse import quote, unquote, urlsplit
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.orm import Session

from .account_links import set_link
from .auth import current_user
from .config import get_settings
from .db import get_db
from .platform_store import account_id, ensure_account, platform_engine

router = APIRouter()


class LinkedInTargetSave(BaseModel):
    account: str = Field(min_length=1, max_length=2048)


def canonical_account(value: str) -> tuple[str, str]:
    value = value.strip()
    if "://" in value or re.match(r"(?:[a-z]+\.)?linkedin\.com/", value, re.IGNORECASE):
        try:
            parsed = urlsplit(value if "://" in value else "https://" + value)
            host = (parsed.hostname or "").lower()
            parts = parsed.path.strip("/").split("/")
            if (
                parsed.scheme not in ("http", "https")
                or parsed.username
                or parsed.password
                or parsed.port not in (None, 80, 443)
                or not re.fullmatch(r"(?:(?:www|[a-z]{2,3})\.)?linkedin\.com", host)
                or len(parts) != 2
                or parts[0] != "in"
            ):
                raise ValueError
            value = parts[1]
        except ValueError:
            raise HTTPException(422, "请输入 LinkedIn 个人主页链接或 /in/ 后的账号") from None
    slug = unquote(value).casefold()
    if (
        not re.fullmatch(r"[\w.~\-]{1,255}", slug)
        or slug in (".", "..")
        or not any(char.isalnum() for char in slug)
    ):
        raise HTTPException(422, "请输入 LinkedIn 个人主页链接或 /in/ 后的账号")
    return slug, "https://www.linkedin.com/in/" + quote(slug, safe=".~-")


def _profile(db, profile_id):
    kind = db.execute(
        text("SELECT profile_type FROM profiles WHERE id=:id AND archived_at IS NULL"),
        {"id": profile_id},
    ).scalar_one_or_none()
    if not kind:
        raise HTTPException(404, "profile not found")
    if kind != "person":
        raise HTTPException(422, "LinkedIn 账号只能添加到人物资料")


def _audit(db, actor, profile_id, action, aid):
    db.execute(
        text("""INSERT INTO audit_events(user_id,action,entity_type,entity_id,metadata)
        VALUES(:actor,:action,'profile',:profile,CAST(:metadata AS jsonb))"""),
        {
            "actor": actor,
            "action": action,
            "profile": profile_id,
            "metadata": json.dumps({"account_id": str(aid)}),
        },
    )


@router.get("/api/v1/profiles/{profile_id}/linkedin-targets")
def profile_targets(profile_id: UUID, db: Session = Depends(get_db), _user=Depends(current_user)):
    _profile(db, profile_id)
    return [
        dict(row)
        for row in db.execute(
            text("""SELECT a.id AS account_id,a.external_id,a.profile_url
        FROM linkedin_collection_targets t JOIN source_accounts a ON a.id=t.account_id
        JOIN source_account_links l ON l.account_id=a.id
        WHERE l.profile_id=:profile ORDER BY t.created_at,a.id"""),
            {"profile": profile_id},
        ).mappings()
    ]


@router.put("/api/v1/profiles/{profile_id}/linkedin-targets")
def save_target(
    profile_id: UUID,
    payload: LinkedInTargetSave,
    db: Session = Depends(get_db),
    user=Depends(current_user),
):
    slug, url = canonical_account(payload.account)
    # Same barrier/order as source ingestion and account management. Serializes
    # competing confirmations before creating a deterministic account projection.
    db.execute(text("SELECT pg_advisory_xact_lock(11803295)"))
    _profile(db, profile_id)
    aid = account_id("linkedin", slug)
    existing = (
        db.execute(
            text("""SELECT a.id,l.profile_id FROM source_accounts a
        LEFT JOIN source_account_links l ON l.account_id=a.id
        WHERE a.provider='linkedin' AND a.external_id=:slug FOR UPDATE OF a"""),
            {"slug": slug},
        )
        .mappings()
        .one_or_none()
    )
    if existing:
        aid = existing["id"]
        if existing["profile_id"] and existing["profile_id"] != profile_id:
            raise HTTPException(409, "此 LinkedIn 账号已关联其他人物，请先在来源账号中核对归属")
    else:
        # Commit source identity first; a failed CRM transaction may leave an
        # unlinked source account, never a fabricated observation or Profile.
        with platform_engine("linkedin").begin() as source:
            ensure_account(source, "linkedin", slug, username=slug, profile_url=url)
        db.execute(
            text("""INSERT INTO source_accounts(id,provider,external_id,object_kind,display_name,username,profile_url,migrated_at)
            VALUES(:id,'linkedin',:slug,'person',:slug,:slug,:url,now())"""),
            {"id": aid, "slug": slug, "url": url},
        )
    set_link(
        db,
        aid,
        profile_id,
        actor=user["id"],
        expected_profile_id=existing["profile_id"] if existing else None,
        evidence={"kind": "manual-linkedin-target", "profile_url": url},
    )
    db.execute(
        text("""INSERT INTO linkedin_collection_targets(account_id,created_by)
        VALUES(:id,:actor) ON CONFLICT DO NOTHING"""),
        {"id": aid, "actor": user["id"]},
    )
    _audit(db, user["id"], profile_id, "linkedin.collection.enabled", aid)
    db.commit()
    return {"account_id": aid, "external_id": slug, "profile_url": url}


@router.delete("/api/v1/profiles/{profile_id}/linkedin-targets/{aid}", status_code=204)
def remove_target(
    profile_id: UUID, aid: UUID, db: Session = Depends(get_db), user=Depends(current_user)
):
    db.execute(text("SELECT pg_advisory_xact_lock(11803295)"))
    _profile(db, profile_id)
    removed = db.execute(
        text("""DELETE FROM linkedin_collection_targets t USING source_account_links l
        WHERE t.account_id=:id AND l.account_id=t.account_id AND l.profile_id=:profile RETURNING t.account_id"""),
        {"id": aid, "profile": profile_id},
    ).scalar_one_or_none()
    if not removed:
        raise HTTPException(404, "collection target not found")
    _audit(db, user["id"], profile_id, "linkedin.collection.disabled", aid)
    db.commit()


def collection_targets(db):
    rows = db.execute(
        text("""SELECT a.external_id FROM linkedin_collection_targets t
        JOIN source_accounts a ON a.id=t.account_id
        JOIN source_account_links l ON l.account_id=a.id
        JOIN profiles p ON p.id=l.profile_id
        WHERE a.provider='linkedin' AND p.profile_type='person' AND p.archived_at IS NULL
          AND NOT EXISTS(SELECT 1 FROM identities i WHERE i.source_account_id=a.id AND i.status='invalid')
        ORDER BY a.external_id""")
    ).scalars()
    targets = []
    for external in rows:
        slug, url = canonical_account(external)
        targets.append(
            {
                "profile_url": url,
                "external_id": slug,
                "metadata": {"provider_user_id": slug, "crm_manual": True},
            }
        )
    return {"version": 1, "targets": targets}


@router.get("/api/v1/linkedin/collection-targets")
def collector_targets(
    db: Session = Depends(get_db), x_linkedin_targets_token: str | None = Header(default=None)
):
    secret = get_settings().linkedin_collection_token
    if (
        not secret
        or not x_linkedin_targets_token
        or not hmac.compare_digest(secret.encode(), x_linkedin_targets_token.encode())
    ):
        raise HTTPException(401, "collector token required")
    return collection_targets(db)
