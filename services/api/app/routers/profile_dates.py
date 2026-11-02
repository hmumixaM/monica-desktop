import json
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy import text
from sqlalchemy.orm import Session

from ..auth import current_user
from ..db import get_db
from ..repositories import get_profile
from ..schemas import SignificantDateWrite
from ..significant_dates import DateError, create_date, delete_date, update_date

router = APIRouter()


def _person(db: Session, profile_id: UUID) -> None:
    profile = get_profile(db, profile_id, for_update=True)
    if not profile:
        raise HTTPException(status_code=404, detail="profile not found")
    if profile["profile_type"] != "person":
        raise HTTPException(status_code=422, detail="not_person")


def _audit(db: Session, user, action: str, profile_id: UUID, metadata: dict) -> None:
    db.execute(text("""INSERT INTO audit_events(user_id, action, entity_type, entity_id, metadata)
        VALUES (:user, :action, 'profile', :id, CAST(:metadata AS jsonb))"""),
        {"user": user["id"], "action": action, "id": profile_id, "metadata": json.dumps(metadata)})


def _rejected(exc: DateError) -> HTTPException:
    status = {"not_found": 404, "birthday_exists": 409}.get(exc.code, 422)
    return HTTPException(status_code=status, detail=exc.code)


@router.post("/api/v1/profiles/{profile_id}/dates", status_code=201)
def add_significant_date(profile_id: UUID, payload: SignificantDateWrite, db: Session = Depends(get_db),
                         user=Depends(current_user)):
    _person(db, profile_id)
    try:
        saved = create_date(db, profile_id, user["id"], **payload.model_dump())
    except DateError as exc:
        raise _rejected(exc) from exc
    _audit(db, user, "profile.date.added", profile_id, saved)
    db.commit()
    return saved


@router.put("/api/v1/profiles/{profile_id}/dates/{date_id}")
def edit_significant_date(profile_id: UUID, date_id: UUID, payload: SignificantDateWrite,
                          db: Session = Depends(get_db), user=Depends(current_user)):
    _person(db, profile_id)
    try:
        saved = update_date(db, profile_id, date_id, user["id"], **payload.model_dump())
    except DateError as exc:
        raise _rejected(exc) from exc
    _audit(db, user, "profile.date.updated", profile_id, saved)
    db.commit()
    return saved


@router.delete("/api/v1/profiles/{profile_id}/dates/{date_id}", status_code=204)
def remove_significant_date(profile_id: UUID, date_id: UUID, db: Session = Depends(get_db),
                            user=Depends(current_user)):
    _person(db, profile_id)
    if not delete_date(db, profile_id, date_id):
        raise HTTPException(status_code=404, detail="not_found")
    _audit(db, user, "profile.date.removed", profile_id, {"id": str(date_id)})
    db.commit()
    return Response(status_code=204)
