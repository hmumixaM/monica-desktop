import hashlib
import io
import json
import threading
from contextlib import contextmanager, nullcontext

import pytest
from PIL import Image
from sqlalchemy import text
from test_account_links import source_engine as source_engine  # noqa: PLC0414
from test_media_integration import db as db  # noqa: PLC0414

from app.archive_linkedin import archive
from app.full_migration import _linkedin_media, load_linkedin


def test_linkedin_local_archive_retains_unlinked_accounts_and_replays(db,source_engine,tmp_path,monkeypatch):
    out=io.BytesIO();Image.new('RGB',(12,8),'green').save(out,format='PNG');data=out.getvalue();sha=hashlib.sha256(data).hexdigest()
    (tmp_path/'assets').mkdir();(tmp_path/'assets'/f'{sha}.png').write_bytes(data)
    url='https://media.licdn.com/photo.png'
    snapshot={'profiles':[{'provider_user_id':'unlinked-person','profile_url':'https://www.linkedin.com/in/unlinked-person/','profile':{'name':'Unlinked','avatar_url':url,'background_url':url},'posts':[{'provider_post_id':'urn:li:activity:123','text':'photo','image_urls':[url]}]}],
              'local_assets':[{'source_url':url,'content_hash':sha,'local_path':f'{sha}.png','byte_count':len(data),'media_type':'image/png'}]}
    db.execute(text("INSERT INTO platform_migration_state(provider,enabled) VALUES('linkedin',true)"));db.commit()
    load_linkedin(db,tmp_path,{},export_snapshot=snapshot)
    from app import remote_media
    retain=remote_media.retain_post_media_batch
    def fail_after_source_commit(*args):
        retain(*args)
        raise RuntimeError('Simulated CRM commit failure after source commit')
    monkeypatch.setattr(remote_media,'retain_post_media_batch',fail_after_source_commit)
    with pytest.raises(RuntimeError):archive(db,snapshot,tmp_path,tmp_path/'media')
    db.rollback()
    assert db.execute(text("SELECT count(*) FROM media_links WHERE entity_type='social_post'")).scalar_one()==0
    monkeypatch.setattr(remote_media,'retain_post_media_batch',retain)
    first=archive(db,snapshot,tmp_path,tmp_path/'media')
    assert first['registered_files']==1 and first['added_links']==1
    assert db.execute(text('SELECT count(*) FROM profiles')).scalar_one()==0
    assert db.execute(text('SELECT count(*) FROM source_account_media')).scalar_one()==2
    with source_engine.connect() as connection:
        count=connection.execute(text('SELECT count(*) FROM revisions')).scalar_one()
        assert connection.execute(text('SELECT count(*) FROM media_links')).scalar_one()==3
    second=archive(db,snapshot,tmp_path,tmp_path/'media')
    assert second['added_links']==0
    with source_engine.connect() as connection:
        assert connection.execute(text('SELECT count(*) FROM revisions')).scalar_one()==count
    snapshot['local_assets'][0]['local_path']='../escape.png'
    with pytest.raises(ValueError):archive(db,snapshot,tmp_path,tmp_path/'media')


def test_linkedin_video_keeps_downloaded_poster():
    media=_linkedin_media({'image_urls':['https://media.licdn.com/poster.jpg'],'video_url':'https://media.licdn.com/clip.mp4'})
    assert media==[{'role':'video-1','media_type':'video','source_url':'https://media.licdn.com/clip.mp4','thumbnail_url':'https://media.licdn.com/poster.jpg'}]


