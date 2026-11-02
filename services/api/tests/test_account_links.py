import os
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from test_media_integration import db as db  # noqa: PLC0414 -- shared pytest fixture

from app.account_links import set_link
from app.imports import project_batch
from app.platform_migrate import register_accounts
from app.repositories import append_field_revision, list_field_history
from app.schemas import ImportBatchRequest


def test_message_cursor_completion_uses_exact_time_and_id():
    from app.platform_migrate import message_cursor_complete

    message_id=uuid4()
    watermark=SimpleNamespace(
        occurred_at=datetime(2026,9,20,tzinfo=UTC),
        message_id=message_id,
    )
    assert message_cursor_complete(
        {'at':'2026-09-20T00:00:00+00:00','id':str(message_id)},
        watermark,
    )
    assert not message_cursor_complete(
        {'at':'2026-09-19T23:59:59+00:00','id':str(uuid4())},
        watermark,
    )
    assert not message_cursor_complete('invalid',watermark)


def test_immutable_message_storage_omits_synthetic_history(source_engine):
    from app.platform_store import ensure_account, observe_immutable_many

    with source_engine.begin() as source:
        account=ensure_account(source,'wechat','immutable-room')
        records=[{'account':account,'kind':'message','external_id':f'message-{n}',
                  'value':{'text':f'Message {n}'},'occurred_at':datetime(2026,9,20,n,tzinfo=UTC)}
                 for n in range(2)]
        observe_immutable_many(source,records)
        observe_immutable_many(source,records)
        assert source.execute(text("SELECT count(*) FROM objects WHERE object_kind='message'")).scalar_one()==2
        assert source.execute(text('SELECT count(*) FROM payloads')).scalar_one()==2
        assert source.execute(text('SELECT count(*) FROM revisions')).scalar_one()==0
        assert source.execute(text('SELECT count(*) FROM observations')).scalar_one()==0
        with pytest.raises(ValueError,match='changed or collided'):
            observe_immutable_many(source,[{**records[0],'value':{'text':'Changed'}}])


@pytest.fixture
def source_engine(monkeypatch):
    url=make_url(os.environ['TEST_DATABASE_URL']);name='account_test_'+uuid4().hex
    admin=create_engine(url.set(database='postgres'),isolation_level='AUTOCOMMIT')
    with admin.connect() as c:c.exec_driver_sql(f'CREATE DATABASE "{name}"')
    engine=create_engine(url.set(database=name))
    with engine.begin() as c:c.execute(text((Path(__file__).parents[1]/'app/platform_schema.sql').read_text()))
    monkeypatch.setattr('app.platform_store.platform_engine',lambda _:engine)
    monkeypatch.setattr('app.platform_migrate.platform_engine',lambda _:engine)
    monkeypatch.setattr('app.source_projection.platform_engine',lambda _:engine)
    monkeypatch.setattr('app.source_verification.platform_engine',lambda _:engine)
    yield engine
    engine.dispose()
    with admin.connect() as c:c.exec_driver_sql(f'DROP DATABASE "{name}" WITH (FORCE)')
    admin.dispose()


def test_ambiguous_source_account_can_unlink_but_cannot_move(db, source_engine):
    person, other = uuid4(), uuid4()
    db.execute(
        text(
            "INSERT INTO profiles(id,profile_type,display_name) "
            "VALUES(:id,'person','Pending source'),(:other,'person','Someone else')"
        ),
        {"id": person, "other": other},
    )
    db.execute(
        text(
            "INSERT INTO identities(profile_id,provider,external_id) "
            "VALUES(:id,'wechat','wxid_pending')"
        ),
        {"id": person},
    )
    register_accounts(db, "wechat")
    account = db.execute(text("SELECT id FROM source_accounts")).scalar_one()
    assert db.execute(text("SELECT migrated_at FROM source_accounts WHERE id=:id"), {"id": account}).scalar_one() is None
    tag = db.execute(text("INSERT INTO tags(name) VALUES('Imported label') RETURNING id")).scalar_one()
    db.execute(
        text("INSERT INTO tag_memberships(tag_id,profile_id,source_type) VALUES(:tag,:id,'wechat')"),
        {"tag": tag, "id": person},
    )

    with pytest.raises(HTTPException, match="platform tags without a source account"):
        set_link(db, account, other, expected_profile_id=person)
    result = set_link(db, account, None, expected_profile_id=person)

    assert result["changed"] is True
    assert db.execute(
        text("SELECT count(*) FROM source_account_links WHERE account_id=:id"),
        {"id": account},
    ).scalar_one() == 0
    # Detached from the ambiguous Profile, the unmigrated account links normally.
    assert set_link(db, account, other, expected_profile_id=None)["changed"] is True


def test_unlink_relink_preserves_source_and_manual_records_and_unlinked_aba(db,source_engine):
    original,target,group=uuid4(),uuid4(),uuid4()
    for pid,kind in [(original,'person'),(target,'person'),(group,'group')]:
        db.execute(text("INSERT INTO profiles(id,profile_type,display_name,summary) VALUES(:id,:type,'Same display name','Handwritten summary')"),{'id':pid,'type':kind})
    db.execute(text("INSERT INTO identities(profile_id,provider,external_id) VALUES(:id,'linkedin','actual-account')"),{'id':original})
    register_accounts(db,'linkedin')
    aid=db.execute(text('SELECT id FROM source_accounts')).scalar_one()
    db.execute(text('UPDATE source_accounts SET migrated_at=now()'))
    db.execute(text("INSERT INTO platform_migration_state(provider,enabled) VALUES('linkedin',true)"))
    append_field_revision(db,profile_id=original,field_key='note',value='Personal note',source_type='manual',operation='manual_edit')
    def submit(batch,value,post=False):
        content={'text':value} if post else {'fields':{'linkedin.about':value,'linkedin.location':value},'education':[{'school':'School','degree':value,'id':'school-1'}]}
        request=ImportBatchRequest(source='linkedin',stream='posts' if post else 'profiles',batch_id=batch,idempotency_key=batch,observed_at=datetime.now(UTC),
            records=[{'external_id':'post-1' if post else 'actual-account','profile_id':original,'entity_type':'post' if post else 'profile','content':{**content,'username':'actual-account'}}])
        project_batch(db,request)
    submit('first','A');submit('post','Original post',True)
    db.commit()
    assert db.execute(text("SELECT value FROM profile_metric_values WHERE field_key='linkedin.location'")).scalar_one()=='A'
    with source_engine.connect() as c:
        before={table:c.execute(text(f'SELECT count(*) FROM {table}')).scalar_one() for table in ('objects','payloads','revisions','observations')}
    set_link(db,aid,None,expected_profile_id=original);db.commit()
    assert db.execute(text('SELECT profile_id FROM social_posts')).scalar_one() is None
    assert db.execute(text("SELECT count(*) FROM profile_field_current WHERE profile_id=:id AND field_key='linkedin.about'"),{'id':original}).scalar_one()==0
    assert db.execute(text('SELECT count(*) FROM profile_structured_current WHERE profile_id=:id'),{'id':original}).scalar_one()==0
    assert db.execute(text('SELECT count(*) FROM profile_metric_values')).scalar_one()==0
    assert [r['field_key'] for r in list_field_history(db,original)]==['note']
    assert db.execute(text('SELECT count(*) FROM profiles WHERE archived_at IS NOT NULL')).scalar_one()==0
    with source_engine.connect() as c:
        assert before=={table:c.execute(text(f'SELECT count(*) FROM {table}')).scalar_one() for table in before}
    # Incoming records may still carry the former Profile ID. It must never
    # recreate the link, skip a transition, or copy unchanged values.
    submit('second','B');submit('third','A');submit('unchanged','A');submit('post-change','Edited while unlinked',True)
    assert db.execute(text('SELECT count(*) FROM profile_metric_values')).scalar_one()==0
    assert db.execute(text('SELECT count(*) FROM source_account_links')).scalar_one()==0
    assert db.execute(text('SELECT profile_id FROM social_posts')).scalar_one() is None
    assert db.execute(text("SELECT count(*) FROM profile_field_revisions WHERE field_key='linkedin.about'")).scalar_one()==3
    set_link(db,aid,target,expected_profile_id=None);db.commit()
    metric=db.execute(text("SELECT profile_id,value FROM profile_metric_values WHERE field_key='linkedin.location'")).one()
    assert metric.profile_id==target and metric.value=='A'
    assert db.execute(text('SELECT profile_id FROM social_posts')).scalar_one()==target
    assert len([r for r in list_field_history(db,target) if r['field_key']=='linkedin.about'])==3
    assert db.execute(text('SELECT count(*) FROM profile_structured_current WHERE profile_id=:id'),{'id':target}).scalar_one()==1
    assert db.execute(text('SELECT summary FROM profiles WHERE id=:id'),{'id':original}).scalar_one()=='Handwritten summary'
    assert db.execute(text('SELECT count(*) FROM profiles')).scalar_one()==3
    with pytest.raises(HTTPException) as conflict:set_link(db,aid,original,expected_profile_id=None)
    assert conflict.value.status_code==409
    with pytest.raises(HTTPException) as mismatch:set_link(db,aid,group,expected_profile_id=target)
    assert mismatch.value.status_code==422
    set_link(db,aid,original,expected_profile_id=target);db.commit()
    assert db.execute(text('SELECT count(*) FROM profile_field_current WHERE profile_id=:id'),{'id':target}).scalar_one()==0
    assert db.execute(text('SELECT profile_id FROM social_posts')).scalar_one()==original


