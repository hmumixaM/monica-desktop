"""Disk-bound archive work must not leave PostgreSQL idle in a transaction."""
import hashlib
import sqlite3
import time
from datetime import UTC, datetime

from sqlalchemy import text

from app import archive_media
from app.imports import project_batch
from app.schemas import ImportBatchRequest


def make_moments(db, tmp_path, count):
    cache = tmp_path / 'cache'
    cache.mkdir()
    source = tmp_path / 'sns.db'
    with sqlite3.connect(source) as sns:
        sns.execute('CREATE TABLE SnsTimeLine(tid TEXT,content TEXT)')
        for index in range(count):
            url = f'https://media.test/{index}'
            sns.execute('INSERT INTO SnsTimeLine VALUES (?,?)', (str(index), (
                f'<root><TimelineObject><id>p{index}</id><ContentObject><type>1</type>'
                f'<mediaList><media><url key="key">{url}</url></media></mediaList>'
                '</ContentObject></TimelineObject></root>')))
            name = hashlib.md5(f'{url}|key'.encode()).hexdigest() + '.bin'
            (cache / name).write_bytes(b'\xff\xd8\xff' + str(index).encode())
    project_batch(db, ImportBatchRequest(source='wechat', stream='moments', batch_id='disk',
        idempotency_key='disk', observed_at=datetime.now(UTC), records=[
            {'external_id': f'p{i}', 'entity_type': 'post', 'content': {'text': str(i)}}
            for i in range(count)]))
    db.commit()
    return source, cache


def test_slow_sqlite_and_snapshot_do_not_hit_idle_transaction_timeout(crm_db, tmp_path, monkeypatch):
    db = crm_db
    source, cache = make_moments(db, tmp_path, 3)
    db.execute(text("SET idle_in_transaction_session_timeout='100ms'"))
    db.commit()
    parse = archive_media.parse_moments_xml
    copy = archive_media.copy_media_snapshot

    def slow_parse(xml):
        assert not db.in_transaction()
        time.sleep(.2)
        return parse(xml)

    def slow_copy(*args):
        assert not db.in_transaction()
        time.sleep(.2)
        return copy(*args)

    monkeypatch.setattr(archive_media, 'parse_moments_xml', slow_parse)
    monkeypatch.setattr(archive_media, 'copy_media_snapshot', slow_copy)
    result = archive_media.moments_cache(db, source, cache, tmp_path / 'media')
    assert result['added_links'] == 3
    assert not db.in_transaction()
    # Replays use the registered immutable bytes without copying them again.
    monkeypatch.setattr(archive_media, 'copy_media_snapshot', lambda *args: (_ for _ in ()).throw(
        AssertionError('unchanged file was copied')))
    assert archive_media.moments_cache(db, source, cache, tmp_path / 'media')['added_links'] == 0
    db.execute(text("SET idle_in_transaction_session_timeout=0"))
    db.commit()


def test_multiple_batches_never_read_source_or_copy_files_in_transaction(crm_db, tmp_path, monkeypatch):
    db = crm_db
    source, cache = make_moments(db, tmp_path, 28)
    parse = archive_media.parse_moments_xml
    copy = archive_media.copy_media_snapshot

    def checked_parse(xml):
        assert not db.in_transaction()
        return parse(xml)

    def checked_copy(*args):
        assert not db.in_transaction()
        return copy(*args)

    monkeypatch.setattr(archive_media, 'parse_moments_xml', checked_parse)
    monkeypatch.setattr(archive_media, 'copy_media_snapshot', checked_copy)
    result = archive_media.moments_cache(db, source, cache, tmp_path / 'media')
    assert result['linked_media'] == result['added_links'] == 28
    assert db.execute(text('SELECT count(*) FROM media_assets')).scalar_one() == 28
    db.commit()
    assert archive_media.moments_cache(db, source, cache, tmp_path / 'media')['added_links'] == 0


