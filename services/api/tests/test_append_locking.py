from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text
from test_account_links import source_engine as source_engine  # noqa: PLC0414

from app.media_store import WriteContended, lock_media_slot, record_asset
from app.platform_store import ensure_account


def test_unchanged_account_does_not_wait_for_an_unrelated_metadata_writer(source_engine):
    with source_engine.begin() as seed:
        account = ensure_account(seed, 'instagram', '1001', display_name='Fixture', identifiers=['fixture'])
    with source_engine.begin() as writer:
        writer.execute(text('SELECT id FROM accounts WHERE id=:id FOR UPDATE'), {'id': account})
        with source_engine.begin() as reader:
            reader.execute(text("SET LOCAL lock_timeout='100ms'"))
            assert ensure_account(reader, 'instagram', '1001', display_name='Fixture', identifiers=['fixture']) == account


def test_known_media_append_fast_path_takes_no_write_lock(crm_db, tmp_path):
    schema = crm_db.execute(text('SELECT current_schema()')).scalar_one()
    media = record_asset(crm_db, sha256='a'*64, path=tmp_path / 'asset', byte_length=1, media_type='image/jpeg')
    crm_db.commit()
    engine = create_engine(crm_db.get_bind().engine.url.update_query_dict({'options': '-csearch_path=' + schema + ',public'}))
    try:
        with engine.begin() as writer:
            writer.execute(text('SELECT id FROM media_assets WHERE id=:id FOR UPDATE'), {'id': media})
            with engine.begin() as reader:
                reader.execute(text("SET LOCAL lock_timeout='100ms'"))
                assert record_asset(reader, sha256='a'*64, path=tmp_path / 'asset', byte_length=1, media_type='image/jpeg') == media
    finally:
        engine.dispose()


def test_slot_contention_is_nonblocking_and_does_not_lock_other_slots(crm_db):
    engine = crm_db.get_bind().engine
    entity = uuid4()
    with engine.begin() as first:
        lock_media_slot(first, 'social_post', entity, 'media-1', 'https://media.test/one')
        with engine.begin() as other:
            lock_media_slot(other, 'social_post', entity, 'media-2', 'https://media.test/two')
        with engine.begin() as competing, pytest.raises(WriteContended):
            lock_media_slot(competing, 'social_post', entity, 'media-1', 'https://media.test/one')
