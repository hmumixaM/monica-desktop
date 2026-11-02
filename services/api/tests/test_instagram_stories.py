import importlib.util
import json
from pathlib import Path

import pytest
from sqlalchemy import text
from test_account_links import source_engine as source_engine  # noqa: PLC0414
from test_media_integration import db as db  # noqa: PLC0414

from app.sync_instagram_stories import cycle, read_manifest


def fixture_archive(tmp_path, video=False):
    root=tmp_path/'stories';owner=root/'somebody';owner.mkdir(parents=True)
    files=['item.jpg','item.json']
    (owner/'item.jpg').write_bytes(b'\xff\xd8\xffphoto')
    (owner/'item.json').write_text('{}')
    if video:
        files.append('item.mp4');(owner/'item.mp4').write_bytes(b'\x00\x00\x00\x18ftypisomvideo')
    doc={'schema_version':1,'mediaid':123,'owner_id':456,'owner_username':'somebody',
         'captured_at':1700000000,'taken_at':'2023-11-14T00:00:00Z','expiring_at':'2023-11-15T00:00:00Z',
         'is_video':video,'caption':'Story','files':files,'raw_metadata':{'display_url':'https://example.test/photo','video_url':'https://example.test/video'}}
    manifest=owner/'story-123.manifest.json';manifest.write_text(json.dumps(doc))
    return root,manifest,doc


def test_story_archive_idempotency_retry_and_source_account_without_new_person(db,source_engine,tmp_path):
    root,manifest,doc=fixture_archive(tmp_path,True)
    db.execute(text("INSERT INTO platform_migration_state(provider,enabled) VALUES('instagram',true)"));db.commit()
    state=tmp_path/'checkpoint.json';media=tmp_path/'media'
    first=cycle(db,root,media,state)
    assert first['inserted']==1 and first['added_media_links']==2 and first['failed']==0
    assert db.execute(text('SELECT count(*) FROM profiles')).scalar_one()==0
    assert db.execute(text('SELECT count(*) FROM social_posts')).scalar_one()==0
    story=db.execute(text('SELECT * FROM social_stories')).mappings().one()
    assert story['profile_id'] is None and story['source_account_id'] is not None
    assert story['coverage_status']=='archived'
    with source_engine.connect() as c:
        assert c.execute(text("SELECT count(*) FROM objects WHERE object_kind='story'")).scalar_one()==1
        assert c.execute(text('SELECT count(*) FROM media_links')).scalar_one()==2
    second=cycle(db,root,media,state)
    assert second['skipped']==1 and second['inserted']==0
    state.unlink()  # Simulate crash after DB commit and before checkpoint.
    third=cycle(db,root,media,state)
    assert third['inserted']==0 and third['changed']==0 and third['added_media_links']==0
    assert db.execute(text('SELECT count(*) FROM social_story_revisions')).scalar_one()==1
    assert db.execute(text("SELECT count(*) FROM jobs WHERE job_type='media.download'")).scalar_one()==0
    # An incomplete transfer must not mark the new manifest as processed.
    doc['files'].append('missing.mp4');manifest.write_text(json.dumps(doc))
    assert cycle(db,root,media,state)['failed']==1


def test_story_manifest_path_and_partial_file_validation(tmp_path):
    _,manifest,doc=fixture_archive(tmp_path)
    doc['files']=['../escape.jpg'];manifest.write_text(json.dumps(doc))
    with pytest.raises(ValueError):read_manifest(manifest)
    doc['files']=['missing.jpg'];manifest.write_text(json.dumps(doc))
    with pytest.raises(ValueError):read_manifest(manifest)


def test_publisher_only_includes_complete_manifests(tmp_path):
    path=Path(__file__).parents[3]/'infra/instagram/publish-stories.py'
    spec=importlib.util.spec_from_file_location('publish_stories',path);module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    root,manifest,doc=fixture_archive(tmp_path)
    files,incomplete=module.inventory(root)
    assert len(files)==3 and incomplete==0
    doc['files'].append('missing.mp4');manifest.write_text(json.dumps(doc))
    files,incomplete=module.inventory(root)
    assert files=={} and incomplete==1