def test_cached_linkedin_manifest_skips_disk_gate_and_unchanged_mapping_writes(db,tmp_path,monkeypatch):
    from sqlalchemy import event

    from app import archive_linkedin

    snapshot=local_export(tmp_path,assets=201)
    first=archive(db,snapshot,tmp_path,tmp_path/'media')
    assert first['asset_urls']==201 and first['registered_files']==1

    def unexpected(*args):
        raise AssertionError('Unchanged cached assets reacquired heavy disk permission')

    monkeypatch.setattr(archive_linkedin,'heavy_io',unexpected)
    statements=[]
    def record(connection,cursor,statement,parameters,context,executemany):
        statements.append(statement)
    event.listen(db.bind.engine,'before_cursor_execute',record)
    try:
        result=archive(db,snapshot,tmp_path,tmp_path/'media')
    finally:
        event.remove(db.bind.engine,'before_cursor_execute',record)
    assert result['asset_urls']==201
    assert not any('INSERT INTO remote_media_sources' in sql for sql in statements)


def test_changed_cached_linkedin_bytes_are_rehashed_before_linking(db,tmp_path):
    import os

    snapshot=local_export(tmp_path)
    archive(db,snapshot,tmp_path,tmp_path/'media')
    path=tmp_path/'assets'/snapshot['local_assets'][0]['local_path']
    stamp=path.stat()
    data=bytearray(path.read_bytes());data[-1]^=1;path.write_bytes(data)
    os.utime(path,ns=(stamp.st_atime_ns,stamp.st_mtime_ns+1000000000))
    with pytest.raises(ValueError,match='hash mismatch'):
        archive(db,snapshot,tmp_path,tmp_path/'media')


def test_media_only_preserves_metadata_cursor(db,tmp_path,monkeypatch):
    import json
    from contextlib import nullcontext

    from app import archive_linkedin, sync_linkedin
    path=tmp_path/'snapshot.json';path.write_text(json.dumps({'profiles':[],'local_assets':[]}))
    monkeypatch.setattr(sync_linkedin,'SessionLocal',lambda:nullcontext(db))
    def no_metadata(*args,**kwargs):raise AssertionError('Media-only must not replay metadata')
    monkeypatch.setattr(sync_linkedin,'load_linkedin',no_metadata)
    calls=[]
    def media(db,snapshot,root,media_root,**_kwargs):
        calls.append(root)
        return {'missing_files':0}
    monkeypatch.setattr(archive_linkedin,'archive',media)
    sync_linkedin.sync(path,media_only=True)
    assert calls==[tmp_path]
    assert db.execute(text("SELECT count(*) FROM source_cursors WHERE source='linkedin'")).scalar_one()==0


def local_export(tmp_path, *, assets=1, posts=1):
    out=io.BytesIO();Image.new('RGB',(12,8),'blue').save(out,format='PNG')
    data=out.getvalue();sha=hashlib.sha256(data).hexdigest()
    (tmp_path/'assets').mkdir();(tmp_path/'assets'/f'{sha}.png').write_bytes(data)
    urls=[f'https://media.licdn.com/checkpoint-{i}.png' for i in range(assets)]
    snapshot={'profiles':[{'provider_user_id':'checkpoint-account','profile':{'name':'Checkpoint'},
        'posts':[{'provider_post_id':f'urn:li:activity:checkpoint-{i}','text':'photo','image_urls':[urls[0]]}
                 for i in range(posts)]}],
        'local_assets':[{'source_url':url,'content_hash':sha,'local_path':f'{sha}.png',
                         'byte_count':len(data),'media_type':'image/png'} for url in urls]}
    return snapshot


