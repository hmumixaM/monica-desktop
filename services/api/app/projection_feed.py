"""Durable append-first input dispatch, independent of the CDC history reader."""
import argparse
import hashlib
import json
from datetime import UTC, datetime, timedelta
from uuid import uuid5

from sqlalchemy import text

from .config import get_settings
from .content import canonical_json
from .platform_store import NAMESPACE, PROVIDERS, platform_engine


def batch_key(key, offset=0):
    return 'input:' + hashlib.sha256(key.encode()).hexdigest() + ':' + str(offset)


def subject_keys(request):
    """Serialize related accounts/objects, without blocking unrelated people."""
    keys = set()
    for record in request.records:
        content = record.content if isinstance(record.content, dict) else {}
        social = record.entity_type in ('post', 'story') or request.stream in ('posts', 'moments', 'stories')
        if social and not any(content.get(key) for key in ('fields', 'field_key', 'address', 'employment', 'education')):
            keys.add('object:' + request.stream + ':' + record.external_id)
            continue
        owner = content.get('owner') if isinstance(content.get('owner'), dict) else {}
        for value in (record.source_account_external_id, content.get('username'), owner.get('id'),
                      getattr(record, 'source_account_id', None)):
            if value:
                keys.add('account:' + str(value))
        if record.profile_id:
            keys.add('profile:' + str(record.profile_id))
        if record.entity_type not in ('post', 'story') and request.stream not in ('posts', 'moments', 'stories'):
            keys.add('account:' + record.external_id)
        keys.add('object:' + request.stream + ':' + record.external_id)
    return sorted(keys)