def test_new_source_account_waits_for_link_and_media_follows_account(db,source_engine,tmp_path):
    import hashlib

    from app.remote_media import materialize_image
    db.execute(text("INSERT INTO platform_migration_state(provider,enabled) VALUES('instagram',true)"))
    request=ImportBatchRequest(source='instagram',stream='profiles',batch_id='new',idempotency_key='new',observed_at=datetime.now(UTC),
        records=[{'external_id':'real_ig_account','content':{'fields':{'instagram.biography':'A new biography'}}}])
    project_batch(db,request);db.commit()
    assert db.execute(text('SELECT count(*) FROM profiles')).scalar_one()==0
    aid=db.execute(text('SELECT id FROM source_accounts')).scalar_one()
    assert db.execute(text('SELECT count(*) FROM source_account_links')).scalar_one()==0
    image=tmp_path/'test-image.jpg';image.write_bytes(b'image bytes')
    digest=hashlib.sha256(image.read_bytes()).hexdigest()
    media=materialize_image(db,{'entity_type':'source_account','entity_id':str(aid),'role':'instagram_avatar',
        'url':'https://cdninstagram.com/test.jpg','download_key':'first'},tmp_path,downloaded=(digest,str(image),image.stat().st_size,'image/jpeg'))
    original,target=uuid4(),uuid4()
    for pid in (original,target):db.execute(text("INSERT INTO profiles(id,profile_type,display_name) VALUES(:id,'person','Same name')"),{'id':pid})
    set_link(db,aid,original,expected_profile_id=None)
    assert str(db.execute(text('SELECT avatar_media_id FROM profiles WHERE id=:id'),{'id':original}).scalar_one())==media
    set_link(db,aid,target,expected_profile_id=original)
    assert db.execute(text('SELECT avatar_media_id FROM profiles WHERE id=:id'),{'id':original}).scalar_one() is None
    assert str(db.execute(text('SELECT avatar_media_id FROM profiles WHERE id=:id'),{'id':target}).scalar_one())==media
    assert db.execute(text('SELECT count(*) FROM media_assets')).scalar_one()==1
    with source_engine.connect() as c:
        assert c.execute(text('SELECT count(*) FROM media')).scalar_one()==1
        assert c.execute(text('SELECT count(*) FROM media_links')).scalar_one()==1


def test_source_batch_commits_once_and_replays_after_crm_failure(db,source_engine,monkeypatch):
    from sqlalchemy import event

    from app import imports

    person=uuid4()
    db.execute(text("INSERT INTO profiles(id,profile_type,display_name) VALUES(:id,'person','Recorded person')"),{'id':person})
    db.execute(text("INSERT INTO identities(profile_id,provider,external_id) VALUES(:id,'instagram','actual_account')"),{'id':person})
    register_accounts(db,'instagram')
    db.execute(text("INSERT INTO platform_migration_state(provider,enabled) VALUES('instagram',true)"));db.commit()
    commits=[]
    def committed(connection):commits.append(True)
    event.listen(source_engine,'commit',committed)
    records=[{'external_id':f'post-{n}','source_account_external_id':'actual_account','entity_type':'post',
        'content':{'text':f'Observed post {n}'}} for n in range(4)]
    # Identical records in a delivery are still one stable source observation.
    records.append(records[0])
    request=ImportBatchRequest(source='instagram',stream='posts',batch_id='atomic-source',
        idempotency_key='atomic-source',observed_at=datetime.now(UTC),records=records)
    project=imports._project_social_record
    def fail_projection(*args):raise RuntimeError('CRM projection interrupted after source commit')
    monkeypatch.setattr(imports,'_project_social_record',fail_projection)
    with pytest.raises(RuntimeError,match='interrupted'):project_batch(db,request)
    db.rollback()
    assert len(commits)==1
    with source_engine.connect() as source:
        assert source.execute(text('SELECT count(*) FROM objects')).scalar_one()==4
        assert source.execute(text('SELECT count(*) FROM revisions')).scalar_one()==4
    assert db.execute(text('SELECT count(*) FROM social_posts')).scalar_one()==0
    monkeypatch.setattr(imports,'_project_social_record',project)
    project_batch(db,request);db.commit()
    assert len(commits)==2
    assert db.execute(text('SELECT count(*) FROM social_posts')).scalar_one()==4
    project_batch(db,request);db.commit()
    assert len(commits)==2  # A complete retry does not reopen the source transaction.
    with source_engine.connect() as source:
        assert source.execute(text('SELECT count(*) FROM revisions')).scalar_one()==4
        assert source.execute(text('SELECT count(*) FROM observations')).scalar_one()==4
    # Python considers True == 1, but these are distinct source JSON values.
    # Reject an event collision before committing either payload/projection.
    conflicting=request.model_copy(update={'batch_id':'collision','idempotency_key':'collision',
        'records':[request.records[0].model_copy(update={'external_id':'collision','content':{'flag':value}})
                   for value in (True,1)]})
    with pytest.raises(ValueError,match='reused with different content'):project_batch(db,conflicting)
    db.rollback()
    assert len(commits)==2
    with source_engine.connect() as source:
        assert source.execute(text('SELECT count(*) FROM revisions')).scalar_one()==4
    event.remove(source_engine,'commit',committed)


def test_ordered_field_transitions_in_one_source_batch_are_not_collapsed(db,source_engine):
    person=uuid4()
    db.execute(text("INSERT INTO profiles(id,profile_type,display_name) VALUES(:id,'person','Same name')"),{'id':person})
    db.execute(text("INSERT INTO identities(profile_id,provider,external_id) VALUES(:id,'linkedin','actual-account')"),{'id':person})
    register_accounts(db,'linkedin')
    db.execute(text("INSERT INTO platform_migration_state(provider,enabled) VALUES('linkedin',true)"))
    request=ImportBatchRequest(source='linkedin',stream='profiles',batch_id='ordered',idempotency_key='ordered',
        observed_at=datetime.now(UTC),records=[{'external_id':f'observation-{n}',
            'source_account_external_id':'actual-account','content':{'fields':{'linkedin.about':value}}}
            for n,value in enumerate(('A','B','A','A'))])
    project_batch(db,request);db.commit()
    with source_engine.connect() as source:
        values=source.execute(text("SELECT p.value FROM revisions r JOIN objects o ON o.id=r.object_id JOIN payloads p ON p.sha256=r.content_hash WHERE o.object_kind='field' ORDER BY r.revision_number")).scalars().all()
        assert values==['A','B','A']
    history=[r['value'] for r in list_field_history(db,person) if r['field_key']=='linkedin.about']
    assert len(history)==3 and history.count('A')==2 and history.count('B')==1
    assert db.execute(text('SELECT count(*) FROM profiles')).scalar_one()==1


def test_archive_batch_keeps_post_media_on_unlinked_source_accounts(db,source_engine,tmp_path):
    from app.archive_media import link, register_file
    from app.platform_store import object_id
    from app.remote_media import retain_post_media_batch

    db.execute(text("INSERT INTO platform_migration_state(provider,enabled) VALUES('wechat',true)"))
    project_batch(db,ImportBatchRequest(source='wechat',stream='moments',batch_id='photos',idempotency_key='photos',
        observed_at=datetime.now(UTC),records=[{'external_id':'post','source_account_external_id':'wxid_actual',
            'entity_type':'post','content':{'text':'Independent archive'}}]));db.commit()
    post,account=db.execute(text('SELECT id,source_account_id FROM social_posts')).one()
    pending=[]
    for n in range(2):
        path=tmp_path/f'{n}.jpg';path.write_bytes(b'\xff\xd8\xff'+bytes([n]))
        media=register_file(db,path,tmp_path/'media','wechat');role=f'media-{n+1}'
        link(db,media,post,role,retain=False)
        pending.append({'post':str(post),'media':str(media),'role':role})
    retain_post_media_batch(db,pending)
    db.rollback()  # Source committed, CRM registration/linking did not.
    pending=[]
    for n in range(2):
        media=register_file(db,tmp_path/f'{n}.jpg',tmp_path/'media','wechat');role=f'media-{n+1}'
        link(db,media,post,role,retain=False)
        pending.append({'post':str(post),'media':str(media),'role':role})
    retain_post_media_batch(db,pending);db.commit()
    assert db.execute(text('SELECT count(*) FROM profiles')).scalar_one()==0
    assert db.execute(text('SELECT count(*) FROM media_links')).scalar_one()==2
    with source_engine.connect() as source:
        assert source.execute(text('SELECT count(*) FROM media')).scalar_one()==2
        assert source.execute(text('SELECT count(*) FROM media_links WHERE object_id=:id'),
            {'id':object_id(account,'post','post')}).scalar_one()==2


def test_group_membership_survives_unlink_of_one_of_two_person_accounts(db,source_engine):
    person,group=uuid4(),uuid4()
    for pid,kind in [(person,'person'),(group,'group')]:
        db.execute(text("INSERT INTO profiles(id,profile_type,display_name) VALUES(:id,:kind,'Account test')"),{'id':pid,'kind':kind})
    for ext,pid,provider in [('wxid_a',person,'wechat'),('wxid_b',person,'wechat'),('123@chatroom',group,'wechat_group')]:
        db.execute(text('INSERT INTO identities(external_id,profile_id,provider) VALUES(:ext,:pid,:provider)'),{'ext':ext,'pid':pid,'provider':provider})
    register_accounts(db,'wechat')
    accounts=dict(db.execute(text('SELECT external_id,id FROM source_accounts')).all())
    db.execute(text('UPDATE source_accounts SET migrated_at=now()'))
    for ext in ('wxid_a','wxid_b'):
        db.execute(text('INSERT INTO source_group_memberships(group_account_id,person_account_id) VALUES(:g,:p)'),{'g':accounts['123@chatroom'],'p':accounts[ext]})
    db.execute(text('INSERT INTO group_memberships(group_profile_id,person_profile_id) VALUES(:g,:p)'),{'g':group,'p':person})
    set_link(db,accounts['wxid_a'],None,expected_profile_id=person)
    assert db.execute(text('SELECT count(*) FROM group_memberships')).scalar_one()==1
    set_link(db,accounts['wxid_b'],None,expected_profile_id=person)
    assert db.execute(text('SELECT count(*) FROM group_memberships')).scalar_one()==0
    assert db.execute(text('SELECT count(*) FROM source_group_memberships')).scalar_one()==2
    set_link(db,accounts['wxid_b'],person,expected_profile_id=None)
    assert db.execute(text('SELECT count(*) FROM group_memberships')).scalar_one()==1


