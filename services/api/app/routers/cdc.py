"""Authenticated WAL history and independent projection queue inspection."""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import text

from ..auth import current_user
from ..config import get_settings
from ..db import get_db

router = APIRouter(dependencies=[Depends(current_user)])


def require_enabled():
    if not get_settings().cdc_enabled:
        raise HTTPException(404, 'CDC is not enabled')


@router.get('/api/v1/pipeline/status')
@router.get('/api/v1/cdc/status', dependencies=[Depends(require_enabled)])
def status(db=Depends(get_db)):
    from ..pipeline_status import read
    return read(lambda query: db.execute(text(query)).mappings().all())


@router.get('/api/v1/cdc/events', dependencies=[Depends(require_enabled)])
def events(limit: int = 50, before: int | None = None, provider: str | None = None, db=Depends(get_db)):
    return list(db.execute(text('''SELECT id,provider,commit_lsn::text,relation_name,operation,
        captured_at,committed_at FROM cdc_events
        WHERE (CAST(:before AS bigint) IS NULL OR id<:before)
          AND (CAST(:provider AS text) IS NULL OR provider=:provider)
        ORDER BY id DESC LIMIT :limit'''),
        {'limit': max(1, min(limit, 200)), 'before': before, 'provider': provider}).mappings())


@router.get('/api/v1/cdc/events/{event}', dependencies=[Depends(require_enabled)])
def event_detail(event: int, db=Depends(get_db)):
    row = db.execute(text('''SELECT id,provider,commit_lsn::text,ordinal,relation_name,operation,
        before_row,after_row,captured_at,committed_at FROM cdc_events WHERE id=:id'''), {'id': event}).mappings().one_or_none()
    if row is None:
        raise HTTPException(404, 'CDC event not found')
    result = dict(row)
    old, new = row['before_row'] or {}, row['after_row'] or {}
    if row['provider'] == 'crm' and row['relation_name'] == 'profile_field_current':
        def values(side):
            revision = side.get('current_revision_id')
            if not revision:
                return {}
            value = db.execute(text('''SELECT r.field_key,c.payload_json FROM profile_field_revisions r
                JOIN content_objects c ON c.id=r.content_object_id WHERE r.id=:id'''), {'id': revision}).one_or_none()
            return {value.field_key: value.payload_json} if value else side
        old, new = values(old), values(new)
    result['changed_fields'] = [{'field': key, 'before': old.get(key), 'after': new.get(key),
                                 'had_before': key in old, 'has_after': key in new}
        for key in sorted(old.keys() | new.keys())
        if key not in ('last_seen_at', 'updated_at')
        and (old.get(key) != new.get(key) or (key in old) != (key in new))]
    return result


@router.post('/api/v1/pipeline/tasks/{event}/retry')
def retry(event: int, db=Depends(get_db)):
    from ..projection_feed import retry_event
    if not db.execute(text('SELECT 1 FROM projection_inbox WHERE id=:id'), {'id': event}).first():
        raise HTTPException(404, 'Projection task not found')
    result = retry_event(db, event)
    db.commit()
    return result
