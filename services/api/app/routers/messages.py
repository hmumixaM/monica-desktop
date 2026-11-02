"""Conversations, message history, chat months and contact events."""
import base64
import json
from datetime import datetime
from uuid import UUID

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
)
from sqlalchemy import text
from sqlalchemy.orm import Session

from ..aggregate_cache import cached_endpoint
from ..auth import current_user
from ..config import get_settings
from ..db import get_db

router = APIRouter()
settings = get_settings()


@router.get('/api/v1/profiles/{profile_id}/contact-events/summary')
@cached_endpoint("contact-event-summary", ("contact_events", "links"))
def contact_event_summary(profile_id: UUID, db: Session=Depends(get_db), _user=Depends(current_user)):
    from ..contact_events import summary
    return summary(db,profile_id)


@router.get('/api/v1/profiles/{profile_id}/contact-events')
def contact_event_records(profile_id: UUID, kind: str='call', offset: int=0, limit: int=30,
                          db: Session=Depends(get_db), _user=Depends(current_user)):
    if kind not in ('call','money'):raise HTTPException(status_code=422,detail='Unknown record kind')
    from ..contact_events import records
    return records(db,profile_id,kind,offset,limit)


@router.get('/api/v1/profiles/{profile_id}/chat-months')
@cached_endpoint("chat-months", ("contact_events", "links"))
def profile_chat_months(profile_id: UUID, db: Session = Depends(get_db), _user=Depends(current_user)):
    from ..chat_months import profile_months
    return profile_months(db,profile_id)


def message_month_bounds(month, timezone):
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
    try:
        zone=ZoneInfo(timezone)
        start=datetime.strptime(month,'%Y-%m').replace(tzinfo=zone)
        if start.strftime('%Y-%m')!=month:raise ValueError()
        end=start.replace(year=start.year+1,month=1) if start.month==12 else start.replace(month=start.month+1)
        return start,end
    except (ValueError,ZoneInfoNotFoundError):
        raise HTTPException(status_code=422,detail='Invalid month or timezone') from None


@router.get("/api/v1/profiles/{profile_id}/messages")
def profile_messages(
    profile_id: UUID | None, before: datetime | None = None, before_id: UUID | None = None,
    conversation_type: str | None = None, q: str | None = None, limit: int = 50,
    conversation_id: UUID | None = None, month: str | None = None, timezone: str = "UTC",
    message: UUID | None = None,
    db: Session = Depends(get_db), _user=Depends(current_user),
):
    clauses=[]
    message_clauses=['s.conversation_id=c.id']
    params={"limit":min(max(limit,1),100)}
    if profile_id:
        clauses.append('c.profile_id=:profile_id');params['profile_id']=profile_id
    if conversation_id:
        clauses.append('c.id=:conversation_id');params['conversation_id']=conversation_id
    if not clauses:raise HTTPException(status_code=422,detail='Select a conversation or profile')
    if month:
        start,end=message_month_bounds(month,timezone)
        message_clauses.extend(["s.occurred_at>=:month_start","s.occurred_at<:month_end"])
        params.update(month_start=start,month_end=end)
    if before:
        message_clauses.append("(s.occurred_at,s.message_id)<(:before,CAST(:before_id AS uuid))")
        params.update(before=before,before_id=before_id or UUID('ffffffff-ffff-ffff-ffff-ffffffffffff'))
    if message and not before:
        message_clauses.append("""(s.occurred_at,s.message_id) <= (
            SELECT anchor.occurred_at,anchor.message_id FROM message_search anchor
            WHERE anchor.message_id=:anchor AND anchor.conversation_id=c.id
            ORDER BY anchor.occurred_at DESC LIMIT 1)""")
        params['anchor'] = message
    if conversation_type in {'direct','group'}:
        clauses.append('c.conversation_type=:kind');params['kind']=conversation_type
    if q:
        message_clauses.append("s.body ILIKE :q");params['q']='%'+q.replace('\\','\\\\').replace('%','\\%').replace('_','\\_')+'%'
    # Resolve the bounded result page first. Passing its exact dates into the
    # second query lets PostgreSQL prune irrelevant historical partitions while
    # planning, rather than opening every partition for a parameterized join.
    selected=list(db.execute(text(f"""
        SELECT s.*,c.conversation_type FROM conversations c JOIN LATERAL (
            SELECT s.message_id,s.occurred_at,s.conversation_id,s.body FROM message_search s
            WHERE {' AND '.join(message_clauses)}
            ORDER BY s.occurred_at DESC,s.message_id DESC LIMIT :limit
        ) s ON true WHERE {' AND '.join(clauses)}
        ORDER BY s.occurred_at DESC,s.message_id DESC LIMIT :limit
    """),params).mappings())
    if not selected:return []
    dates=sorted({row['occurred_at'] for row in selected})
    messages=db.execute(text("""SELECT m.id,m.occurred_at,m.message_type,
        CASE WHEN m.sender_account_id IS NOT NULL THEN account_link.profile_id ELSE m.sender_profile_id END AS sender_profile_id,
        p.display_name AS sender_name
        FROM message_events m
        LEFT JOIN source_account_links account_link ON account_link.account_id=m.sender_account_id
        LEFT JOIN profiles p ON p.id=CASE WHEN m.sender_account_id IS NOT NULL THEN account_link.profile_id ELSE m.sender_profile_id END
        WHERE m.occurred_at=ANY(CAST(:dates AS timestamptz[])) AND m.id=ANY(CAST(:ids AS uuid[]))
    """),{'dates':dates,'ids':[str(row['message_id']) for row in selected]}).mappings()
    by_key={(row['id'],row['occurred_at']):dict(row) for row in messages}
    return [{**by_key[(row['message_id'],row['occurred_at'])],'conversation_id':row['conversation_id'],
             'conversation_type':row['conversation_type'],'text':row['body']} for row in selected
            if (row['message_id'],row['occurred_at']) in by_key]


