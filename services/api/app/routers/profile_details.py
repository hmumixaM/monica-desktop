"""Hand-kept profile details, edited one displayed row at a time.

A row is a whole field, or one entry of a bundled object field such as the
Monica notebook's `monica.custom_fields`. Saving a row can also retire other
rows in the same transaction: the old name of a renamed row, or duplicates
under the same name. Every change is a manual revision, so later imports never
overwrite it and the history tab can restore any earlier value.
"""
import json
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import text
from sqlalchemy.orm import Session

from ..auth import current_user
from ..db import get_db
from ..repositories import append_field_revision, get_profile
from ..schemas import DetailRef, DetailRemove, DetailSave

router = APIRouter()

EDITABLE_SOURCES = {"manual", "monica"}
# Name and summary have their own editor and also live on the profiles row.
RESERVED_KEYS = {"display_name", "summary"}


def _current(db: Session, profile_id: UUID, field_key: str):
    return db.execute(text("""
        SELECT c.current_source_type, co.payload_json AS value
        FROM profile_field_current c
        JOIN profile_field_revisions r ON r.id = c.current_revision_id
        JOIN content_objects co ON co.id = r.content_object_id
        WHERE c.profile_id = :profile AND c.field_key = :key
    """), {"profile": profile_id, "key": field_key}).mappings().one_or_none()


def _write(db: Session, profile_id: UUID, field_key: str, value, actor, operation: str):
    append_field_revision(db, profile_id=profile_id, field_key=field_key, value=value,
                          source_type="manual", operation=operation, actor_user_id=actor)


def _remove(db: Session, profile_id: UUID, ref: DetailRef, actor):
    row = _current(db, profile_id, ref.field_key)
    if (ref.field_key in RESERVED_KEYS or not row or row["value"] is None
            or row["current_source_type"] not in EDITABLE_SOURCES):
        raise HTTPException(status_code=404, detail=f"editable detail {ref.field_key} not found")
    if ref.item is None:
        _write(db, profile_id, ref.field_key, None, actor, "manual_delete")
        return
    bundle = row["value"]
    if not isinstance(bundle, dict) or ref.item not in bundle:
        raise HTTPException(status_code=404, detail=f"detail {ref.item} not found in {ref.field_key}")
    remaining = {key: value for key, value in bundle.items() if key != ref.item}
    _write(db, profile_id, ref.field_key, remaining or None, actor, "manual_edit" if remaining else "manual_delete")


def _audit(db: Session, user, profile_id: UUID, action: str, metadata: dict):
    db.execute(text("""
        INSERT INTO audit_events(user_id, action, entity_type, entity_id, metadata)
        VALUES (:user, :action, 'profile', :id, CAST(:metadata AS jsonb))
    """), {"user": user["id"], "action": action, "id": profile_id, "metadata": json.dumps(metadata, ensure_ascii=False)})


def _require_profile(db: Session, profile_id: UUID):
    if not get_profile(db, profile_id, for_update=True):
        raise HTTPException(status_code=404, detail="profile not found")


@router.put("/api/v1/profiles/{profile_id}/details")
def save_detail(profile_id: UUID, payload: DetailSave, db: Session = Depends(get_db), user=Depends(current_user)):
    _require_profile(db, profile_id)
    field_key, value = payload.field_key.strip(), payload.value.strip()
    if not field_key or not value:
        raise HTTPException(status_code=422, detail="A detail needs a name and a value")
    if field_key in RESERVED_KEYS:
        raise HTTPException(status_code=422, detail="Edit the name and summary from the profile header")
    target = _current(db, profile_id, field_key)
    if target and target["value"] is not None and (
            target["current_source_type"] not in EDITABLE_SOURCES or isinstance(target["value"], dict)):
        raise HTTPException(status_code=409, detail=f"{field_key} is imported data and cannot be overwritten here")
    retired = [ref for ref in payload.replaces if not (ref.field_key == field_key and ref.item is None)]
    for ref in retired:
        _remove(db, profile_id, ref, user["id"])
    _write(db, profile_id, field_key, value, user["id"], "manual_edit")
    _audit(db, user, profile_id, "profile.detail.saved",
           {"field_key": field_key, "replaced": [ref.model_dump() for ref in retired]})
    db.commit()
    return {"field_key": field_key, "value": value}


@router.delete("/api/v1/profiles/{profile_id}/details", status_code=204)
def remove_details(profile_id: UUID, payload: DetailRemove, db: Session = Depends(get_db), user=Depends(current_user)):
    _require_profile(db, profile_id)
    for ref in payload.refs:
        _remove(db, profile_id, ref, user["id"])
    _audit(db, user, profile_id, "profile.detail.removed", {"refs": [ref.model_dump() for ref in payload.refs]})
    db.commit()
