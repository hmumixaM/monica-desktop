from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session
from test_desktop import desktop_settings
from test_desktop import desktop_stores as desktop_stores  # noqa: PLC0414

from app import cdc, job_queue, projection_feed
from app.config import get_settings
from app.desktop_init import initialize
from app.imports import project_batch
from app.schemas import ImportBatchRequest


@pytest.fixture
def pipeline(crm_db, desktop_stores, monkeypatch):
    desktop_settings(monkeypatch)
    monkeypatch.setenv('CDC_ENABLED', 'true')
    monkeypatch.setenv('ASYNC_PROJECTION_ENABLED', 'true')
    get_settings.cache_clear()
    monkeypatch.setattr(cdc, 'platform_engine', desktop_stores.__getitem__)
    monkeypatch.setattr(projection_feed, 'platform_engine', desktop_stores.__getitem__)
    initialize(crm_db)
    schema = crm_db.execute(text('SELECT current_schema()')).scalar_one()
    crm_db.commit()
    engine = create_engine(crm_db.get_bind().engine.url.update_query_dict({'options': '-csearch_path=' + schema + ',public'}))
    monkeypatch.setattr(cdc, 'audit_engine', lambda provider: engine if provider == 'crm' else desktop_stores[provider])
    with engine.begin() as db:
        cdc.initialize(db)
    try:
        yield engine, desktop_stores
    finally:
        for provider, source_engine in {**desktop_stores, 'crm': engine}.items():
            with source_engine.connect().execution_options(isolation_level='AUTOCOMMIT') as source:
                slot = cdc.slot_name(provider, source_engine.url.database)
                source.execute(text('SELECT pg_drop_replication_slot(:slot)'), {'slot': slot})
        engine.dispose()


def request(key='fixture', value='Collected'):
    return ImportBatchRequest(source='instagram', stream='profiles', batch_id=key,
        idempotency_key=key, observed_at=datetime.now(UTC), records=[{
            'external_id': '1001', 'content': {'display_name': 'Fixture', 'username': 'fixture',
                                            'fields': {'instagram.biography': value}},
        }])


def project_next(engine):
    job = job_queue.claim(engine, 'cdc-test', 'projection')
    assert job is not None
    with engine.begin() as connection, Session(bind=connection) as db:
        projection_feed.project_event(db, job['payload'])
        job_queue.finish(connection, job)
    return job


def test_projection_does_not_require_cdc_and_replay_is_idempotent(crm_db, pipeline):
    engine, sources = pipeline
    accepted = project_batch(crm_db, request())
    crm_db.commit()
    assert accepted['status'] == 'queued'
    with engine.connect() as audit:
        assert audit.execute(text('SELECT count(*) FROM cdc_events')).scalar_one() == 0
    assert crm_db.execute(text('SELECT count(*) FROM profiles')).scalar_one() == 0
    crm_db.commit()
    assert projection_feed.dispatch('instagram', engine) == 1
    project_next(engine)
    with engine.connect() as db:
        assert db.execute(text('SELECT count(*) FROM profiles')).scalar_one() == 1
        assert db.execute(text('SELECT count(*) FROM projection_inbox WHERE projected_at IS NULL')).scalar_one() == 0
        assert db.execute(text("SELECT count(*) FROM import_batches WHERE status='complete'")).scalar_one() == 2
    project_batch(crm_db, request())
    crm_db.commit()
    projection_feed.dispatch('instagram', engine)
    assert job_queue.claim(engine, 'cdc-test', 'projection') is None
    with sources['instagram'].connect() as source:
        assert source.execute(text('SELECT count(*) FROM ingest_batches')).scalar_one() == 1


def test_capture_commit_before_ack_can_replay_without_duplicate_jobs(crm_db, pipeline):
    engine, _sources = pipeline
    project_batch(crm_db, request())
    crm_db.commit()
    def crash():
        raise RuntimeError('simulated crash before WAL acknowledgement')
    with pytest.raises(RuntimeError, match='simulated crash'):
        projection_feed.dispatch('instagram', engine, before_ack=crash)
    assert projection_feed.dispatch('instagram', engine) == 0
    with engine.connect() as db:
        assert db.execute(text('SELECT count(*) FROM projection_inbox')).scalar_one() == 1
        assert db.execute(text("SELECT count(*) FROM jobs WHERE job_type='source.project'")).scalar_one() == 1
    project_next(engine)