@router.get('/api/v1/conversations')
def list_conversations(q: str = '', cursor: str | None = None, limit: int = 40, db: Session = Depends(get_db), _user=Depends(current_user)):
    """Most recent conversations first, for the two-pane chat view."""
    params = {'q': '%' + q + '%', 'limit': min(max(limit, 1), 100), 'cursor_at': None, 'cursor_id': None}
    if cursor:
        try:
            stamp, cursor_id = json.loads(base64.urlsafe_b64decode(cursor.encode()))
            params['cursor_id'] = UUID(cursor_id)
            params['cursor_at'] = datetime.fromisoformat(stamp) if stamp else None
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            raise HTTPException(status_code=422, detail='invalid cursor') from exc
    rows = db.execute(text("""SELECT c.id, c.conversation_type, c.external_id, c.profile_id, p.display_name,
          profile_avatar_media(p.id, p.avatar_media_id) AS avatar_media_id,
          c.last_message_at, c.last_message_preview, c.message_count
        FROM conversations c LEFT JOIN profiles p ON p.id = c.profile_id
        WHERE (p.display_name ILIKE :q OR c.external_id ILIKE :q)
          AND (CAST(:cursor_id AS uuid) IS NULL OR (
            CASE WHEN CAST(:cursor_at AS timestamptz) IS NULL
              THEN c.last_message_at IS NULL AND c.id < CAST(:cursor_id AS uuid)
              ELSE c.last_message_at < CAST(:cursor_at AS timestamptz) OR c.last_message_at IS NULL
                OR (c.last_message_at = CAST(:cursor_at AS timestamptz) AND c.id < CAST(:cursor_id AS uuid)) END))
        ORDER BY c.last_message_at DESC NULLS LAST, c.id DESC LIMIT :limit"""), params).mappings()
    return [{**dict(row), 'cursor': base64.urlsafe_b64encode(json.dumps(
        [row['last_message_at'].isoformat() if row['last_message_at'] else None, str(row['id'])]).encode()).decode()}
        for row in rows]


@router.get('/api/v1/conversations/{conversation_id}/messages')
def conversation_messages(conversation_id: UUID, before: datetime | None = None, before_id: UUID | None = None,
                          q: str | None = None, limit: int = 50, month: str | None = None, timezone: str = "UTC", message: UUID | None = None, db: Session = Depends(get_db), _user=Depends(current_user)):
    return profile_messages(None,before=before,before_id=before_id,q=q,limit=limit,month=month,timezone=timezone,conversation_id=conversation_id,message=message,db=db,_user=_user)