def test_new_contact_cover_is_staged_without_idle_transaction(crm_db, tmp_path, monkeypatch):
    from uuid import uuid4

    from app import wechat_contacts

    db = crm_db
    account = uuid4()
    db.execute(text("INSERT INTO source_accounts(id,provider,external_id,object_kind,display_name) "
                    "VALUES(:id,'wechat','wx-cover','person','Cover')"), {'id': account})
    db.commit()
    cache = tmp_path / 'cache'
    cache.mkdir()
    url = 'https://media.test/cover'
    name = hashlib.md5(f'{url}|'.encode()).hexdigest() + '.bin'
    (cache / name).write_bytes(b'\xff\xd8\xffcover')
    friend = {'username': 'wx-cover', 'big_head_url': '', 'small_head_url': '', 'moments_cover_url': url}
    monkeypatch.setattr(wechat_contacts, 'read_friends', lambda *args: ([friend], {}))
    monkeypatch.setattr(wechat_contacts, 'retain_source_media', lambda *args: None)
    cached_media = wechat_contacts.cached_media
    copy = archive_media.copy_media_snapshot

    def checked_cached_media(*args):
        assert not db.in_transaction()
        return cached_media(*args)

    def slow_copy(*args):
        assert not db.in_transaction()
        time.sleep(.2)
        return copy(*args)

    monkeypatch.setattr(wechat_contacts, 'cached_media', checked_cached_media)
    monkeypatch.setattr(archive_media, 'copy_media_snapshot', slow_copy)
    db.execute(text("SET idle_in_transaction_session_timeout='100ms'"))
    db.commit()
    report = wechat_contacts.sync_contact_media(db, tmp_path / 'contacts.db', cache,
                                              tmp_path / 'media', cache_names={name})
    assert report['linked'] == 1
    assert report['unmatched_queue_files'] == []
    assert db.execute(text("SELECT count(*) FROM media_links WHERE entity_type='source_account' "
                           "AND entity_id=:id AND role='cover'"), {'id': account}).scalar_one() == 1
    db.execute(text("SET idle_in_transaction_session_timeout=0"))
    db.commit()


def test_failed_disk_batch_replays_queue_without_duplicates(crm_db, tmp_path, monkeypatch):
    import json
    from contextlib import nullcontext

    import pytest

    from app import sync_archives

    db = crm_db
    source, cache = make_moments(db, tmp_path, 28)
    snapshots = tmp_path / 'snapshots'
    (snapshots / 'moments').mkdir(parents=True)
    source.replace(snapshots / 'moments/sns.db')
    # The helper uses tids 0..27; zero is not a real queued WeChat tid, so use
    # source ids 1..28 while retaining each post's external id and cache URL.
    with sqlite3.connect(snapshots / 'moments/sns.db') as sns:
        sns.execute('UPDATE SnsTimeLine SET tid=CAST(tid AS INTEGER)+1')
    queue = cache / 'media-import-queue.jsonl'
    queue.write_text(''.join(json.dumps({'version': 1, 'kind': 'photo', 'tid': i + 1,
        'cache_name': hashlib.md5(f'https://media.test/{i}|key'.encode()).hexdigest() + '.bin'}) + '\n'
        for i in range(28)))
    checkpoint = snapshots / 'media-import-queue-state.json'
    checkpoint.write_text('{"version":1,"offset":0}')
    monkeypatch.setenv('WECHAT_MEDIA_IMPORT_QUEUE', str(queue))
    monkeypatch.setattr(sync_archives, 'SessionLocal', lambda: nullcontext(db))
    copy = archive_media.copy_media_snapshot
    copies = 0

    def interrupted_copy(*args):
        nonlocal copies
        copies += 1
        if copies == 26:
            raise OSError('simulated collector disk failure')
        return copy(*args)

    monkeypatch.setattr(archive_media, 'copy_media_snapshot', interrupted_copy)
    with pytest.raises(OSError, match='simulated collector disk failure'):
        sync_archives.consume_media_import_queue(snapshots, cache, tmp_path / 'media')
    assert json.loads(checkpoint.read_text())['offset'] == 0
    assert db.execute(text('SELECT count(*) FROM media_links')).scalar_one() == 1
    db.commit()
    monkeypatch.setattr(archive_media, 'copy_media_snapshot', copy)
    report = sync_archives.consume_media_import_queue(snapshots, cache, tmp_path / 'media')
    assert report['state'] == 'complete'
    assert report['moments']['added_links'] == 27
    assert json.loads(checkpoint.read_text())['offset'] == queue.stat().st_size
    assert db.execute(text('SELECT count(*) FROM media_links')).scalar_one() == 28
    db.commit()
    assert sync_archives.consume_media_import_queue(snapshots, cache, tmp_path / 'media') == {
        'records': 0, 'state': 'unchanged', 'offset': queue.stat().st_size}