def test_native_update_delete_capture_old_values_and_rollback_is_invisible(pipeline):
    engine, sources = pipeline
    with sources['instagram'].begin() as source:
        source.execute(text("INSERT INTO accounts(id,external_id,object_kind,display_name) VALUES(gen_random_uuid(),'raw','person','Before')"))
    cdc.capture_provider('instagram', engine)
    with sources['instagram'].connect() as source:
        source.execute(text("UPDATE accounts SET display_name='Rolled back' WHERE external_id='raw'"))
        source.rollback()
    assert cdc.capture_provider('instagram', engine) == 0
    with sources['instagram'].begin() as source:
        source.execute(text("UPDATE accounts SET display_name='After' WHERE external_id='raw'"))
    cdc.capture_provider('instagram', engine)
    with sources['instagram'].begin() as source:
        source.execute(text("DELETE FROM accounts WHERE external_id='raw'"))
    cdc.capture_provider('instagram', engine)
    with engine.connect() as db:
        rows = db.execute(text('SELECT operation,before_row,after_row FROM cdc_events ORDER BY id')).all()
    assert [row.operation for row in rows] == ['I', 'U', 'D']
    assert rows[1].before_row['display_name'] == 'Before'
    assert rows[1].after_row['display_name'] == 'After'
    assert rows[2].before_row['display_name'] == 'After'


def test_history_preserves_unchanged_large_toast_value(pipeline):
    import secrets

    engine, _sources = pipeline
    summary = secrets.token_hex(12000)
    with engine.begin() as db:
        profile = db.execute(text("INSERT INTO profiles(profile_type,display_name,summary) VALUES('person','Before',:summary) RETURNING id"),
                             {'summary': summary}).scalar_one()
    cdc.capture_provider('crm', engine)
    with engine.begin() as db:
        db.execute(text("UPDATE profiles SET display_name='After' WHERE id=:id"), {'id': profile})
    cdc.capture_provider('crm', engine)
    with engine.connect() as db:
        row = db.execute(text("SELECT before_row,after_row FROM cdc_events WHERE relation_name='profiles' AND operation='U'")).one()
    assert row.before_row['summary'] == summary
    assert row.after_row['summary'] == summary
    assert '_unchanged_toast_columns' not in row.after_row


def test_ordered_aba_survives_blocked_earlier_projection(crm_db, pipeline):
    engine, sources = pipeline
    for index, value in enumerate(('A', 'B', 'A')):
        project_batch(crm_db, request(str(index), value))
        crm_db.commit()
    projection_feed.dispatch('instagram', engine)
    first = job_queue.claim(engine, 'first', 'projection')
    assert first is not None
    assert job_queue.claim(engine, 'other', 'projection') is None
    with engine.begin() as connection, Session(bind=connection) as db:
        projection_feed.project_event(db, first['payload'])
        job_queue.finish(connection, first)
    project_next(engine)
    project_next(engine)
    with sources['instagram'].connect() as source:
        values = source.execute(text("""SELECT p.value FROM revisions r JOIN objects o ON o.id=r.object_id
            JOIN payloads p ON p.sha256=r.content_hash WHERE o.object_kind='field'
            ORDER BY r.revision_number""")).scalars().all()
    assert values == ['A', 'B', 'A']


def test_mismatched_input_key_is_rejected_before_another_projection(crm_db, pipeline):
    project_batch(crm_db, request(value='A'))
    crm_db.commit()
    with pytest.raises(ValueError, match='different content'):
        project_batch(crm_db, request(value='B'))
    crm_db.rollback()


def test_history_inbox_failure_does_not_ack_wal(pipeline, monkeypatch):
    engine, sources = pipeline
    with sources['instagram'].begin() as source:
        source.execute(text("INSERT INTO accounts(id,external_id,object_kind,display_name) VALUES(gen_random_uuid(),'history','person','Before')"))
    with monkeypatch.context() as context:
        context.setattr(cdc, 'persist_transaction', lambda *_: (_ for _ in ()).throw(RuntimeError('inbox failure')))
        with pytest.raises(RuntimeError, match='inbox failure'):
            cdc.capture_provider('instagram', engine)
    assert cdc.capture_provider('instagram', engine) == 1


