"""Durable media joins: either source or bytes may arrive first.

The append-only download queue can be acknowledged once dependency records
commit. Missing source snapshots are a wait state, never a discarded event.
Only the dedicated media lane consumes this table, in bounded batches.
"""
import json
from pathlib import Path

from sqlalchemy import text


def consume(snapshots, cache, media, session_factory):
    from .archive_media import moments_cache
    from .sync_archives import read_media_import_queue, write_media_import_checkpoint
    from .wechat_contacts import sync_contact_media

    queue = cache / 'media-import-queue.jsonl'
    checkpoint = snapshots / 'media-import-queue-state.json'
    records, offset = read_media_import_queue(queue, checkpoint)
    with session_factory() as db:
        for record in records:
            name = record['cache_name']
            if not isinstance(name, str) or Path(name).name != name or name.startswith('.'):
                raise ValueError('Unsafe media dependency name')
            record = {**record, 'tids': [int(tid) for tid in record.get('tids', [record.get('tid')]) if tid is not None]}
            key = name + ':' + record['kind'] + ':' + str(record.get('username') or '')
            db.execute(text("""INSERT INTO media_dependencies(id,cache_name,record) VALUES(:id,:name,CAST(:record AS jsonb))
                ON CONFLICT(id) DO UPDATE SET record=EXCLUDED.record || jsonb_build_object('tids',(
                  SELECT coalesce(jsonb_agg(DISTINCT value),'[]'::jsonb) FROM jsonb_array_elements(
                    (media_dependencies.record->'tids') || (EXCLUDED.record->'tids')))),
                state='waiting',next_attempt_at=now(),updated_at=now()"""),
                {'id': key, 'name': name, 'record': json.dumps(record)})
        db.commit()
    write_media_import_checkpoint(checkpoint, offset)
    with session_factory() as db:
        pending = db.execute(text("""SELECT id,record FROM media_dependencies WHERE state='waiting' AND next_attempt_at<=now()
            ORDER BY created_at,id LIMIT 25""")).all()
        db.commit()
    report = {'records': len(records), 'attempted': len(pending), 'linked_media': 0, 'waiting': 0, 'offset': offset}
    for dependency_id, record in pending:
        name = record['cache_name']
        snapshot = snapshots / ('moments/sns.db' if record['kind'] in ('photo', 'video', 'live_photo') else 'contacts/contact.db')
        ready, reason = False, 'source_pending'
        if not (cache / name).is_file():
            reason = 'file_pending'
        elif snapshot.is_file():
            with session_factory() as db:
                if record['kind'] in ('photo', 'video', 'live_photo'):
                    result = moments_cache(db, snapshot, cache, media,
                        tids={int(tid) for tid in record.get('tids', [record.get('tid')]) if tid is not None},
                        cache_names={name})
                else:
                    role = {'avatar_big': 'wechat_avatar_big', 'avatar_small': 'wechat_avatar_small', 'moments_cover': 'cover'}[record['kind']]
                    result = sync_contact_media(db, snapshot, cache, media, roles={role},
                        usernames={str(record['username'])}, cache_names={name})
                ready = name not in result['unmatched_queue_files'] and not result.get('unsupported_files')
                if result.get('unsupported_files'):
                    reason = 'unsupported_file'
                report['linked_media'] += result.get('linked_media', 0)
        with session_factory() as db:
            if ready:
                db.execute(text('DELETE FROM media_dependencies WHERE id=:id'), {'id': dependency_id})
            else:
                db.execute(text("""UPDATE media_dependencies SET reason=:reason,updated_at=now(),
                    state=CASE WHEN :reason='unsupported_file' THEN 'error' ELSE 'waiting' END,
                    next_attempt_at=now()+interval '5 seconds' WHERE id=:id"""), {'id': dependency_id, 'reason': reason})
                report['waiting'] += 1
            db.commit()
    with session_factory() as db:
        remaining = db.execute(text('SELECT count(*) FROM media_dependencies')).scalar_one()
    report['pending_dependencies'] = remaining
    report['state'] = 'pending' if remaining or (queue.exists() and queue.stat().st_size > offset) else 'complete'
    return report
