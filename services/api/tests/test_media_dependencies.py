import hashlib
import json
import sqlite3
from contextlib import nullcontext
from datetime import UTC, datetime

import pytest
from sqlalchemy import text

from app import media_dependencies, sync_archives
from app.imports import project_batch
from app.schemas import ImportBatchRequest


def fixture(db, tmp_path):
    snapshots, cache, media = tmp_path / 'snapshots', tmp_path / 'cache', tmp_path / 'media'
    snapshots.mkdir()
    cache.mkdir()
    url = 'https://media.test/dependency'
    name = hashlib.md5((url + '|key').encode()).hexdigest() + '.bin'
    record = {'version': 1, 'kind': 'photo', 'cache_name': name, 'tids': [1]}
    queue = cache / 'media-import-queue.jsonl'
    queue.write_text(json.dumps(record) + '\n')

    def content():
        project_batch(db, ImportBatchRequest(source='wechat', stream='moments', batch_id='dep',
            idempotency_key='dep', observed_at=datetime.now(UTC), records=[{
                'external_id': 'p1', 'entity_type': 'post', 'content': {'text': 'Dependency fixture'}}]))
        db.commit()

    def snapshot():
        (snapshots / 'moments').mkdir(exist_ok=True)
        with sqlite3.connect(snapshots / 'moments/sns.db') as source:
            source.execute('CREATE TABLE IF NOT EXISTS SnsTimeLine(tid INTEGER,content TEXT)')
            source.execute('INSERT INTO SnsTimeLine VALUES(1,?)', (
                ('<root><TimelineObject><id>p1</id><ContentObject><type>1</type><mediaList>'
                 f'<media><url key="key">{url}</url></media></mediaList></ContentObject></TimelineObject></root>'),))

    def file():
        from PIL import Image
        Image.new('RGB', (12, 8), 'blue').save(cache / name, format='JPEG')

    def consume():
        return media_dependencies.consume(snapshots, cache, media, lambda: nullcontext(db))

    return content, snapshot, file, consume, queue, name


@pytest.mark.parametrize('order', ['file_first', 'content_first', 'queue_before_snapshot'])
def test_media_join_survives_either_arrival_order_and_queue_replay(crm_db, tmp_path, order, monkeypatch):
    content, snapshot, file, consume, queue, _name = fixture(crm_db, tmp_path)
    if order == 'file_first':
        snapshot()
        file()
    elif order == 'content_first':
        content()
        snapshot()
    else:
        file()
    first = consume()
    assert first['state'] == 'pending' and first['pending_dependencies'] == 1
    assert json.loads((tmp_path / 'snapshots/media-import-queue-state.json').read_text())['offset'] == queue.stat().st_size
    assert crm_db.execute(text('SELECT count(*) FROM media_dependencies')).scalar_one() == 1
    crm_db.commit()
    content()
    if order == 'queue_before_snapshot':
        snapshot()
    if order == 'content_first':
        file()
    crm_db.execute(text('UPDATE media_dependencies SET next_attempt_at=now()'))
    crm_db.commit()
    second = consume()
    assert second['state'] == 'complete' and second['pending_dependencies'] == 0
    assert crm_db.execute(text("SELECT count(*) FROM media_links WHERE entity_type='social_post'")).scalar_one() == 1
    assert crm_db.execute(text('SELECT count(*) FROM media_assets')).scalar_one() == 1
    crm_db.commit()
    with queue.open('a') as stream:
        stream.write(queue.read_text().splitlines()[0] + '\n')
    monkeypatch.setattr('app.archive_media.copy_media_snapshot', lambda *_: pytest.fail('Replayed image copied again'))
    assert consume()['pending_dependencies'] == 0
    assert crm_db.execute(text('SELECT count(*) FROM media_assets')).scalar_one() == 1


def test_crash_after_durable_dependency_before_file_checkpoint_is_replayable(crm_db, tmp_path, monkeypatch):
    _content, _snapshot, _file, consume, _queue, _name = fixture(crm_db, tmp_path)
    with monkeypatch.context() as context:
        context.setattr(sync_archives, 'write_media_import_checkpoint', lambda *_: (_ for _ in ()).throw(RuntimeError('checkpoint crash')))
        with pytest.raises(RuntimeError, match='checkpoint crash'):
            consume()
    assert crm_db.execute(text('SELECT count(*) FROM media_dependencies')).scalar_one() == 1
    crm_db.commit()
    assert consume()['pending_dependencies'] == 1
    assert crm_db.execute(text('SELECT count(*) FROM media_dependencies')).scalar_one() == 1


def test_repeated_cache_file_merges_waiting_post_ids(crm_db, tmp_path):
    _content, _snapshot, _file, consume, queue, _name = fixture(crm_db, tmp_path)
    assert consume()['pending_dependencies'] == 1
    record = json.loads(queue.read_text().strip())
    record['tids'] = [2]
    with queue.open('a') as stream:
        stream.write(json.dumps(record) + '\n')
    assert consume()['pending_dependencies'] == 1
    saved = crm_db.execute(text('SELECT record FROM media_dependencies')).scalar_one()
    assert set(saved['tids']) == {1, 2}