def test_archive_media_retains_unlinked_account_and_avatar_aba(db,source_engine,tmp_path):
    from app.archive_media import link, register_file
    from app.platform_store import object_id

    db.execute(text("INSERT INTO platform_migration_state(provider,enabled) VALUES('instagram',true)"))
    project_batch(db,ImportBatchRequest(source='instagram',stream='profiles',batch_id='archive',idempotency_key='archive',observed_at=datetime.now(UTC),
        records=[{'external_id':'archive-account','content':{'fields':{'instagram.biography':'Archived'}}}]))
    aid=db.execute(text('SELECT id FROM source_accounts')).scalar_one()
    media_root=tmp_path/'media';media_root.mkdir()
    assets=[]
    for filename,content in [('a.jpg',b'\xff\xd8\xffone'),('b.jpg',b'\xff\xd8\xfftwo')]:
        path=tmp_path/filename;path.write_bytes(content)
        assets.append(register_file(db,path,media_root,'instagram'))
    for asset in [assets[0],assets[1],assets[0],assets[0]]:
        link(db,asset,aid,'instagram_avatar',entity_type='source_account')
    assert db.execute(text('SELECT count(*) FROM profiles')).scalar_one()==0
    assert db.execute(text('SELECT media_id FROM source_account_media')).scalar_one()==assets[0]
    with source_engine.connect() as source:
        oid=object_id(aid,'profile_media','instagram_avatar')
        assert source.execute(text('SELECT count(*) FROM revisions WHERE object_id=:id'),{'id':oid}).scalar_one()==3
        assert source.execute(text('SELECT count(*) FROM media')).scalar_one()==2
    person=uuid4();db.execute(text("INSERT INTO profiles(id,profile_type,display_name) VALUES(:id,'person','Manual person')"),{'id':person})
    set_link(db,aid,person,expected_profile_id=None)
    assert db.execute(text('SELECT avatar_media_id FROM profiles')).scalar_one()==assets[0]


def test_source_migration_keeps_aba_and_uses_explicit_cover_manifest(db,source_engine,tmp_path,monkeypatch):
    import json

    from sqlalchemy import event

    from app.archive_media import link, register_file
    from app.platform_migrate import migrate_fields, migrate_pointers, verify
    from app.platform_store import object_id

    person=uuid4();db.execute(text("INSERT INTO profiles(id,profile_type,display_name) VALUES(:id,'person','Legacy shared profile')"),{'id':person})
    for external in ('wxid_a','wxid_b'):
        db.execute(text("INSERT INTO identities(profile_id,provider,external_id) VALUES(:id,'wechat',:ext)"),{'id':person,'ext':external})
    for value in ['A','B','A']:
        append_field_revision(db,profile_id=person,field_key='wechat.signature',value=value,source_type='wechat',source_record_id='wxid_a',operation='import')
    register_accounts(db,'wechat')
    accounts=dict(db.execute(text('SELECT external_id,id FROM source_accounts')).all())
    report=migrate_fields(db,'wechat')
    assert report['field_revisions_migrated']==3
    assert report['unresolved_field_revision_ids']==[]
    with source_engine.connect() as source:
        oid=object_id(accounts['wxid_a'],'field','wechat.signature')
        assert source.execute(text('SELECT revision_number FROM objects WHERE id=:id'),{'id':oid}).scalar_one()==3
    assert migrate_fields(db,'wechat')['field_revisions_migrated']==0
    file=tmp_path/'cover.jpg';file.write_bytes(b'\xff\xd8\xffcover')
    media_root=tmp_path/'media';media_root.mkdir()
    media=register_file(db,file,media_root,'wechat');link(db,media,person,'cover',entity_type='profile')
    manifest=tmp_path/'manifest.json';manifest.write_text(json.dumps({'wxid_b':{'file':'cover.jpg'}}))
    commits=[]
    def committed(_connection):commits.append(True)
    event.listen(source_engine,'commit',committed)
    # A lost CRM transaction after the independent source commit must be
    # recoverable with the same observations and exact cover ownership.
    execute=db.execute
    def fail_projection(statement,*args,**kwargs):
        if str(statement).startswith('INSERT INTO source_account_media'):raise RuntimeError('projection interrupted')
        return execute(statement,*args,**kwargs)
    monkeypatch.setattr(db,'execute',fail_projection)
    with pytest.raises(RuntimeError,match='projection interrupted'):migrate_pointers(db,'wechat',manifest)
    db.rollback();monkeypatch.setattr(db,'execute',execute)
    assert len(commits)==1
    assert migrate_pointers(db,'wechat',manifest)['source_media_saved']==1
    assert db.execute(text('SELECT account_id FROM source_account_media')).scalar_one()==accounts['wxid_b']
    assert migrate_pointers(db,'wechat',manifest)['source_media_saved']==1
    with source_engine.connect() as source:
        oid=object_id(accounts['wxid_b'],'profile_media','cover')
        assert source.execute(text('SELECT revision_number FROM objects WHERE id=:id'),{'id':oid}).scalar_one()==1
        assert source.execute(text('SELECT count(*) FROM media_links WHERE object_id=:id'),{'id':oid}).scalar_one()==1
    event.remove(source_engine,'commit',committed)
    assert verify(db,'wechat')['errors']==[]
    # Equal totals must not hide a missing source file pointer or a stale value.
    with source_engine.begin() as source:
        source.execute(text('DELETE FROM media_links'))
        source.execute(text("""UPDATE objects o SET current_hash=r.content_hash FROM revisions r
            WHERE o.object_kind='field' AND r.object_id=o.id AND r.revision_number=2"""))
    audit=verify(db,'wechat')
    assert audit['projection_checks']['media']['mismatched']==1
    assert audit['projection_checks']['fields']['mismatched']==1
    with pytest.raises(ValueError,match='mismatch'):verify(db,'wechat',activate=True)
    assert db.execute(text('SELECT count(*) FROM platform_migration_state')).scalar_one()==0


def test_message_source_verification_compares_room_identity_timestamp_and_hash(db,source_engine):
    from sqlalchemy import event

    from app.content import get_or_create_content
    from app.platform_migrate import migrate_messages, verify
    from app.platform_store import object_id

    person=uuid4()
    db.execute(text("INSERT INTO profiles(id,profile_type,display_name) VALUES(:id,'person','Message owner')"),{'id':person})
    db.execute(text("INSERT INTO identities(profile_id,provider,external_id) VALUES(:id,'wechat','wxid_messages')"),{'id':person})
    register_accounts(db,'wechat')
    aid=db.execute(text('SELECT id FROM source_accounts')).scalar_one()
    room=db.execute(text("INSERT INTO conversations(profile_id,conversation_type,external_id) VALUES(:id,'direct','direct:wxid_messages') RETURNING id"),{'id':person}).scalar_one()
    at=datetime(2018,1,2,3,4,5,123456,tzinfo=UTC)
    content,_=get_or_create_content(db,value={'text':'Original message','sender_external_id':'wxid_messages'},content_kind='message')
    mid=db.execute(text("""INSERT INTO message_events(conversation_id,occurred_at,content_object_id,source_record_id,message_type)
        VALUES(:room,:at,:content,'server:1','text') RETURNING id"""),{'room':room,'at':at,'content':content}).scalar_one()
    db.execute(text("INSERT INTO message_search(message_id,occurred_at,conversation_id,body) VALUES(:id,:at,:room,'Original message')"),{'id':mid,'at':at,'room':room})
    db.commit()
    def fail_projection(conn,cursor,statement,params,context,executemany):
        if statement.startswith('UPDATE message_events SET sender_account_id'):raise RuntimeError('sender projection interrupted')
    event.listen(db.bind,'before_cursor_execute',fail_projection)
    try:
        with pytest.raises(RuntimeError,match='sender projection interrupted'):migrate_messages(db)
    finally:
        event.remove(db.bind,'before_cursor_execute',fail_projection);db.rollback()
    with source_engine.connect() as source:
        assert source.execute(text("SELECT count(*) FROM objects WHERE object_kind='message'")).scalar_one()==1
        assert source.execute(text("SELECT count(*) FROM migration_cursors WHERE stream LIKE 'messages:%'")).scalar_one()==0
        revisions=source.execute(text('SELECT count(*) FROM revisions')).scalar_one()
    assert migrate_messages(db)['messages_copied']==1
    assert migrate_messages(db)['messages_copied']==0
    assert db.execute(text('SELECT sender_account_id FROM message_events WHERE id=:id'),{'id':mid}).scalar_one()==aid
    with source_engine.connect() as source:
        assert source.execute(text('SELECT count(*) FROM revisions')).scalar_one()==revisions
        assert source.execute(text('SELECT sum(object_count) FROM migration_cursors')).scalar_one()==1
    check=verify(db,'wechat')['projection_checks']['messages']
    assert check['checked']==1 and check['mismatched']==0
    with source_engine.begin() as source:
        source.execute(text('UPDATE objects SET current_hash=NULL WHERE id=:id'),{'id':object_id(aid,'message','server:1:'+at.isoformat())})
    result=verify(db,'wechat')
    assert result['messages']=={'source':1,'projection':1}
    assert result['projection_checks']['messages']['mismatched']==1