def test_channels_cover_is_queued_and_materialized_as_wechat(crm_db, tmp_path):
    from app.moments_xml import enqueue_finder_covers
    from app.remote_media import materialize_image

    db = crm_db
    make_moments(db, tmp_path, 1)
    post = db.execute(text("SELECT id FROM social_posts WHERE external_id='p0'")).scalar_one()
    url = 'https://finder.video.qq.com/cover'
    enqueue_finder_covers(db, post, [{'source_kind': 'wechat_channels', 'thumbnail_url': url}])
    payload = db.execute(text("SELECT payload FROM jobs WHERE job_type='media.download'")).scalar_one()
    assert payload['url'] == url and payload['role'] == 'thumbnail-1'
    db.commit()
    raw = b'\xff\xd8\xffcover'
    path = tmp_path / 'downloaded-cover'
    path.write_bytes(raw)
    downloaded = (hashlib.sha256(raw).hexdigest(), str(path), len(raw), 'image/jpeg')
    asset = materialize_image(db, payload, tmp_path, downloaded)
    assert db.execute(text('SELECT source_type FROM media_assets WHERE id=:id'), {'id': asset}).scalar_one() == 'wechat'
    assert db.execute(text("SELECT count(*) FROM media_links WHERE entity_id=:id AND role='thumbnail-1'"), {'id': post}).scalar_one() == 1
    db.commit()


def test_quarantined_signed_id_is_recovered_and_linked_end_to_end(crm_db, tmp_path, monkeypatch):
    import base64
    import json
    from contextlib import nullcontext

    from app import sync_archives
    source, cache = make_moments(crm_db, tmp_path, 1)
    tid = -3415012451541445976
    with sqlite3.connect(source) as sns:
        sns.execute('UPDATE SnsTimeLine SET tid=?', (tid,))
    snapshots = tmp_path / 'snapshots'
    (snapshots / 'moments').mkdir(parents=True)
    source.replace(snapshots / 'moments/sns.db')
    quarantine = snapshots / 'media-queue-quarantine'
    quarantine.mkdir()
    record = {'version': 1, 'kind': 'photo', 'tid': tid, 'tids': [tid],
              'cache_name': hashlib.md5(b'https://media.test/0|key').hexdigest() + '.bin'}
    (quarantine / '0.json').write_text(json.dumps({'reason': 'Invalid message IDs',
        'raw_base64': base64.b64encode(json.dumps(record).encode()).decode()}))
    queue = cache / 'queue.jsonl'
    monkeypatch.setenv('WECHAT_MEDIA_IMPORT_QUEUE', str(queue))
    monkeypatch.setattr(sync_archives, 'SessionLocal', lambda: nullcontext(crm_db))
    assert sync_archives.recover_signed_media_queue(snapshots, queue)['recovered_records'] == 1
    report = sync_archives.consume_media_import_queue(snapshots, cache, tmp_path / 'media')
    assert report['moments']['added_links'] == 1
    assert report['unmatched'] == 0
    assert report['offset'] == queue.stat().st_size
    assert sync_archives.recover_signed_media_queue(snapshots, queue)['recovered_records'] == 0
    assert crm_db.execute(text('SELECT count(*) FROM media_links')).scalar_one() == 1
    crm_db.commit()
