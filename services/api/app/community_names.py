"""Community names: typed by hand, or suggested by the model; hand-typed names win."""

from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel, StringConstraints
from sqlalchemy import text
from sqlalchemy.orm import Session

from .auth import current_user
from .db import get_db

router = APIRouter()
MODEL_KIND = "community_name"


def community_labels(db) -> dict[str, dict]:
    names = {row["subject"]: {"label": row["label"], "label_source": "model"} for row in db.execute(text(
        "SELECT subject, output->>'label' AS label FROM ai_outputs WHERE kind = :kind"), {"kind": MODEL_KIND}).mappings()}
    for row in db.execute(text("SELECT community_id, label FROM community_names")).mappings():
        names[row["community_id"]] = {"label": row["label"], "label_source": "manual"}
    return names


class CommunityName(BaseModel):
    label: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=40)]


@router.put("/api/v1/communities/{community_id}/name")
def rename_community(community_id: str, body: CommunityName, db: Session = Depends(get_db), _user=Depends(current_user)):
    label = body.label
    db.execute(text("""INSERT INTO community_names(community_id, label) VALUES(:id, :label)
                       ON CONFLICT (community_id) DO UPDATE SET label = EXCLUDED.label, updated_at = now()"""),
               {"id": community_id, "label": label})
    db.commit()
    return {"key": community_id, "label": label, "label_source": "manual"}


@router.delete("/api/v1/communities/{community_id}/name")
def reset_community_name(community_id: str, db: Session = Depends(get_db), _user=Depends(current_user)):
    db.execute(text("DELETE FROM community_names WHERE community_id = :id"), {"id": community_id})
    db.commit()
    return {"key": community_id, **community_labels(db).get(community_id, {})}