def test_lost_slot_fails_closed_instead_of_silently_skipping_changes(crm_db, pipeline):
    engine, sources = pipeline
    project_batch(crm_db, request())
    crm_db.commit()
    projection_feed.dispatch('instagram', engine)
    source_engine = sources['instagram']
    slot = cdc.slot_name('instagram', source_engine.url.database)
    with source_engine.connect().execution_options(isolation_level='AUTOCOMMIT') as source:
        source.execute(text('SELECT pg_drop_replication_slot(:slot)'), {'slot': slot})
    with engine.begin() as db, pytest.raises(RuntimeError, match='disappeared'):
        cdc.initialize(db)
    project_next(engine)  # Live updates are independent of the history slot.
    # Restore only for the fixture cleanup; this is NOT an application reset.
    with source_engine.connect().execution_options(isolation_level='AUTOCOMMIT') as source:
        source.execute(text("SELECT pg_create_logical_replication_slot(:slot,'pgoutput')"), {'slot': slot})


def test_story_media_waits_for_cdc_projection_before_acknowledging_archive(crm_db, pipeline, tmp_path):
    from PIL import Image

    from app.sync_instagram_stories import import_story

    engine, _sources = pipeline
    image = tmp_path / 'story.jpg'
    Image.new('RGB', (12, 8), 'blue').save(image)
    doc = {'mediaid': '2002', 'owner_id': '1001', 'owner_username': 'fixture', 'captured_at': 1700000000,
           'taken_at': '2023-11-14T00:00:00Z', 'expiring_at': '2023-11-15T00:00:00Z', 'is_video': False}
    assert import_story(crm_db, doc, [image], tmp_path / 'media') == {'pending_projection': 1}
    projection_feed.dispatch('instagram', engine)
    project_next(engine)
    result = import_story(crm_db, doc, [image], tmp_path / 'media')
    assert result['added_media_links'] == 1
    assert crm_db.execute(text('SELECT count(*) FROM social_stories')).scalar_one() == 1
    assert crm_db.execute(text("SELECT count(*) FROM media_links WHERE entity_type='social_story'")).scalar_one() == 1


def test_blocked_account_does_not_block_an_unrelated_account(crm_db, pipeline):
    engine, _sources = pipeline
    project_batch(crm_db, request('first'))
    other = request('other').model_copy(update={'records': [request('other').records[0].model_copy(
        update={'external_id': '2002', 'content': {'username': 'other', 'display_name': 'Other'}})]})
    project_batch(crm_db, other)
    crm_db.commit()
    projection_feed.dispatch('instagram', engine)
    first = job_queue.claim(engine, 'first', 'projection')
    second = job_queue.claim(engine, 'second', 'projection')
    assert first and second and first['id'] != second['id']


def test_linkedin_batches_project_and_local_assets_join_after_cdc(crm_db, pipeline, tmp_path):
    from test_linkedin_archive import local_export

    from app.archive_linkedin import archive
    from app.linkedin_import import load_linkedin

    engine, _sources = pipeline
    snapshot = local_export(tmp_path)
    load_linkedin(crm_db, tmp_path, {}, export_snapshot=snapshot, incremental=True)
    crm_db.commit()
    projection_feed.dispatch('linkedin', engine)
    while True:
        job = job_queue.claim(engine, 'linkedin-test', 'projection')
        if job is None:
            break
        with engine.begin() as connection, Session(bind=connection) as db:
            projection_feed.project_event(db, job['payload'])
            job_queue.finish(connection, job)
    result = archive(crm_db, snapshot, tmp_path, tmp_path / 'media')
    assert result['added_links'] == 1 and result['registered_files'] == 1
    assert crm_db.execute(text('SELECT count(*) FROM social_posts')).scalar_one() == 1
    assert crm_db.execute(text('SELECT count(*) FROM profiles')).scalar_one() == 1