def test_reconcile_backdated_messages_only_adds_missing_and_repairs_interrupted_sender(db,source_engine):
    from sqlalchemy import event

    from app.content import get_or_create_content
    from app.platform_migrate import migrate_messages, reconcile_messages
    from app.platform_store import object_id, observe

    person=uuid4()
    db.execute(text("INSERT INTO profiles(id,profile_type,display_name) VALUES(:id,'person','Archive owner')"),{'id':person})
    db.execute(text("INSERT INTO identities(profile_id,provider,external_id) VALUES(:id,'wechat','wxid_backdate')"),{'id':person})
    register_accounts(db,'wechat')
    aid=db.execute(text('SELECT id FROM source_accounts')).scalar_one()
    room=db.execute(text("INSERT INTO conversations(profile_id,conversation_type,external_id) VALUES(:id,'direct','direct:wxid_backdate') RETURNING id"),{'id':person}).scalar_one()

    def message(record,year,sender='wxid_backdate'):
        at=datetime(year,2,3,4,5,6,tzinfo=UTC)
        value={'text':record,'sender_external_id':sender}
        content,_=get_or_create_content(db,value=value,content_kind='message')
        mid=db.execute(text("""INSERT INTO message_events(conversation_id,occurred_at,content_object_id,source_record_id,message_type)
            VALUES(:room,:at,:content,:record,'text') RETURNING id"""),{'room':room,'at':at,'content':content,'record':record}).scalar_one()
        db.execute(text('INSERT INTO message_search(message_id,occurred_at,conversation_id,body) VALUES(:id,:at,:room,:body)'),
                   {'id':mid,'at':at,'room':room,'body':record})
        db.commit()
        return mid,at,value

    message('newer',2025)
    assert migrate_messages(db)['messages_copied']==1
    mid,at,_=message('arrived-later-but-older',2015,'unknown-backdated-sender')
    assert migrate_messages(db)['messages_copied']==0  # The forward cursor cannot see it.
    audit=reconcile_messages(db)
    assert (audit['messages_checked'],audit['missing_messages'],audit['messages_added'])==(2,1,0)
    assert audit['unregistered_sender_messages']==1
    assert db.execute(text("SELECT count(*) FROM source_accounts WHERE external_id='unknown-backdated-sender'")).scalar_one()==0
    with source_engine.connect() as source:before=source.execute(text('SELECT count(*) FROM revisions')).scalar_one()
    def fail_projection(conn,cursor,statement,params,context,executemany):
        if statement.startswith('UPDATE message_events SET sender_account_id'):raise RuntimeError('sender interrupted')
    event.listen(db.bind,'before_cursor_execute',fail_projection)
    try:
        with pytest.raises(RuntimeError,match='sender interrupted'):reconcile_messages(db,apply=True)
    finally:
        event.remove(db.bind,'before_cursor_execute',fail_projection);db.rollback()
    retry=reconcile_messages(db,apply=True)
    assert (retry['missing_messages'],retry['messages_added'],retry['senders_repaired'])==(0,0,1)
    sender=db.execute(text("SELECT id FROM source_accounts WHERE external_id='unknown-backdated-sender'")).scalar_one()
    assert db.execute(text('SELECT sender_account_id FROM message_events WHERE id=:id'),{'id':mid}).scalar_one()==sender
    with source_engine.connect() as source:assert source.execute(text('SELECT count(*) FROM revisions')).scalar_one()==before
    assert reconcile_messages(db,apply=True)['senders_repaired']==0
    with source_engine.begin() as source:
        observe(source,aid,'message','arrived-later-but-older:'+at.isoformat(),{'text':'New independent source observation'},source_event='new-source-observation')
        changed=source.execute(text('SELECT current_hash FROM objects WHERE id=:id'),{'id':object_id(aid,'message','arrived-later-but-older:'+at.isoformat())}).scalar_one()
    mismatch=reconcile_messages(db,apply=True)
    assert mismatch['different_messages']==1 and mismatch['messages_added']==0
    with source_engine.connect() as source:
        assert source.execute(text('SELECT current_hash FROM objects WHERE id=:id'),{'id':object_id(aid,'message','arrived-later-but-older:'+at.isoformat())}).scalar_one()==changed


def test_source_careers_commit_per_batch_and_retry_keeps_ordered_history(db,source_engine):
    from sqlalchemy import event

    db.execute(text("INSERT INTO platform_migration_state(provider,enabled) VALUES('linkedin',true)"));db.commit()
    commits=[]
    def committed(conn):commits.append(True)
    event.listen(source_engine,'commit',committed)
    at=datetime(2026,1,1,tzinfo=UTC)
    request=ImportBatchRequest(source='linkedin',stream='profiles',batch_id='careers',idempotency_key='careers',observed_at=at,
        records=[{'external_id':'career-batch-owner','content':{'employment':[{'id':str(i),'company':'Work','title':'A'} for i in range(12)]}}])
    try:
        project_batch(db,request)
        assert len(commits)==2  # Source profiles/fields, then all twelve careers.
        db.rollback()  # Source commits survive a failed CRM transaction.
        project_batch(db,request);db.commit()
        with source_engine.connect() as source:
            assert source.execute(text("SELECT count(*) FROM revisions r JOIN objects o ON o.id=r.object_id WHERE o.object_kind='employment'")).scalar_one()==12
        for step,title in enumerate(('B','A','A'),start=1):
            update=request.model_copy(update={'batch_id':f'careers-{step}','idempotency_key':f'careers-{step}',
                'observed_at':at.replace(day=step+1),'records':[request.records[0].model_copy(update={'content':{
                    'employment':[{'id':str(i),'company':'Work','title':title} for i in range(12)]}})]})
            commits.clear();project_batch(db,update);db.commit()
            assert len(commits)==2
        with source_engine.connect() as source:
            history=source.execute(text("""SELECT p.value->>'title' FROM revisions r JOIN objects o ON o.id=r.object_id
                JOIN payloads p ON p.sha256=r.content_hash WHERE o.object_kind='employment' AND o.external_id LIKE :ending
                ORDER BY r.revision_number"""),{'ending':'%:0'}).scalars().all()
            assert history==['A','B','A']
        assert db.execute(text('SELECT count(*) FROM profile_employment_revisions')).scalar_one()==36
        assert 'structured_source_batch' not in db.info
    finally:
        event.remove(source_engine,'commit',committed)


def test_type_collision_repair_appends_only_proven_values_preserving_manual_priority(db,source_engine):
    from app.platform_migrate import migrate_fields, migrate_pointers, verify
    from app.platform_store import observe
    from app.source_verification import repair_field_types

    person=uuid4()
    db.execute(text("INSERT INTO profiles(id,profile_type,display_name) VALUES(:id,'person','Counter owner')"),{'id':person})
    db.execute(text("INSERT INTO identities(profile_id,provider,external_id) VALUES(:id,'wechat','wxid_counter')"),{'id':person})
    append_field_revision(db,profile_id=person,field_key='wechat.counter',value='0',source_type='wechat',source_record_id='wxid_counter',operation='import')
    register_accounts(db,'wechat');aid=db.execute(text('SELECT id FROM source_accounts')).scalar_one()
    migrate_fields(db,'wechat');migrate_pointers(db,'wechat')
    with source_engine.begin() as source:
        observe(source,aid,'field','wechat.counter',0,source_event='source-numeric')
        before=source.execute(text('SELECT count(*) FROM revisions')).scalar_one()
    append_field_revision(db,profile_id=person,field_key='wechat.counter',value='Handwritten 10',source_type='manual',operation='manual_edit')
    db.commit()
    assert repair_field_types(db,'wechat')['typed_collisions']==1
    assert repair_field_types(db,'wechat',True)['appended_revisions']==1
    assert repair_field_types(db,'wechat',True)['typed_collisions']==0
    assert verify(db,'wechat')['projection_checks']['fields']['mismatched']==0
    assert db.execute(text("SELECT current_source_type FROM profile_field_current WHERE field_key='wechat.counter'")).scalar_one()=='manual'
    history=list_field_history(db,person,'wechat.counter')
    assert [r['value'] for r in history]==[0,'Handwritten 10','0']
    with source_engine.connect() as source:
        assert source.execute(text('SELECT count(*) FROM revisions')).scalar_one()==before


def test_legacy_fact_tie_uses_revision_order_and_retains_original_source_evidence(db,source_engine):
    from app.content import get_or_create_content
    from app.platform_migrate import migrate_facts
    from app.platform_store import object_id, observe
    from app.source_verification import repair_legacy_fact_order

    person=uuid4();at=datetime.now(UTC)
    db.execute(text("INSERT INTO profiles(id,profile_type,display_name) VALUES(:id,'person','Career owner')"),{'id':person})
    db.execute(text("INSERT INTO identities(profile_id,provider,external_id) VALUES(:id,'linkedin','career-account')"),{'id':person})
    register_accounts(db,'linkedin');aid=db.execute(text('SELECT id FROM source_accounts')).scalar_one()
    ids=['ffffffff-ffff-ffff-ffff-ffffffffffff','00000000-0000-0000-0000-000000000001']
    for rid,title in zip(ids,['A','B']):
        content,digest=get_or_create_content(db,value={'title':title},content_kind='structured_employment')
        db.execute(text("""INSERT INTO profile_employment_revisions(id,profile_id,source_type,source_record_id,content_object_id,content_hash,observed_at,fact_key)
            VALUES(:id,:person,'linkedin','career-account',:content,:hash,:at,'legacy')"""),{'id':rid,'person':person,'content':content,'hash':digest,'at':at})
    assert migrate_facts(db,'linkedin')['facts_migrated']==2
    oid=object_id(aid,'employment','legacy')
    with source_engine.connect() as source:
        actual=source.execute(text('SELECT p.value FROM objects o JOIN payloads p ON p.sha256=o.current_hash WHERE o.id=:id'),{'id':oid}).scalar_one()
        assert actual=={'title':'B'}
    db.execute(text("INSERT INTO source_account_facts(account_id,fact_type,fact_key,revision_id) VALUES(:id,'employment','legacy',:revision)"),{'id':aid,'revision':ids[-1]});db.commit()
    # Reproduce the old random-UUID tie ordering in an isolated source store.
    with source_engine.begin() as source:
        source.execute(text('DELETE FROM observations WHERE object_id=:id'),{'id':oid})
        source.execute(text('DELETE FROM revisions WHERE object_id=:id'),{'id':oid})
        source.execute(text('DELETE FROM objects WHERE id=:id'),{'id':oid})
        observe(source,aid,'employment','legacy',{'title':'B'},source_event='legacy-fact:'+ids[1],observed_at=at)
        observe(source,aid,'employment','legacy',{'title':'A'},source_event='legacy-fact:'+ids[0],observed_at=at)
    assert repair_legacy_fact_order(db,'linkedin')['tied_legacy_currents']==1
    assert repair_legacy_fact_order(db,'linkedin',True)['source_corrections']==1
    assert repair_legacy_fact_order(db,'linkedin',True)['source_corrections']==0
    assert db.execute(text('SELECT count(*) FROM profile_employment_revisions')).scalar_one()==2
    with source_engine.connect() as source:
        history=source.execute(text('SELECT p.value FROM revisions r JOIN payloads p ON p.sha256=r.content_hash WHERE r.object_id=:id ORDER BY r.revision_number'),{'id':oid}).scalars().all()
        assert history==[{'title':'B'},{'title':'A'},{'title':'B'}]