def test_story_location_tags_preserve_coordinates_and_ignore_sticker_positions():
    from app.sync_instagram_stories import story_locations
    raw={'iphone_struct':{'story_locations':[{'x':0.2,'y':0.4,'location':{'pk':1,'name':'Restaurant Daniel','lat':40.76678,'lng':-73.96757}}]}}
    places=story_locations(raw)
    assert places[0]['latitude']==40.76678 and places[0]['longitude']==-73.96757
    assert places[0]['coordinate_source']=='instagram_place_tag'
    assert 'x' not in places[0] and 'y' not in places[0]
    assert story_locations({'tappable_objects':[{'x':.2,'y':.4}]})==[]


def test_story_place_is_on_profile_and_global_maps(db,source_engine,tmp_path):
    from uuid import uuid4

    from app.location_atlas import atlas
    from app.platform_migrate import register_accounts
    from app.routers.locations import profile_locations
    pid=uuid4()
    db.execute(text("INSERT INTO profiles(id,profile_type,display_name) VALUES(:id,'person','Story owner')"),{'id':pid})
    db.execute(text("INSERT INTO identities(provider,external_id,profile_id) VALUES('instagram','456',:id)"),{'id':pid})
    register_accounts(db,'instagram')
    db.execute(text("INSERT INTO platform_migration_state(provider,enabled) VALUES('instagram',true)"));db.commit()
    root,path,doc=fixture_archive(tmp_path)
    doc['raw_metadata']['iphone_struct']={'story_locations':[{'location':{'name':'Restaurant Daniel','lat':40.76678,'lng':-73.96757}}]}
    path.write_text(json.dumps(doc))
    cycle(db,root,tmp_path/'media',tmp_path/'checkpoint.json')
    point=profile_locations(pid,db,{})['points'][0]
    assert point['label']=='Restaurant Daniel' and point['latitude']==40.76678
    assert 'event=' in point['events'][0]['url']
    result=atlas(db,provider='instagram',year=2023,kind='post')
    assert result['located_records']==1 and result['points'][0]['longitude']==-73.96757
    assert 'Story' in result['points'][0]['events'][0]['text']
    assert atlas(db,provider='instagram',year=2024,kind='post')['located_records']==0


def test_live_highlight_archive_survives_versioned_importer_upgrade(db, source_engine, tmp_path):
    from app.sync_instagram_stories import highlight_cycle, read_highlight_manifest
    root, manifest, doc = fixture_archive(tmp_path, True)
    folder = root/'somebody'/'789'
    folder.mkdir()
    for path in list(manifest.parent.iterdir()):
        if path != folder:
            path.rename(folder/path.name)
    manifest = folder/manifest.name
    doc.update(archive_kind='highlight', highlight_id='789', highlight_title='Saved stories')
    manifest.write_text(json.dumps(doc))
    read_highlight_manifest(manifest)
    db.execute(text("INSERT INTO platform_migration_state(provider,enabled) VALUES('instagram',true)"))
    db.commit()
    checkpoint = tmp_path/'highlight-checkpoint.json'
    first = highlight_cycle(db, root, tmp_path/'media', checkpoint)
    assert first['inserted'] == 1 and first['added_media_links'] == 2
    assert db.execute(text('SELECT external_id FROM social_stories')).scalar_one() == 'highlight:789:123'
    assert highlight_cycle(db, root, tmp_path/'media', checkpoint)['skipped'] == 1
    checkpoint.unlink()
    replay = highlight_cycle(db, root, tmp_path/'media', checkpoint)
    assert replay['inserted'] == replay['changed'] == replay['added_media_links'] == 0
    doc['files'].append('missing.jpg')
    manifest.write_text(json.dumps(doc))
    assert highlight_cycle(db, root, tmp_path/'media', checkpoint)['failed'] == 1
