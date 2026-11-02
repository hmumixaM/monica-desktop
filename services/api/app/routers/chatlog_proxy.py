"""Live group speech for one person. Nothing from Chatlog is written to Monica."""

import time
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import text
from sqlalchemy.orm import Session

from ..auth import current_user
from ..chatlog_proxy import (
    group_speech,
    imported_group_messages,
    person_groups,
    person_senders,
    spoken_rooms,
    with_spoken_rooms,
)
from ..config import get_settings
from ..db import get_db

router = APIRouter()


class QueryBudget:
    def __init__(self, db, deadline):
        self.db, self.deadline = db, deadline

    def execute(self, *args, **kwargs):
        remaining = int((self.deadline-time.monotonic())*1000)
        if remaining <= 0:
            raise HTTPException(504, 'Group speech query budget exhausted')
        self.db.execute(text("SELECT set_config('statement_timeout',:budget,true)"), {'budget': str(remaining)})
        return self.db.execute(*args, **kwargs)

    def close(self):
        self.db.close()



@router.get("/api/v1/profiles/{profile_id}/group-speech")
def profile_group_speech(
    profile_id: UUID,
    group_id: UUID | None = None,
    q: str = "",
    since: str = "",
    until: str = "",
    limit: int = 50,
    group_offset: int = 0,
    window_index: int = 0,
    db: Session = Depends(get_db),
    _user=Depends(current_user),
):
    """Return this person's remarks in their WeChat groups.

    The database lookup is finished, and its connection released, before
    Chatlog Alpha is called. A slow room does not hold a Monica query open.
    """
    started = time.monotonic()
    db = QueryBudget(db, started+15)
    settings = get_settings()
    if not settings.chatlog_alpha_url:
        raise HTTPException(status_code=503, detail="Chatlog Alpha is not configured")
    senders = person_senders(db, profile_id)
    if not senders:
        raise HTTPException(status_code=404, detail="这个人没有微信号")
    groups = with_spoken_rooms(person_groups(db, profile_id, group_id), spoken_rooms(db, profile_id, group_id))
    if group_id is not None and not groups:
        raise HTTPException(status_code=404, detail="这个人不在该群里")
    bounded = min(max(limit, 1), 200)
    offset = max(0, group_offset)
    page = groups[offset:offset+10]
    imported = imported_group_messages(db, profile_id, [group['group_id'] for group in page])
    db.close()
    result = group_speech(
        senders=senders,
        groups=page,
        query=q,
        since=since.strip(),
        until=until.strip(),
        limit=bounded,
        base_url=settings.chatlog_alpha_url,
        timeout=settings.chatlog_alpha_timeout_seconds,
        imported=imported,
        window_index=min(max(0, window_index), 8),
        budget=15-(time.monotonic()-started),
    )

    result["next_group_offset"] = offset+10 if offset+10 < len(groups) else None
    return result