def test_unlinked_instagram_profile_refresh_and_worker_share_source_store(db,source_engine,tmp_path,monkeypatch):
    import hashlib
    import importlib.util
    import json
    from contextlib import contextmanager

    from app.reconcile_sources import instagram_profiles

    db.execute(text("INSERT INTO platform_migration_state(provider,enabled) VALUES('instagram',true)"))
    root=tmp_path/'profiles';directory=root/'source_user';directory.mkdir(parents=True)
    profile=directory/'profile.json'
    profile.write_text(json.dumps({'userid':123456789,'username':'source_user','biography':'A',
                                   'profile_pic_url':'https://cdninstagram.com/avatar.jpg'}))
    assert instagram_profiles(db,root,apply=True)['changed_profiles']==1
    aid=db.execute(text('SELECT id FROM source_accounts')).scalar_one()
    assert db.execute(text('SELECT count(*) FROM profiles')).scalar_one()==0
    avatar_job=db.execute(text("SELECT payload FROM jobs WHERE job_type='media.download'")).scalar_one()
    assert avatar_job['entity_type']=='source_account' and avatar_job['entity_id']==str(aid)
    assert avatar_job['role']=='instagram_avatar'
    assert instagram_profiles(db,root,apply=True)['changed_profiles']==0
    profile.write_text(json.dumps({'userid':123456789,'username':'source_user','biography':'B'}))
    assert instagram_profiles(db,root,apply=True)['changed_profiles']==1
    person=uuid4();db.execute(text("INSERT INTO profiles(id,profile_type,display_name) VALUES(:id,'person','Manual link')"),{'id':person})
    set_link(db,aid,person,expected_profile_id=None)
    assert db.execute(text('SELECT source_account_id FROM identities')).scalar_one()==aid
    spec=importlib.util.spec_from_file_location('source_media_worker',Path(__file__).parents[2]/'worker'/'worker.py')
    worker=importlib.util.module_from_spec(spec);spec.loader.exec_module(worker)
    class TestEngine:
        @contextmanager
        def connect(self):yield db
        begin=connect
    monkeypatch.setattr(worker,'engine',TestEngine())
    db.execute(text("INSERT INTO jobs(job_type,payload,available_at) VALUES('metrics.refresh','{}',now()-interval '1 day')"))
    monkeypatch.setattr(worker, 'RESOURCE', 'media')
    claimed=worker.claim_job()
    assert claimed['job_type']=='media.download' and claimed['payload']['role']=='instagram_avatar'
    file=tmp_path/'download.jpg';file.write_bytes(b'image bytes')
    digest=hashlib.sha256(file.read_bytes()).hexdigest()
    monkeypatch.setattr(worker,'download_image',lambda *_:(digest,str(file),file.stat().st_size,'image/jpeg'))
    worker.execute_job({**claimed, 'payload':{'url':'https://cdninstagram.com/avatar.jpg',
        'entity_type':'source_account','entity_id':str(aid),'role':'instagram_avatar','download_key':'worker-first'}})
    assert db.execute(text('SELECT avatar_media_id FROM profiles')).scalar_one() is not None
    with source_engine.connect() as source:assert source.execute(text('SELECT count(*) FROM media')).scalar_one()==1


def test_monthly_chat_archive_follows_account_without_message_body_queries(db,source_engine,monkeypatch,tmp_path):
    import json

    from app.chat_months import import_archive, profile_months
    from app.routers.messages import message_month_bounds
    monkeypatch.setattr('app.chat_months.platform_engine',lambda _:source_engine)
    original,target=uuid4(),uuid4()
    for pid in (original,target):db.execute(text("INSERT INTO profiles(id,profile_type,display_name) VALUES(:id,'person','Same name')"),{'id':pid})
    db.execute(text("INSERT INTO identities(profile_id,provider,external_id) VALUES(:id,'wechat','wxid_month_owner')"),{'id':original})
    register_accounts(db,'wechat')
    aid=db.execute(text('SELECT id FROM source_accounts')).scalar_one()
    db.execute(text('UPDATE source_accounts SET migrated_at=now()'))
    db.execute(text("INSERT INTO platform_migration_state(provider,enabled) VALUES('wechat',true)"));db.commit()
    row={'id':1,'wechat_username':'wxid_month_owner','month':'2026-03','sent':3,'received':7,'total':10,
         'first_message_at':1772517600,'last_message_at':1772776800,'snapshot_at':'2026-08-21 00:00:00'}
    archive=tmp_path/'months.json';archive.write_text(json.dumps({'timezone':'America/Chicago','rows':[row]}))
    assert import_archive(db,archive)['changed_source_objects']==1
    assert import_archive(db,archive)['changed_source_objects']==0
    assert profile_months(db,original)['total']==10
    assert db.execute(text('SELECT count(*) FROM message_events')).scalar_one()==0
    set_link(db,aid,target,expected_profile_id=original);db.commit()
    assert profile_months(db,original)['months']==[]
    assert profile_months(db,target)['sent']==3
    set_link(db,aid,None,expected_profile_id=target);db.commit()
    assert profile_months(db,target)['months']==[]
    with source_engine.connect() as c:
        assert c.execute(text("SELECT count(*) FROM objects WHERE object_kind='chat_month'")).scalar_one()==1
        assert c.execute(text('SELECT count(*) FROM revisions')).scalar_one()==1
    start,end=message_month_bounds('2026-03','America/Chicago')
    assert start.astimezone(UTC).hour==6 and end.astimezone(UTC).hour==5
    with pytest.raises(HTTPException):message_month_bounds('2026-13','UTC')
    with pytest.raises(HTTPException):message_month_bounds('2026-03','bad_timezone')


def test_source_calls_money_keep_unknown_amount_and_follow_unlinked_account(db,source_engine,monkeypatch,tmp_path):
    import json

    from app.contact_events import import_archive, records, summary
    monkeypatch.setattr('app.contact_events.platform_engine',lambda _:source_engine)
    original,target=uuid4(),uuid4()
    for pid in (original,target):db.execute(text("INSERT INTO profiles(id,profile_type,display_name) VALUES(:id,'person','Same name')"),{'id':pid})
    db.execute(text("INSERT INTO identities(profile_id,provider,external_id) VALUES(:id,'wechat','wxid_event_owner')"),{'id':original})
    register_accounts(db,'wechat');aid=db.execute(text('SELECT id FROM source_accounts')).scalar_one()
    db.execute(text('UPDATE source_accounts SET migrated_at=now()'));db.execute(text("INSERT INTO platform_migration_state(provider,enabled) VALUES('wechat',true)"));db.commit()
    common={'provider':'wechat','snapshot_at':'2026-08-21 00:00:00','updated_at':'2026-08-21 00:00:00','occurred_at':1700000000}
    calls=[{**common,'id':i,'external_id':f'call-{i}','session_name':'wxid_event_owner','media_type':'voice','direction':'out',
        'media_type_confidence':'high' if i==1 else 'low','status':'completed' if i==1 else 'canceled','duration_seconds':123 if i==1 else None} for i in (1,2)]
    money=[{**common,'id':i,'external_id':f'money-{i}','context_id':'wxid_event_owner','kind':'red_packet' if i==3 else 'transfer',
        'amount_minor':None if i==3 else 100,'currency':'USD' if i==2 else 'CNY','direction':None if i==3 else 'in'} for i in (1,2,3)]
    path=tmp_path/'events.json';path.write_text(json.dumps({'call_interactions':calls,'money_interactions':money}))
    assert import_archive(db,path)['call']['projected_rows']==2
    replay=import_archive(db,path);assert replay['call']['changed_source_objects']==replay['money']['changed_source_objects']==0
    data=summary(db,original);assert data['calls']['total']==2 and data['calls']['duration_seconds']==123
    assert data['money']['currencies']=={'CNY':{'in':100,'out':0},'USD':{'in':100,'out':0}}
    assert data['money']['unknown_amount']==data['money']['unknown_direction']==1
    assert len(records(db,original,'call',0,1))==1 and len(records(db,original,'call',1,1))==1
    set_link(db,aid,None,expected_profile_id=original);db.commit();assert summary(db,original)['money']['total']==0
    set_link(db,aid,target,expected_profile_id=None);db.commit();assert summary(db,target)['money']['total']==3
    with source_engine.connect() as c:assert c.execute(text('SELECT count(*) FROM objects')).scalar_one()==5