def test_same_person_different_posts_can_append_concurrently(crm_db, pipeline):
    from concurrent.futures import ThreadPoolExecutor

    engine, _sources = pipeline
    project_batch(crm_db, request('profile'))
    crm_db.commit()
    projection_feed.dispatch('instagram', engine)
    project_next(engine)
    for number in (1, 2):
        batch = request('post-' + str(number)).model_copy(update={'stream': 'posts', 'records': [
            request().records[0].model_copy(update={'external_id': str(number), 'entity_type': 'post',
                'source_account_external_id': '1001', 'content': {'text': 'Post ' + str(number),
                    'username': 'fixture', 'owner': {'id': '1001'}}})]})
        project_batch(crm_db, batch)
        crm_db.commit()
    projection_feed.dispatch('instagram', engine)
    jobs = [job_queue.claim(engine, str(index), 'projection') for index in range(2)]
    assert all(jobs)
    def apply(job):
        with engine.begin() as connection, Session(bind=connection) as db:
            projection_feed.project_event(db, job['payload'])
            job_queue.finish(connection, job)
    with ThreadPoolExecutor(2) as workers:
        list(workers.map(apply, jobs))
    with engine.connect() as db:
        assert db.execute(text('SELECT count(*) FROM social_posts')).scalar_one() == 2
        assert db.execute(text('SELECT count(*) FROM profiles')).scalar_one() == 1


def test_crm_history_resolves_field_values_and_never_enqueues_projection(crm_db, pipeline):
    from app.routers.cdc import event_detail

    engine, _sources = pipeline
    for index, value in enumerate(('Before', 'After')):
        project_batch(crm_db, request(str(index), value))
        crm_db.commit()
        projection_feed.dispatch('instagram', engine)
        project_next(engine)
        cdc.capture_provider('crm', engine)
    with engine.connect() as db:
        event = db.execute(text("SELECT id FROM cdc_events WHERE provider='crm' AND relation_name='profile_field_current' AND operation='U' ORDER BY id DESC LIMIT 1")).scalar_one()
        detail = event_detail(event, db)
        assert detail['changed_fields'] == [{'field': 'instagram.biography', 'before': 'Before', 'after': 'After', 'had_before': True, 'has_after': True}]
        assert db.execute(text("SELECT count(*) FROM jobs WHERE job_type='source.project' AND status='queued'")).scalar_one() == 0


def test_late_source_commit_is_not_skipped_by_a_sequence_watermark(crm_db, pipeline):
    from uuid import uuid4

    engine, sources = pipeline
    late_id = uuid4()
    late_request = request('late')
    manifest = late_request.model_dump(mode='json')
    with sources['instagram'].connect() as late:
        late.execute(text("""INSERT INTO ingest_batches(id,idempotency_key,request_hash,manifest)
            VALUES(:id,'late',:hash,CAST(:manifest AS jsonb))"""),
            {'id': late_id, 'hash': 'a'*64, 'manifest': __import__('json').dumps(manifest)})
        late.execute(text('INSERT INTO projection_delivery(batch_id) VALUES(:id)'), {'id': late_id})
        project_batch(crm_db, request('committed-first'))
        crm_db.commit()
        assert projection_feed.dispatch('instagram', engine) == 1
        late.commit()
    assert projection_feed.dispatch('instagram', engine) == 1
    with engine.connect() as db:
        assert db.execute(text('SELECT count(*) FROM projection_inbox')).scalar_one() == 2


def test_source_commit_before_receipt_failure_can_rebuild_receipt(crm_db, pipeline, monkeypatch):
    from app import imports

    engine, _sources = pipeline
    original = imports._write_raw_manifest
    def interrupted(batch):
        if batch.idempotency_key == 'receipt-crash':
            raise RuntimeError('receipt crash after source commit')
        return original(batch)
    with monkeypatch.context() as context:
        context.setattr(imports, '_write_raw_manifest', interrupted)
        with pytest.raises(RuntimeError, match='receipt crash'):
            project_batch(crm_db, request('receipt-crash'))
        crm_db.rollback()
    result = project_batch(crm_db, request('receipt-crash'))
    crm_db.commit()
    assert result['status'] == 'queued'
    assert projection_feed.dispatch('instagram', engine) == 1
    project_next(engine)
