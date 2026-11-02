"""Home page aggregate: key numbers. Attention lists and organization rankings live in insights."""


from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.orm import Session

from .aggregate_cache import cached_endpoint
from .auth import current_user
from .db import get_db

router = APIRouter()


def _key_numbers(db):
    counts = db.execute(text("""SELECT count(*) FILTER (WHERE profile_type = 'person') AS contacts,
        count(*) FILTER (WHERE profile_type = 'group') AS groups FROM profiles WHERE archived_at IS NULL""")).mappings().one()
    recent = db.execute(text("""SELECT count(*) AS messages_30d,
          count(DISTINCT c.profile_id) FILTER (WHERE c.conversation_type = 'direct') AS active_contacts_30d
        FROM message_events e JOIN conversations c ON c.id = e.conversation_id
        WHERE e.occurred_at >= now() - interval '30 days'""")).mappings().one()
    return {**dict(counts), **dict(recent)}


def build_home(db) -> dict:
    return {"key_numbers": _key_numbers(db)}


@router.get("/api/v1/home")
@cached_endpoint("home", ("profiles", "messages"), ttl=900, stale_on_change=True)
def home(db: Session = Depends(get_db), _user=Depends(current_user)):
    return build_home(db)