def test_global_atlas_and_statistics_follow_links_across_worker_caches(db,source_engine):
    from app import aggregate_cache
    from app.location_atlas import atlas
    from app.social_analytics import social_analytics
    aggregate_cache.clear_memory()
    original,target=uuid4(),uuid4()
    for pid in (original,target):db.execute(text("INSERT INTO profiles(id,profile_type,display_name) VALUES(:id,'person','Atlas person')"),{'id':pid})
    db.execute(text("INSERT INTO identities(profile_id,provider,external_id) VALUES(:id,'linkedin','atlas-account')"),{'id':original})
    register_accounts(db,'linkedin');aid=db.execute(text('SELECT id FROM source_accounts')).scalar_one()
    db.execute(text('UPDATE source_accounts SET migrated_at=now()'))
    db.execute(text("INSERT INTO platform_migration_state(provider,enabled) VALUES('linkedin',true)"))
    project_batch(db,ImportBatchRequest(source='linkedin',stream='profiles',batch_id='atlas',idempotency_key='atlas',observed_at=datetime.now(UTC),
        records=[{'external_id':'atlas-account','profile_id':original,'content':{'fields':{'linkedin.location':'Pittsburgh, Pennsylvania, United States'}}}]))
    db.commit()
    first=atlas(db,provider='linkedin',kind='profile')
    assert first['people']==1 and first['points'][0]['people'][0]['id']==str(original)
    assert first['points'][0]['latitude']==pytest.approx(40.44062)
    assert social_analytics(db,original)['providers']['linkedin']['people']==1
    # A second process or an in-flight request can retain the previous cache
    # even after the linking process clears its own memory.
    retained=dict(aggregate_cache._memory)
    set_link(db,aid,None,expected_profile_id=original);db.commit()
    aggregate_cache._memory.update(retained)
    assert atlas(db,provider='linkedin',kind='profile')['people']==0
    assert social_analytics(db,original)['providers']['linkedin']['people']==0
    set_link(db,aid,target,expected_profile_id=None);db.commit()
    moved=atlas(db,provider='linkedin',kind='profile')
    assert moved['people']==1 and moved['points'][0]['people'][0]['id']==str(target)
    with source_engine.connect() as c:assert c.execute(text("SELECT count(*) FROM objects WHERE object_kind='profile'")).scalar_one()==1


def test_simultaneous_careers_keep_keys_across_reorder_edits_unlink_and_replay(db,source_engine):
    from app.source_verification import projection_content
    person=uuid4()
    db.execute(text("INSERT INTO profiles(id,profile_type,display_name) VALUES(:id,'person','Career entries')"),{'id':person})
    db.execute(text("INSERT INTO identities(profile_id,provider,external_id) VALUES(:id,'linkedin','careers')"),{'id':person})
    register_accounts(db,'linkedin');aid=db.execute(text('SELECT id FROM source_accounts')).scalar_one()
    db.execute(text("INSERT INTO platform_migration_state(provider,enabled) VALUES('linkedin',true)"));db.commit()
    first={'company_name':'University','start_date':'2023-08','title':'COMP 550 assistant','description':'A'}
    second={**first,'title':'COMP 590 assistant'}
    def deliver(values,key):
        request=ImportBatchRequest(source='linkedin',stream='profiles',batch_id=key,idempotency_key=key,
            observed_at=datetime.now(UTC),records=[{'external_id':'careers','content':{'employment':values}}])
        project_batch(db,request);db.commit()
    def current():
        return {r.value['title']:(r.fact_key,r.value) for r in db.execute(text('''SELECT f.fact_key,o.payload_json AS value FROM source_account_facts f
            JOIN profile_employment_revisions r ON r.id=f.revision_id JOIN content_objects o ON o.id=r.content_object_id'''))}
    deliver([first,second],'both');initial=current();assert len(initial)==2
    deliver([second,first],'reordered');assert current()==initial
    assert db.execute(text('SELECT count(*) FROM profile_employment_revisions')).scalar_one()==2
    deliver([{**first,'description':'B'},second],'changed')
    deliver([second,first],'reverted');assert current()==initial
    key=initial[first['title']][0]
    history=db.execute(text('''SELECT o.payload_json->>'description' FROM profile_employment_revisions r
        JOIN content_objects o ON o.id=r.content_object_id WHERE r.fact_key=:key ORDER BY r.revision_seq'''),{'key':key}).scalars().all()
    assert history==['A','B','A']
    db.execute(text('UPDATE source_accounts SET migrated_at=now() WHERE id=:id'),{'id':aid});db.commit()
    set_link(db,aid,None,expected_profile_id=person);db.commit()
    deliver([second,{**first,'description':'C'}],'unlinked-change')
    assert db.execute(text('SELECT count(*) FROM profile_structured_current')).scalar_one()==0
    set_link(db,aid,person,expected_profile_id=None);db.commit()
    assert db.execute(text('SELECT count(*) FROM profile_structured_current')).scalar_one()==2
    with source_engine.connect() as source:assert projection_content(db,source,'linkedin')['employment']['mismatched']==0


def test_original_export_restores_collapsed_same_title_careers_once(db,source_engine,tmp_path):
    import json

    from app.imports import _project_structured_fact
    from app.platform_migrate import migrate_facts, migrate_pointers
    from app.structured_facts import repair_linkedin_collections
    person=uuid4();at=datetime.now(UTC)
    db.execute(text("INSERT INTO profiles(id,profile_type,display_name) VALUES(:id,'person','Two research roles')"),{'id':person})
    db.execute(text("INSERT INTO identities(profile_id,provider,external_id) VALUES(:id,'linkedin','researcher')"),{'id':person})
    values=[{'company_name':'University','start_date':'2022-08','end_date':end,'title':'Research Assistant','description':topic}
        for end,topic in [('2023-05','Stocks'),('2022-12','Statistics')]]
    for value in values:_project_structured_fact(db,profile_id=person,fact_type='employment',value=value,source_type='linkedin',source_record_id='researcher',observed_at=at)
    register_accounts(db,'linkedin');migrate_facts(db,'linkedin');migrate_pointers(db,'linkedin')
    archive=tmp_path/'linkedin.json';archive.write_text(json.dumps({'profiles':[{'provider_user_id':'researcher','profile':{'experiences':values}}]}))
    before=db.execute(text('SELECT count(*) FROM profile_employment_revisions')).scalar_one()
    assert repair_linkedin_collections(db,archive)['different_entries']==1
    assert repair_linkedin_collections(db,archive,True)['appended_entries']==1
    assert repair_linkedin_collections(db,archive,True)['different_entries']==0
    assert db.execute(text('SELECT count(*) FROM profile_employment_revisions')).scalar_one()==before+1
    assert db.execute(text('SELECT count(*) FROM source_account_facts')).scalar_one()==2
    assert db.execute(text('SELECT count(*) FROM profile_structured_current')).scalar_one()==2


def test_career_collection_reuses_earlier_whole_value_keys(db,source_engine):
    from app.imports import _project_structured_fact
    from app.platform_migrate import migrate_facts, migrate_pointers
    from app.structured_facts import collection_keys, fingerprint
    person=uuid4();at=datetime.now(UTC)
    db.execute(text("INSERT INTO profiles(id,profile_type,display_name) VALUES(:id,'person','Existing two roles')"),{'id':person})
    db.execute(text("INSERT INTO identities(profile_id,provider,external_id) VALUES(:id,'linkedin','roles')"),{'id':person})
    values=[{'company_name':'University','start_date':'2023-08','title':title} for title in ['COMP 550','COMP 590']]
    for value in values:_project_structured_fact(db,profile_id=person,fact_type='employment',value=value,source_type='linkedin',
        source_record_id='roles',observed_at=at,fact_key_override='linkedin:roles:'+fingerprint(value))
    register_accounts(db,'linkedin');migrate_facts(db,'linkedin');migrate_pointers(db,'linkedin')
    aid=db.execute(text('SELECT id FROM source_accounts')).scalar_one()
    resolved=collection_keys(db,source='linkedin',record='roles',kind='employment',values=list(reversed(values)),account=aid)
    assert {key for _,key in resolved}=={'linkedin:roles:'+fingerprint(value) for value in values}
    assert db.execute(text('SELECT count(*) FROM profile_employment_revisions')).scalar_one()==2


def test_relationship_source_copy_retries_after_projection_failure(db,source_engine):
    from sqlalchemy import event

    from app.platform_migrate import migrate_interactions
    ids=[uuid4() for _ in range(3)]
    for i,pid in enumerate(ids):
        db.execute(text("INSERT INTO profiles(id,profile_type,display_name) VALUES(:id,'person','Same name')"),{'id':pid})
        db.execute(text("INSERT INTO identities(profile_id,provider,external_id) VALUES(:id,'wechat',:external)"),{'id':pid,'external':f'actual-{i}'})
    register_accounts(db,'wechat')
    kind=db.execute(text("INSERT INTO relationship_types(name) VALUES('Confirmed friend') RETURNING id")).scalar_one()
    for target in ids[1:]:
        db.execute(text("INSERT INTO relationship_edges(from_profile_id,to_profile_id,relationship_type_id,source_type) VALUES(:a,:b,:kind,'wechat')"),{'a':ids[0],'b':target,'kind':kind})
    db.commit()
    def fail_projection(conn,cursor,statement,params,context,executemany):
        if statement.startswith('INSERT INTO source_relationship_edges'):raise RuntimeError('projection interrupted')
    event.listen(db.bind,'before_cursor_execute',fail_projection)
    try:
        with pytest.raises(RuntimeError,match='projection interrupted'):migrate_interactions(db,'wechat')
    finally:
        event.remove(db.bind,'before_cursor_execute',fail_projection);db.rollback()
    with source_engine.connect() as source:
        assert source.execute(text("SELECT count(*) FROM objects WHERE object_kind='relationship'")).scalar_one()==2
        revision_count=source.execute(text('SELECT count(*) FROM revisions')).scalar_one()
    migrate_interactions(db,'wechat');migrate_interactions(db,'wechat')
    assert db.execute(text('SELECT count(*) FROM source_relationship_edges')).scalar_one()==2
    assert db.execute(text('SELECT count(*) FROM relationship_edges WHERE from_account_id IS NULL OR to_account_id IS NULL')).scalar_one()==0
    with source_engine.connect() as source:
        assert source.execute(text('SELECT count(*) FROM revisions')).scalar_one()==revision_count