def test_linkedin_shutdown_keeps_durable_assets_and_does_not_advance_cursor(db,source_engine,tmp_path,monkeypatch):
    from app import archive_linkedin, runtime_status, sync_linkedin

    stop=threading.Event();monkeypatch.setattr(runtime_status,'STOP',stop)
    snapshot=local_export(tmp_path,assets=2)
    db.execute(text("INSERT INTO platform_migration_state(provider,enabled) VALUES('linkedin',true)"));db.commit()
    load_linkedin(db,tmp_path,{},export_snapshot=snapshot)
    db.execute(text("""INSERT INTO source_cursors(source,stream,cursor_value,batch_id)
        SELECT 'linkedin','collector_export','previous-export',id FROM import_batches
        WHERE source='linkedin' ORDER BY completed_at DESC LIMIT 1"""));db.commit()
    path=tmp_path/'snapshot.json';path.write_text(json.dumps(snapshot))
    monkeypatch.setattr(sync_linkedin,'SessionLocal',lambda:nullcontext(db))
    def local_archive(session,export,root,_media_root,**kwargs):
        return archive(session,export,root,tmp_path/'media',**kwargs)
    monkeypatch.setattr(archive_linkedin,'archive',local_archive)
    register=archive_linkedin.register_file
    def after_first_asset(*args):
        media=register(*args);stop.set();return media
    monkeypatch.setattr(archive_linkedin,'register_file',after_first_asset)
    with pytest.raises(RuntimeError,match='committed checkpoint'):
        sync_linkedin.sync(path)
    assert db.execute(text("SELECT cursor_value FROM source_cursors WHERE source='linkedin' AND stream='collector_export'")).scalar_one()=='previous-export'
    assert db.execute(text('SELECT count(*) FROM media_assets')).scalar_one()==1
    assert db.execute(text('SELECT count(*) FROM remote_media_sources')).scalar_one()==1
    monkeypatch.setattr(archive_linkedin,'register_file',register);stop.clear()
    assert sync_linkedin.sync(path)=='success'
    assert db.execute(text("SELECT cursor_value FROM source_cursors WHERE source='linkedin' AND stream='collector_export'")).scalar_one()!='previous-export'
    assert db.execute(text('SELECT count(*) FROM media_assets')).scalar_one()==1
    assert db.execute(text('SELECT count(*) FROM remote_media_sources')).scalar_one()==2
    assert db.execute(text('SELECT count(*) FROM social_posts')).scalar_one()==1


def test_linkedin_shutdown_while_waiting_for_disk_skips_the_next_asset(db,tmp_path,monkeypatch):
    from app import archive_linkedin

    snapshot=local_export(tmp_path);stop=threading.Event()
    @contextmanager
    def acquire_after_shutdown(_name):
        stop.set()
        yield
    monkeypatch.setattr(archive_linkedin,'heavy_io',acquire_after_shutdown)
    def unexpected_registration(*_):
        pytest.fail('Shutdown was requested before disk permission was acquired')
    monkeypatch.setattr(archive_linkedin,'register_file',unexpected_registration)
    with pytest.raises(RuntimeError,match='committed checkpoint'):
        archive(db,snapshot,tmp_path,tmp_path/'media',stop_requested=stop.is_set)
    assert db.execute(text('SELECT count(*) FROM media_assets')).scalar_one()==0


def test_linkedin_shutdown_flushes_pending_post_links_before_checkpoint(db,source_engine,tmp_path,monkeypatch):
    from app import archive_linkedin

    snapshot=local_export(tmp_path,posts=2);stop=threading.Event()
    db.execute(text("INSERT INTO platform_migration_state(provider,enabled) VALUES('linkedin',true)"));db.commit()
    load_linkedin(db,tmp_path,{},export_snapshot=snapshot)
    link=archive_linkedin.link
    def after_first_post(*args,**kwargs):
        result=link(*args,**kwargs)
        if kwargs.get('entity_type')=='social_post':stop.set()
        return result
    monkeypatch.setattr(archive_linkedin,'link',after_first_post)
    with pytest.raises(RuntimeError,match='committed checkpoint'):
        archive(db,snapshot,tmp_path,tmp_path/'media',stop_requested=stop.is_set)
    assert db.execute(text("SELECT count(*) FROM media_links WHERE entity_type='social_post'")).scalar_one()==1
    with source_engine.connect() as source:
        assert source.execute(text('SELECT count(*) FROM media_links')).scalar_one()==1
    monkeypatch.setattr(archive_linkedin,'link',link);stop.clear()
    archive(db,snapshot,tmp_path,tmp_path/'media',stop_requested=stop.is_set)
    assert db.execute(text("SELECT count(*) FROM media_links WHERE entity_type='social_post'")).scalar_one()==2
    with source_engine.connect() as source:
        assert source.execute(text('SELECT count(*) FROM media_links')).scalar_one()==2