def stage_batch(db, request):
    """Accept bounded immutable source inputs; delivery queue drives CRM writes.

The intake performs no CRM projection. Per-chunk identities preserve retries
without creating a new event when only an observation timestamp changed.
"""
    from .content import get_or_create_content
    from .imports import _write_raw_manifest

    receipt_exists = db.execute(text('SELECT 1 FROM import_batches WHERE idempotency_key=:key'), {'key': request.idempotency_key}).first()


    keys = []
    with platform_engine(request.source).begin() as source:
        for offset in range(0, max(len(request.records), 1), 25):
            key = batch_key(request.idempotency_key, offset)
            chunk = request.model_copy(update={'records': request.records[offset:offset + 25],
                                               'idempotency_key': key, 'batch_id': key})
            records = [r.model_dump(mode='json', exclude={'profile_id', 'sender_profile_id', 'source_updated_at'}) for r in chunk.records]
            digest = hashlib.sha256(canonical_json({'source': request.source, 'stream': request.stream, 'records': records})).hexdigest()
            existing = source.execute(text('SELECT request_hash FROM ingest_batches WHERE idempotency_key=:key'), {'key': key}).scalar_one_or_none()
            if existing is not None:
                if existing != digest:
                    raise ValueError('Import key was reused with different content')
                keys.append(key)
                continue
            manifest, _path = _write_raw_manifest(chunk)
            manifest['input_parent'] = {'key': request.idempotency_key, 'chunks': max(1, (len(request.records) + 24) // 25)}
            manifest['input_subjects'] = subject_keys(chunk)
            dated = [(record.occurred_at.replace(tzinfo=UTC) if record.occurred_at.tzinfo is None else record.occurred_at)
                     for record in chunk.records if record.occurred_at]
            manifest['input_priority'] = (90 if dated and max(dated) >= datetime.now(UTC) - timedelta(days=7)
                                        else 10 if dated else 40 if request.stream == 'contacts' else 50)
            inserted = source.execute(text("""INSERT INTO ingest_batches(id,idempotency_key,request_hash,manifest)
                VALUES(:id,:key,:digest,CAST(:manifest AS jsonb)) ON CONFLICT(idempotency_key) DO NOTHING RETURNING id"""),
                {'id': uuid5(NAMESPACE, request.source + ':' + key), 'key': key, 'digest': digest,
                 'manifest': json.dumps(manifest)}).scalar_one_or_none()
            if inserted is None and source.execute(text('SELECT request_hash FROM ingest_batches WHERE idempotency_key=:key'), {'key': key}).scalar_one() != digest:
                raise ValueError('Concurrent import reused a key with different content')
            keys.append(key)
        source.execute(text('INSERT INTO projection_delivery(batch_id) SELECT id FROM ingest_batches WHERE idempotency_key=ANY(:keys) ON CONFLICT DO NOTHING'), {'keys': keys})
    if not receipt_exists:
        manifest, path = _write_raw_manifest(request)
        manifest_id, _ = get_or_create_content(db, value=manifest, content_kind='raw_json', storage_uri=path, store_payload=False)
        db.execute(text("""INSERT INTO import_batches(source,stream,schema_version,batch_id,idempotency_key,
            cursor_before,cursor_after,raw_manifest_object_id,status) VALUES(:source,:stream,:version,:batch,:key,
            :before,:after,:manifest,'queued') ON CONFLICT(idempotency_key) DO NOTHING"""),
            {'source': request.source, 'stream': request.stream, 'version': request.schema_version,
             'batch': request.batch_id, 'key': request.idempotency_key, 'before': request.cursor_before,
             'after': request.cursor_after, 'manifest': manifest_id})
    finalize_parent(db, request.idempotency_key, len(keys))
    return dict(db.execute(text('SELECT * FROM import_batches WHERE idempotency_key=:key'), {'key': request.idempotency_key}).mappings().one())


def finalize_parent(db, key, chunks):
    keys = [batch_key(key, offset * 25) for offset in range(chunks)]
    counts = db.execute(text("""SELECT count(*) AS done,coalesce(sum(inserted_count),0) AS inserted,
        coalesce(sum(changed_count),0) AS changed,coalesce(sum(unchanged_count),0) AS unchanged
        FROM import_batches WHERE idempotency_key=ANY(:keys) AND status='complete'"""), {'keys': keys}).mappings().one()
    if counts['done'] == chunks:
        db.execute(text("""UPDATE import_batches SET status='complete',completed_at=now(),inserted_count=:inserted,
            changed_count=:changed,unchanged_count=:unchanged WHERE idempotency_key=:key AND status<>'complete'"""),
            {**counts, 'key': key})


def project_event(db, payload):
    from .imports import hydrate_manifest, project_batch
    from .schemas import ImportBatchRequest

    event = db.execute(text('SELECT * FROM projection_inbox WHERE id=:id'), {'id': int(payload['event_id'])}).mappings().one()
    if event['projected_at']:
        return {'state': 'unchanged'}
    request = ImportBatchRequest.model_validate(hydrate_manifest(event['manifest']))
    if request.source != event['provider'] or request.source != payload['provider']:
        raise ValueError('Input provider mismatch')
    db.info['input_projecting'] = True
    try:
        result = project_batch(db, request)
        parent = event['manifest'].get('input_parent')
        if parent:
            finalize_parent(db, parent['key'], int(parent['chunks']))
        db.execute(text('UPDATE projection_inbox SET projected_at=now() WHERE id=:id'), {'id': event['id']})
        return result
    finally:
        db.info.pop('input_projecting', None)


def retry_event(db, event):
    from .job_queue import enqueue

    row = db.execute(text('SELECT provider,projected_at,subject_keys FROM projection_inbox WHERE id=:id'), {'id': event}).mappings().one()
    if row['projected_at']:
        return {'state': 'complete'}
    enqueue(db, 'source.project', {'provider': row['provider'], 'event_id': event, 'subjects': row['subject_keys']}, refresh=True)
    return {'state': 'queued'}


def dispatch(provider, crm_engine, *, before_ack=None):
    """Copy committed immutable inputs; acknowledge only after inbox commit.

    No high-water sequence cursor: sequence allocation and commit order may
    differ. A narrow delivery table prevents a late commit from being skipped.
    Concurrent dispatchers rely on uniqueness, without any advisory/row locks.
    """
    with platform_engine(provider).connect() as source:
        rows = source.execute(text('''SELECT b.id,b.manifest FROM projection_delivery d
            JOIN ingest_batches b ON b.id=d.batch_id WHERE d.delivered_at IS NULL
            ORDER BY b.sequence LIMIT 50''')).mappings().all()
    if not rows:
        return 0
    added = 0
    from .job_queue import enqueue
    with crm_engine.begin() as crm:
        for row in rows:
            manifest = row['manifest']
            subjects = manifest.get('input_subjects', [])
            event = crm.execute(text('''INSERT INTO projection_inbox(provider,source_batch_id,manifest,subject_keys)
                VALUES(:provider,:batch,CAST(:manifest AS jsonb),:subjects)
                ON CONFLICT(provider,source_batch_id) DO NOTHING RETURNING id'''),
                {'provider': provider, 'batch': row['id'], 'manifest': json.dumps(manifest), 'subjects': subjects}).scalar_one_or_none()
            if event is not None:
                enqueue(crm, 'source.project', {'event_id': event, 'provider': provider, 'subjects': subjects},
                        priority=max(0, min(100, int(manifest.get('input_priority', 50)))))
                added += 1
    if before_ack:
        before_ack()
    with platform_engine(provider).begin() as source:
        source.execute(text('''UPDATE projection_delivery SET delivered_at=now()
            WHERE batch_id=ANY(:ids) AND delivered_at IS NULL'''), {'ids': [row['id'] for row in rows]})
    return added


def main():
    from .db import engine
    from .runtime_status import STOP, install_shutdown

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('watch', 'retry'))
    parser.add_argument('--event-id', type=int)
    args = parser.parse_args()
    if not get_settings().async_projection_enabled:
        raise SystemExit('ASYNC_PROJECTION_ENABLED is required')
    if args.command == 'retry':
        if args.event_id is None:
            parser.error('--event-id is required')
        with engine.begin() as db:
            print(json.dumps(retry_event(db, args.event_id)))
        return
    install_shutdown()
    while not STOP.is_set():
        for provider in PROVIDERS:
            if STOP.is_set():
                break
            try:
                dispatch(provider, engine)
            except Exception as error:  # noqa: BLE001 - isolate source intake failures
                code = getattr(getattr(error, 'orig', None), 'sqlstate', None) or type(error).__name__
                print(json.dumps({'input_dispatch': provider, 'error_code': code}), flush=True)
        STOP.wait(1)


if __name__ == '__main__':
    main()