def test_group_migration_preserves_unknown_role_across_relink(db,source_engine):
    from app.platform_migrate import migrate_groups
    people=[uuid4(),uuid4()];group=uuid4()
    for pid in people:
        db.execute(text("INSERT INTO profiles(id,profile_type,display_name) VALUES(:id,'person','Same name')"),{'id':pid})
    db.execute(text("INSERT INTO profiles(id,profile_type,display_name) VALUES(:id,'group','Known room')"),{'id':group})
    db.execute(text("INSERT INTO identities(profile_id,provider,external_id) VALUES(:id,'wechat','actual-person'),(:group,'wechat','actual-room@chatroom')"),{'id':people[0],'group':group})
    register_accounts(db,'wechat')
    db.execute(text("INSERT INTO group_memberships(group_profile_id,person_profile_id,source_record_id) VALUES(:group,:person,'membership:actual-room@chatroom:actual-person')"),{'group':group,'person':people[0]})
    db.commit()
    assert migrate_groups(db)['memberships']==1
    assert migrate_groups(db)['memberships']==1
    assert db.execute(text('SELECT role FROM source_group_memberships')).scalar_one() is None
    with source_engine.connect() as source:
        assert source.execute(text("SELECT count(*) FROM revisions r JOIN objects o ON o.id=r.object_id WHERE o.object_kind='membership'")).scalar_one()==1
    db.execute(text("UPDATE source_accounts SET migrated_at=now()"))
    db.execute(text("INSERT INTO platform_migration_state(provider,enabled) VALUES('wechat',true)"));db.commit()
    aid=db.execute(text("SELECT id FROM source_accounts WHERE external_id='actual-person'")).scalar_one()
    set_link(db,aid,None,expected_profile_id=people[0]);db.commit()
    assert db.execute(text('SELECT count(*) FROM group_memberships')).scalar_one()==0
    set_link(db,aid,people[1],expected_profile_id=None);db.commit()
    member=db.execute(text('SELECT person_profile_id,role FROM group_memberships')).one()
    assert member.person_profile_id==people[1] and member.role is None


def test_source_checks_reject_wrong_group_role_or_tag_with_equal_counts(db,source_engine,tmp_path):
    import json

    from app.platform_migrate import migrate_groups, migrate_tags, verify
    people=[uuid4(),uuid4()];group=uuid4()
    for number,pid in enumerate(people):
        db.execute(text("INSERT INTO profiles(id,profile_type,display_name) VALUES(:id,'person','Same name')"),{'id':pid})
        db.execute(text("INSERT INTO identities(profile_id,provider,external_id) VALUES(:id,'wechat',:external)"),{'id':pid,'external':f'person-{number}'})
    db.execute(text("INSERT INTO profiles(id,profile_type,display_name) VALUES(:id,'group','Room')"),{'id':group})
    db.execute(text("INSERT INTO identities(profile_id,provider,external_id) VALUES(:id,'wechat','actual-room@chatroom')"),{'id':group})
    register_accounts(db,'wechat')
    db.execute(text("""INSERT INTO group_memberships(group_profile_id,person_profile_id,role,joined_at,source_record_id)
        VALUES(:group,:person,NULL,'2020-01-02T03:04:05.123456+00:00','chatlog:actual-room@chatroom:person-0')"""),
        {'group':group,'person':people[0]});db.commit()
    migrate_groups(db)
    archive=tmp_path/'tags.json';archive.write_text(json.dumps({'tags':{'person-0':['Friends'],'person-1':['School']}}))
    migrate_tags(db,archive)
    report=verify(db,'wechat')
    assert report['errors']==[]
    assert report['projection_checks']['memberships']['checked']==1
    assert report['projection_checks']['tags']['checked']==2
    # Counts still match, but the role and account tag associations no longer
    # agree with the independent source. Activation must be refused.
    db.execute(text("UPDATE source_group_memberships SET role='owner'"))
    db.execute(text("UPDATE source_account_tags SET tag_id=(SELECT id FROM tags WHERE name='School') WHERE tag_id=(SELECT id FROM tags WHERE name='Friends')"));db.commit()
    report=verify(db,'wechat')
    assert report['memberships']=={'source':1,'projection':1}
    assert report['projection_checks']['memberships']['mismatched']==1
    assert report['projection_checks']['tags']['mismatched']==1
    with pytest.raises(ValueError,match='memberships content or media mismatch'):verify(db,'wechat',True)
    assert db.execute(text("SELECT count(*) FROM platform_migration_state WHERE enabled")).scalar_one()==0


def test_group_source_id_never_falls_back_to_another_profile_account(db,source_engine):
    from app.platform_migrate import migrate_groups
    group,person=uuid4(),uuid4()
    for pid,kind in [(group,'group'),(person,'person')]:
        db.execute(text("INSERT INTO profiles(id,profile_type,display_name) VALUES(:id,:kind,'Same name')"),{'id':pid,'kind':kind})
    db.execute(text("INSERT INTO identities(profile_id,provider,external_id) VALUES(:group,'wechat','room@chatroom'),(:person,'wechat','other-existing-account')"),{'group':group,'person':person})
    register_accounts(db,'wechat')
    db.execute(text("""INSERT INTO group_memberships(group_profile_id,person_profile_id,source_record_id)
        VALUES(:group,:person,'membership:room@chatroom:native-member')"""),{'group':group,'person':person});db.commit()
    assert migrate_groups(db)['unlinked_source_accounts_created']==1
    assert migrate_groups(db)['unlinked_source_accounts_created']==0
    row=db.execute(text('''SELECT a.id,a.external_id FROM source_group_memberships m
        JOIN source_accounts a ON a.id=m.person_account_id''')).one()
    assert row.external_id=='native-member'
    assert db.execute(text('SELECT count(*) FROM source_account_links WHERE account_id=:id'),{'id':row.id}).scalar_one()==0
    assert db.execute(text('SELECT count(*) FROM profiles')).scalar_one()==2
    assert db.execute(text('SELECT count(*) FROM identities')).scalar_one()==2
    with source_engine.connect() as source:
        assert source.execute(text("SELECT count(*) FROM objects WHERE object_kind='membership'")).scalar_one()==1
        assert source.execute(text('SELECT count(*) FROM revisions')).scalar_one()==1


def test_linkedin_incremental_accounts_skip_unchanged_and_preserve_aba(db, source_engine, monkeypatch, tmp_path):
    import copy

    from app import full_migration
    from app.platform_store import account_id, object_id

    db.execute(text("INSERT INTO platform_migration_state(provider,enabled) VALUES('linkedin',true)"));db.commit()
    path = tmp_path / 'export.json'
    path.write_text('{}')
    snapshot = {'profiles': [
        {'provider_user_id': name, 'profile': {'name': 'Same name', 'about': 'A'},
         'posts': [{'provider_post_id': 'post-' + name, 'text': 'initial'}]}
        for name in ('first-account', 'second-account')
    ]}
    run = full_migration.run_batch
    batches = []
    def record(*args, **kwargs):
        batches.append((kwargs['stream'], [r['external_id'] for r in kwargs['records']]))
        return run(*args, **kwargs)
    from app import linkedin_import
    monkeypatch.setattr(linkedin_import, 'run_batch', record)
    def submit():
        full_migration.load_linkedin(db, path, {}, export_snapshot=copy.deepcopy(snapshot), incremental=True)
    submit()
    batches.clear();submit()
    assert batches == []
    post_versions = dict(db.execute(text("SELECT id,xmin::text FROM social_posts WHERE external_id='post-second-account'")).all())
    snapshot['profiles'][0]['profile']['about'] = 'B'
    submit()
    # Importing one account must not rewrite another account's normalized
    # posts and invalidate the visibility map needed by covering indexes.
    assert dict(db.execute(text("SELECT id,xmin::text FROM social_posts WHERE external_id='post-second-account'")).all()) == post_versions
    assert ('profiles', ['first-account']) in batches
    assert all('second-account' not in ids and 'post-second-account' not in ids for _, ids in batches)
    snapshot['profiles'][0]['profile']['about'] = 'A'
    submit()
    oid = object_id(account_id('linkedin', 'first-account'), 'field', 'linkedin.about')
    with source_engine.connect() as source:
        assert source.execute(text('SELECT revision_number FROM objects WHERE id=:id'), {'id': oid}).scalar_one() == 3
    assert db.execute(text('SELECT count(*) FROM profiles')).scalar_one() == 0
    # A post failure cannot mark the changed account complete.
    snapshot['profiles'][0]['posts'][0]['text'] = 'changed'
    def fail_posts(*args, **kwargs):
        if kwargs['stream'] == 'posts':
            raise RuntimeError('post projection interrupted')
        return run(*args, **kwargs)
    monkeypatch.setattr(linkedin_import, 'run_batch', fail_posts)
    with pytest.raises(RuntimeError, match='post projection interrupted'):
        submit()
    db.rollback()
    from app import linkedin_import
    monkeypatch.setattr(linkedin_import, 'run_batch', record)
    batches.clear();submit()
    assert ('posts', ['post-first-account']) in batches
    batches.clear();submit()
    assert batches == []


def test_unlinked_message_senders_survive_cross_database_retry_and_manual_link(db,source_engine,tmp_path):
    import hashlib
    import sqlite3

    from sqlalchemy import event

    from app.import_messages import ingest
    from app.platform_migrate import migrate_messages, verify
    from app.repositories import create_profile
    from app.routers.messages import profile_messages

    owner=create_profile(db,'person','Conversation owner',None)['id']
    db.execute(text("INSERT INTO identities(profile_id,provider,external_id) VALUES(:id,'wechat','wxid_room')"),{'id':owner})
    db.commit()
    table='Msg_'+hashlib.md5(b'wxid_room').hexdigest()
    file=tmp_path/'message_0.db'
    with sqlite3.connect(file) as source:
        source.execute('CREATE TABLE Name2Id(user_name TEXT)')
        source.executemany('INSERT INTO Name2Id VALUES(?)',[('wxid_room',),('wxid_unlinked_sender',),('wxid_later_sender',)])
        source.execute(f'CREATE TABLE {table}(local_id INTEGER PRIMARY KEY,server_id INTEGER,create_time INTEGER,local_type INTEGER,real_sender_id INTEGER,message_content TEXT,compress_content TEXT)')
        source.execute(f"INSERT INTO {table} VALUES(1,1,1767225600,1,2,'Original message',NULL)")
    ingest(db,tmp_path);register_accounts(db,'wechat')
    def fail(conn,cursor,statement,params,context,executemany):
        if statement.startswith('UPDATE message_events SET sender_account_id'):raise RuntimeError('sender interrupted')
    event.listen(db.bind,'before_cursor_execute',fail)
    try:
        with pytest.raises(RuntimeError,match='sender interrupted'):migrate_messages(db)
    finally:
        event.remove(db.bind,'before_cursor_execute',fail);db.rollback()
    assert db.execute(text("SELECT count(*) FROM source_accounts WHERE external_id='wxid_unlinked_sender'")).scalar_one()==0
    with source_engine.begin() as source:
        sender=source.execute(text("UPDATE accounts SET display_name='Preserved source metadata' WHERE external_id='wxid_unlinked_sender' RETURNING id")).scalar_one()
        revisions=source.execute(text('SELECT count(*) FROM revisions')).scalar_one()
    stopping=False
    def committed_cursor(conn,cursor,statement,params,context,executemany):
        nonlocal stopping
        if 'INSERT INTO migration_cursors' in statement:stopping=True
    event.listen(source_engine,'after_cursor_execute',committed_cursor)
    try:
        checkpoint=migrate_messages(db,batch_size=1,stop_requested=lambda:stopping)
        assert checkpoint['messages_copied']==1 and checkpoint['stopped_at_checkpoint']
    finally:event.remove(source_engine,'after_cursor_execute',committed_cursor)
    assert migrate_messages(db)['messages_copied']==0
    assert db.execute(text('SELECT count(*) FROM profiles')).scalar_one()==1
    assert db.execute(text('SELECT display_name FROM source_accounts WHERE id=:id'),{'id':sender}).scalar_one()=='Preserved source metadata'
    assert db.execute(text('SELECT sender_account_id FROM message_events')).scalar_one()==sender
    assert db.execute(text('SELECT count(*) FROM source_account_links WHERE account_id=:id'),{'id':sender}).scalar_one()==0
    assert verify(db,'wechat',activate=True)['activated']
    human=create_profile(db,'person','Manually confirmed sender',None)['id'];db.commit()
    set_link(db,sender,human);db.commit()
    assert profile_messages(owner,db=db,_user={})[0]['sender_name']=='Manually confirmed sender'
    set_link(db,sender,None,expected_profile_id=human);db.commit()
    assert profile_messages(owner,db=db,_user={})[0]['sender_name'] is None
    with source_engine.connect() as source:assert source.execute(text('SELECT count(*) FROM revisions')).scalar_one()==revisions
    with sqlite3.connect(file) as source:source.execute(f"INSERT INTO {table} VALUES(2,2,1767225601,1,3,'New source sender',NULL)")
    assert ingest(db,tmp_path)['inserted']==1
    assert db.execute(text('SELECT count(*) FROM profiles')).scalar_one()==2
    assert db.execute(text("SELECT count(*) FROM message_events m JOIN source_accounts a ON a.id=m.sender_account_id WHERE a.external_id='wxid_later_sender' AND a.migrated_at IS NOT NULL")).scalar_one()==1


def test_batch_sender_projection_uses_message_id_and_original_time(db,source_engine):
    from app.content import get_or_create_content
    from app.platform_migrate import ensure_message_senders, update_message_senders

    aliases={}
    ensure_message_senders(db,aliases,['sender-a','sender-b'])
    room=db.execute(text("INSERT INTO conversations(conversation_type,external_id) VALUES('direct','direct:room') RETURNING id")).scalar_one()
    content,_=get_or_create_content(db,value={'text':'Same stable ID at different original times'},content_kind='message')
    mid=uuid4();times=[datetime(year,1,2,tzinfo=UTC) for year in (2018,2025,2026)]
    for at in times:
        db.execute(text('''INSERT INTO message_events(id,conversation_id,occurred_at,content_object_id,source_record_id,message_type,sender_account_id)
            VALUES(:id,:room,:at,:content,'server:1','text',:account)'''),
            {'id':mid,'room':room,'at':at,'content':content,'account':aliases['sender-a']})
    updates=[{'id':mid,'at':times[0],'account':aliases['sender-b']},{'id':mid,'at':times[1],'account':None}]
    assert update_message_senders(db,updates)==2
    assert update_message_senders(db,updates)==0
    actual=db.execute(text('SELECT occurred_at,sender_account_id,content_object_id FROM message_events ORDER BY occurred_at')).all()
    assert [(r.occurred_at,str(r.sender_account_id) if r.sender_account_id else None) for r in actual]==[
        (times[0],aliases['sender-b']),(times[1],None),(times[2],aliases['sender-a'])]
    assert all(str(r.content_object_id)==str(content) for r in actual)
    assert db.execute(text("SELECT count(*) FROM source_account_links l JOIN source_accounts a ON a.id=l.account_id WHERE a.external_id='wxid_later_sender'")).scalar_one()==0


def test_source_message_cli_signal_exits_75_and_releases_lease(db):
    import subprocess
    import sys

    code="""
import os,signal,sys
from app import platform_migrate
def interrupted(db,limit,**options):
    os.kill(os.getpid(),signal.SIGTERM)
    assert options['stop_requested']()
    return {'stopped_at_checkpoint':True}
platform_migrate.migrate_messages=interrupted
sys.argv=['platform_migrate','messages','--provider','wechat','--batch-size','1']
platform_migrate.main()
"""
    result=subprocess.run([sys.executable,'-c',code],capture_output=True,text=True,check=False,timeout=20,
        env={**os.environ,'DATABASE_URL':os.environ['TEST_DATABASE_URL']})
    assert result.returncode==75,result.stderr
    assert '"stopped_at_checkpoint": true' in result.stdout
    assert db.execute(text('SELECT pg_try_advisory_xact_lock(11803293)')).scalar_one()


def test_message_payloads_reference_crm_content_and_trim_existing_history(db, source_engine):
    from app.content import canonical_json
    from app.platform_store import (
        ensure_account,
        observe_immutable_many,
        observe_many,
        payload_values,
    )
    from app.platform_trim import trim

    value={'text':'hello','sender_external_id':'wxid_a','message_type':'1'}
    digest=__import__('hashlib').sha256(canonical_json(value)).hexdigest()
    db.execute(text("""INSERT INTO content_objects(sha256,content_kind,byte_length,payload_json)
        VALUES(:sha,'message',1,CAST(:v AS jsonb))"""),{'sha':digest,'v':__import__('json').dumps(value)})
    with source_engine.begin() as source:
        account=ensure_account(source,'wechat','trim-room')
        observe_immutable_many(source,[{'account':account,'kind':'message','external_id':'new','value':value,
                                        'occurred_at':datetime(2026,9,20,tzinfo=UTC)}])
        assert source.execute(text("SELECT value IS NULL AND crm_content_kind='message' FROM payloads")).scalar_one()
        assert payload_values(source,db,[digest])=={digest:value}
        legacy={**value,'text':'legacy'}
        legacy_hash=__import__('hashlib').sha256(canonical_json(legacy)).hexdigest()
        observe_many(source,[{'account':account,'kind':'message','external_id':'old','value':legacy,'source_event':'e1'}])
    db.execute(text("""INSERT INTO content_objects(sha256,content_kind,byte_length,payload_json)
        VALUES(:sha,'message',1,CAST(:v AS jsonb))"""),{'sha':legacy_hash,'v':__import__('json').dumps(legacy)})
    report=trim(db,'wechat')
    assert report['payloads_referenced']==1 and report['revisions_removed']==1 and report['observations_removed']==1
    with source_engine.connect() as source:
        assert source.execute(text('SELECT count(*) FROM payloads WHERE value IS NOT NULL')).scalar_one()==0
        assert payload_values(source,db,[legacy_hash])=={legacy_hash:legacy}


@pytest.mark.parametrize('zone', ['UTC', 'America/Chicago'])
def test_message_legacy_timezone_identity_is_preserved(source_engine, zone):
    from zoneinfo import ZoneInfo

    from app.platform_store import ensure_account, object_id, observe_immutable_many
    from app.source_time import message_key
    at = datetime(2026, 7, 1, 12, 34, 56, tzinfo=UTC)
    legacy = 'stable-message:' + at.astimezone(ZoneInfo('America/Chicago')).isoformat()
    with source_engine.begin() as source:
        source.execute(text("SELECT set_config('TimeZone', :zone, true)"), {'zone': zone})
        account = ensure_account(source, 'wechat', 'timezone-room')
        row = {'account': account, 'kind': 'message', 'external_id': legacy,
               'occurred_at': at, 'value': {'text': 'exact same event'}}
        old = observe_immutable_many(source, [row])[0]['object_id']
        canonical = {**row, 'external_id': message_key('stable-message', at)}
        assert observe_immutable_many(source, [canonical])[0]['object_id'] == old
        assert old == object_id(account, 'message', legacy)
        assert source.execute(text('SELECT count(*) FROM objects')).scalar_one() == 1
        # Same timestamp and body alone never merge two source message IDs.
        other = {**canonical, 'external_id': message_key('another-message', at)}
        assert observe_immutable_many(source, [other])[0]['object_id'] != old
