import json
import os
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from app.bulk_moments import project_moments
from app.remote_media import enqueue_media, materialize_image
from app.schemas import ImportBatchRequest


def test_group_cannot_inherit_moments_or_contact_share_friends(db, tmp_path):
    from fastapi import HTTPException

    from app.graph import graph_projection
    from app.main import profile_moments_circle, profile_relationships
    from app.repositories import append_field_revision
    from app.routers.locations import profile_locations

    group,owner,friend,former=uuid4(),uuid4(),uuid4(),uuid4()
    for pid,kind,name in [(group,'group','Rental room'),(owner,'person','Owner'),(friend,'person','Friend'),(former,'person','Former member')]:
        db.execute(text('INSERT INTO profiles(id,profile_type,display_name) VALUES(:id,:kind,:name)'),{'id':pid,'kind':kind,'name':name})
    append_field_revision(db,profile_id=owner,field_key='wechat.location_history',value={'places':[{'lat':35.66,'lon':139.72,'poi':'Tokyo','visits':1}]},source_type='wechat',operation='import')
    for pid,role,left in [(owner,'owner',False),(friend,'admin',False),(former,'member',True)]:
        db.execute(text("""INSERT INTO group_memberships(group_profile_id,person_profile_id,role,left_at)
            VALUES(:group,:person,:role,CASE WHEN :left THEN now() END)"""),{'group':group,'person':pid,'role':role,'left':left})
    rel=db.execute(text("INSERT INTO relationship_types(name) VALUES('微信名片推荐') RETURNING id")).scalar_one()
    for a,b in [(group,friend),(owner,friend)]:
        db.execute(text('INSERT INTO relationship_edges(from_profile_id,to_profile_id,relationship_type_id) VALUES(:a,:b,:t)'),{'a':a,'b':b,'t':rel})
    db.flush()
    assert profile_locations(group,db,{})['points']==[]
    assert profile_locations(owner,db,{})['place_count']==1
    relations=profile_relationships(group,db,{})
    assert {r['related_profile_id'] for r in relations}=={str(owner),str(friend)}
    assert {r['relationship_type'] for r in relations}=={'群主','管理员'}
    assert all(r['id'].startswith('membership:') for r in relations)
    graph=graph_projection(db,tmp_path)
    assert all(not e.get('relationship_types') for e in graph['edges'])
    with pytest.raises(HTTPException) as error:profile_moments_circle(group,db,{})
    assert error.value.status_code==422
    with pytest.raises(Exception,match='A group cannot own personal Moments'),db.begin_nested():
        append_field_revision(db,profile_id=group,field_key='wechat.location_history',value={'places':[]},source_type='wechat',operation='import')


def test_notebook_reconcile_uses_exact_ids_preserves_manual_tags_and_replays_changes(db,tmp_path):
    from app.full_migration import add_tag
    from app.main import profile
    from app.reconcile_notebook import reconcile

    person,group=uuid4(),uuid4()
    for pid,kind in [(person,'person'),(group,'group')]:
        db.execute(text("INSERT INTO profiles(id,profile_type,display_name) VALUES(:id,:kind,'Same name')"),{'id':pid,'kind':kind})
    for provider,ext,pid in [('monica','contact:1',person),('monica','contact:2',group),('wechat','wxid_owner',person)]:
        db.execute(text('INSERT INTO identities(provider,external_id,profile_id) VALUES(:provider,:ext,:pid)'),{'provider':provider,'ext':ext,'pid':pid})
    add_tag(db,name='Wrong owner label',profile_id=group,source_type='wechat',source_record_id='old')
    add_tag(db,name='Manual',profile_id=group,source_type='manual',source_record_id='manual')
    data={'contacts':[{'id':1,'nickname':'Personal note'},{'id':2}],
          'contact_field_types':[{'id':10,'name':'微信备注'}],
          'contact_fields':[{'contact_id':1,'contact_field_type_id':10,'data':'A'}],
          'tags':[{'id':1,'name':'Classmate'}],'contact_tag':[{'contact_id':1,'tag_id':1}], 'notes':[]}
    export=tmp_path/'notebook.ndjson';tags=tmp_path/'tags.json'
    tags.write_text(json.dumps({'tags':{'wxid_owner':['Classmate']}}))
    def write():
        rows=[{'table':table,'row':row} for table,values in data.items() for row in values]
        rows.append({'table':'export_manifest','row':{'counts':{table:len(values) for table,values in data.items()}}})
        export.write_text('\n'.join(json.dumps(row) for row in rows))
    write();before=reconcile(db,export,tags)
    assert before['tags_added']==1 and before['tags_quarantined']==1
    reconcile(db,export,tags,True)
    p=profile(person,db,{})
    assert [tag['name'] for tag in p['tags']]==['Classmate']
    assert p['presentation']['sources']['wechat']['remark']=='A'
    assert [tag['name'] for tag in profile(group,db,{})['tags']]==['Manual']
    assert reconcile(db,export,tags)['field_differences']==0
    assert reconcile(db,export,tags)['tags_added']==0
    for value in ('B','A'):
        data['contact_fields'][0]['data']=value;write();reconcile(db,export,tags,True)
    assert db.execute(text("SELECT count(*) FROM profile_field_revisions WHERE profile_id=:id AND field_key='monica.custom_fields'"),{'id':person}).scalar_one()==3
    export.write_text(export.read_text().rsplit('\n',1)[0])
    with pytest.raises(ValueError,match='truncated'):reconcile(db,export,tags,True)


@pytest.fixture
def db(monkeypatch, tmp_path):
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL is required for isolated PostgreSQL integration tests")
    engine = create_engine(url)
    schema = "test_" + uuid4().hex
    with engine.connect() as connection:
        connection.execute(text(f"CREATE SCHEMA {schema}"))
        connection.execute(text(f"SET search_path TO {schema}, public"))
        for path in sorted((Path(__file__).parents[3] / "migrations").glob("*.sql")):
            connection.exec_driver_sql(path.read_text())
        connection.commit()
        from app.config import get_settings
        monkeypatch.setenv("RAW_ROOT", str(tmp_path / "raw"))
        get_settings.cache_clear()
        try:
            with Session(bind=connection) as session:
                yield session
        finally:
            connection.rollback()
            connection.execute(text("SET search_path TO public"))
            connection.execute(text(f"DROP SCHEMA {schema} CASCADE"))
            connection.commit()
            get_settings.cache_clear()
    engine.dispose()


def test_bulk_moments_retry_and_unchanged_observation(db):
    def submit(batch, caption):
        request = ImportBatchRequest(source="wechat", stream="moments", batch_id=batch,
            idempotency_key=batch, observed_at=datetime.now(UTC), cursor_after=batch,
            records=[{"external_id": "post-1", "entity_type": "post", "occurred_at": "2026-01-01T12:00:00Z",
                      "content": {"text": caption, "media": [], "interactions": []}}])
        project_moments(db, request)
        db.commit()

    def counts():
        return tuple(db.execute(text(f"SELECT count(*) FROM {table}")).scalar_one()
                     for table in ("social_posts", "social_post_revisions", "source_observations", "jobs"))

    submit("one", "before")
    assert counts() == (1, 1, 1, 1)
    submit("one", "before")
    assert counts() == (1, 1, 1, 1)
    submit("two", "before")
    assert counts() == (1, 1, 2, 1)
    assert db.execute(text("SELECT count(*) FROM content_objects WHERE content_kind='post_body'")).scalar_one() == 1
    submit("three", "after")
    assert counts() == (1, 2, 3, 2)
    assert db.execute(text("SELECT cursor_value FROM source_cursors")).scalar_one() == "three"


def test_two_posts_share_one_local_image_and_idempotent_jobs(db, monkeypatch, tmp_path):
    calls = []
    image_path = tmp_path / "photo"
    image_path.write_bytes(b"photo")

    def download(url, *_):
        calls.append(url)
        return "a" * 64, str(image_path), 5, "image/jpeg"

    monkeypatch.setattr("app.remote_media.download_image", download)
    for entity_id in (uuid4(), uuid4()):
        kwargs = {"url": "https://media.licdn.com/photo", "entity_type": "social_post", "entity_id": entity_id, "role": "media-1"}
        assert enqueue_media(db, **kwargs)
        assert not enqueue_media(db, **kwargs)
    for payload in db.execute(text("SELECT payload FROM jobs WHERE job_type='media.download'")).scalars():
        from app.remote_media import needs_download
        downloaded=download(payload['url']) if needs_download(db,payload) else None
        materialize_image(db, payload, tmp_path,downloaded)
    assert len(calls) == 1
    assert db.execute(text("SELECT count(*) FROM media_assets")).scalar_one() == 1
    assert db.execute(text("SELECT count(*) FROM media_links")).scalar_one() == 2
    assert db.execute(text("SELECT count(*) FROM content_objects WHERE content_kind='media'")).scalar_one() == 1
    assert db.execute(text("SELECT count(*) FROM jobs WHERE job_type='media.download'")).scalar_one() == 2
    assert db.execute(text("SELECT count(*) FROM jobs WHERE job_type='media.dimensions'")).scalar_one() == 1


def test_pending_media_deduplicates_across_days_and_retains_migration_evidence(db):
    kwargs={'url':'https://media.licdn.com/same','entity_type':'social_post',
            'entity_id':uuid4(),'role':'media-1'}
    assert enqueue_media(db,**kwargs)
    db.execute(text("""UPDATE jobs SET payload=payload ||
        '{"check_day":"2000-01-01","download_key":"older-day"}'::jsonb"""))
    assert not enqueue_media(db,**kwargs)
    db.execute(text("UPDATE jobs SET status='complete'"))
    assert enqueue_media(db,**kwargs)
    assert db.execute(text('SELECT count(*) FROM jobs')).scalar_one()==2
    # Recreate a legacy queue with several dates while a worker owns one job.
    db.execute(text('DROP INDEX jobs_media_active_slot_idx'))
    db.execute(text('DROP INDEX jobs_active_subject'))
    current=db.execute(text("SELECT id,payload FROM jobs WHERE status='queued'")).one()
    db.execute(text("UPDATE jobs SET status='running',locked_at=now(),attempts=2 WHERE id=:id"),{'id':current.id})
    for day in ('2001-01-01','2001-01-02'):
        db.execute(text("INSERT INTO jobs(job_type,payload) VALUES('media.download',CAST(:p AS jsonb))"),
                   {'p':json.dumps({**current.payload,'check_day':day,'download_key':day})})
    before=dict(db.execute(text('SELECT id,payload FROM jobs')).all())
    migration=(Path(__file__).parents[3]/'migrations/041_media_active_jobs.sql').read_text()
    db.execute(text(migration));db.execute(text(migration))
    assert dict(db.execute(text('SELECT id,payload FROM jobs')).all())==before
    assert dict(db.execute(text('SELECT status,count(*) FROM jobs GROUP BY status')).all())=={
        'complete':1,'running':1,'superseded':2}
    assert db.execute(text('SELECT attempts FROM jobs WHERE id=:id'),{'id':current.id}).scalar_one()==2
    assert not enqueue_media(db,**kwargs)


def test_materialization_cannot_download_inside_transaction(db,monkeypatch,tmp_path):
    def forbidden(*_args):
        pytest.fail('Network access from a database materialization transaction')
    monkeypatch.setattr('app.remote_media.download_image',forbidden)
    payload={'url':'https://media.licdn.com/uncached','entity_type':'social_post',
             'entity_id':str(uuid4()),'role':'media-1'}
    with pytest.raises(ValueError,match='before opening'):
        materialize_image(db,payload,tmp_path)
    assert db.execute(text('SELECT count(*) FROM media_assets')).scalar_one()==0


def test_linkedin_export_sync_advances_watermark(db, monkeypatch, tmp_path):
    from contextlib import nullcontext

    from app import sync_linkedin

    monkeypatch.setattr(sync_linkedin, "SessionLocal", lambda: nullcontext(db))
    path = tmp_path / "linkedin.json"
    path.write_text(json.dumps({"profiles": [{"provider_user_id": "test-user", "profile": {"name": "Test"},
        "posts": [{"provider_post_id": "urn:li:activity:1", "posted_at": "2026-01-01T00:00:00Z",
                   "text": "Hello", "image_urls": ["https://media.licdn.com/a"]}]}]}))
    sync_linkedin.sync(path)
    count = db.execute(text("SELECT count(*) FROM import_batches")).scalar_one()
    sync_linkedin.sync(path)
    assert db.execute(text("SELECT count(*) FROM import_batches")).scalar_one() == count
    assert db.execute(text("SELECT count(*) FROM jobs WHERE job_type='media.download'")).scalar_one() == 1
    assert db.execute(text("SELECT external_id FROM social_posts")).scalar_one() == "urn:li:activity:1"
    assert db.execute(text("SELECT count(*) FROM social_posts WHERE occurred_at IS NOT NULL")).scalar_one() == 1


def test_timeline_and_detail_resolve_local_media(db, monkeypatch, tmp_path):
    from app.imports import project_batch
    from app.routers.timeline import timeline, timeline_detail

    for entity, stream in (("post", "posts"), ("story", "stories")):
        content = {"text": "Image post", "media": [{"source_url": "https://media.licdn.com/image"}]}
        request = ImportBatchRequest(source="linkedin", stream=stream, batch_id=entity,
            idempotency_key=entity, observed_at=datetime.now(UTC), records=[{
                "external_id": entity, "entity_type": entity, "content": content,
                "occurred_at": "2026-01-01T00:00:00Z"}])
        project_batch(db, request)
        entity_id = db.execute(text(f"SELECT id FROM social_{stream}")).scalar_one()
        monkeypatch.setattr("app.remote_media.download_image", lambda *_: ("b"*64, str(tmp_path / "photo"), 5, "image/jpeg"))
        materialize_image(db, {"url": "https://media.licdn.com/image", "entity_type": "social_" + entity,
                              "entity_id": entity_id, "role": "media-1"}, tmp_path,
                          ("b"*64,str(tmp_path / "photo"),5,"image/jpeg"))
        detail = timeline_detail(entity_id, db=db, _user={})
        assert len(detail["media"]) == 1
        assert detail["media"][0]["id"]
    rows = timeline(db=db, _user={})
    assert len(rows) == 2
    assert all(len(row["media"]) == 1 and row["media"][0]["id"] for row in rows)


def test_field_transitions_keep_history_and_manual_priority(db):
    from app.repositories import append_field_revision, create_profile
    profile = create_profile(db, 'person', 'Test', None)
    def put(value, source='wechat'):
        return append_field_revision(db, profile_id=profile['id'], field_key='bio', value=value,
                                     source_type=source, operation='manual_edit' if source=='manual' else 'import')
    for value in ('A', 'B', 'A'):
        assert put(value)[1]
    assert not put('A')[1]
    assert db.execute(text("SELECT count(*) FROM content_objects WHERE content_kind='field_value'")).scalar_one() == 2
    assert db.execute(text('SELECT count(*) FROM profile_field_revisions')).scalar_one() == 3
    put('My choice', 'manual')
    assert put('B')[1]
    assert put('A')[1]
    assert not put('A')[1]
    assert db.execute(text("SELECT current_source_type FROM profile_field_current")).scalar_one() == 'manual'
    assert db.execute(text('SELECT count(*) FROM profile_field_revisions WHERE is_conflict')).scalar_one() == 2


def test_bulk_moments_return_to_old_content_keeps_transition(db):
    for index, value in enumerate(('A', 'B', 'A', 'A')):
        project_moments(db, ImportBatchRequest(source='wechat', stream='moments',
            batch_id=str(index), idempotency_key=str(index), observed_at=datetime.now(UTC),
            records=[{'external_id':'p1','entity_type':'post','content':{'text':value}}]))
        db.commit()
    assert db.execute(text('SELECT count(*) FROM social_post_revisions')).scalar_one() == 3
    assert db.execute(text("SELECT count(*) FROM content_objects WHERE content_kind='post_body'")).scalar_one() == 2


def test_group_identity_reimport_and_membership_types(db):

    from app.imports import project_batch
    for index in range(2):
        project_batch(db, ImportBatchRequest(source='wechat',stream='groups', batch_id=str(index),
            idempotency_key=str(index), observed_at=datetime.now(UTC), records=[{
                'external_id':'room@chatroom', 'content':{'profile_type':'group','display_name':'Group'}}]))
        db.commit()
    assert db.execute(text('SELECT count(*) FROM profiles')).scalar_one() == 1
    assert db.execute(text('SELECT provider FROM identities')).scalar_one() == 'wechat_group'
    group = db.execute(text('SELECT id FROM profiles')).scalar_one()
    with pytest.raises(Exception, match='Membership requires'), db.begin_nested():
        db.execute(text('INSERT INTO group_memberships(group_profile_id,person_profile_id) VALUES (:id,:id)'), {'id':group})


def test_multiple_employment_facts_and_partial_import(db):
    from app.imports import project_batch
    def submit(name, facts):
        project_batch(db, ImportBatchRequest(source='linkedin',stream='profiles',batch_id=name,
            idempotency_key=name,observed_at=datetime.now(UTC),records=[{'external_id':'test',
            'content':{'display_name':'Test','employment':facts}}]))
        db.commit()
    facts=[{'id':'one','company':'A','title':'Engineer'},{'id':'two','company':'B','title':'Manager'}]
    submit('one',facts)
    submit('two',facts[::-1])
    assert db.execute(text('SELECT count(*) FROM profile_structured_current')).scalar_one()==2
    assert db.execute(text('SELECT count(*) FROM profile_employment_revisions')).scalar_one()==2
    submit('three',[{'id':'one','company':'A','title':'Senior Engineer'}])
    assert db.execute(text('SELECT count(*) FROM profile_structured_current')).scalar_one()==2
    assert db.execute(text('SELECT count(*) FROM profile_employment_revisions')).scalar_one()==3


def test_timeline_pagination_same_time_and_missing_dates(db):
    from app.imports import project_batch
    from app.routers.timeline import timeline
    records=[{'external_id':str(i),'entity_type':'post','occurred_at':'2026-01-01T00:00:00Z' if i<5 else None,'content':{'text':str(i)}} for i in range(8)]
    project_batch(db,ImportBatchRequest(source='wechat',stream='posts',batch_id='one',idempotency_key='one',observed_at=datetime.now(UTC),records=records))
    seen=[];cursor=None
    for _ in range(5):
        rows=timeline(limit=2,cursor=cursor,db=db,_user={})
        if not rows:break
        seen.extend(str(row['id']) for row in rows);cursor=rows[-1]['cursor']
    assert len(seen)==len(set(seen))==8


def test_unchanged_manifest_reuses_payload_and_replays(db, tmp_path):
    from app.config import get_settings
    from app.imports import hydrate_manifest, project_batch
    content={'text':'a long source body' * 1000}
    for name in ('one','two'):
        request=ImportBatchRequest(source='instagram',stream='posts',batch_id=name,idempotency_key=name,
            observed_at=datetime.now(UTC),records=[{'external_id':'post','entity_type':'post','content':content}])
        project_batch(db,request)
        db.commit()
    root=get_settings().raw_root
    assert len(list((root/'objects').rglob('*.json')))==1
    manifests=list((root/'instagram').rglob('*.json'))
    assert len(manifests)==2
    raw=json.loads(manifests[0].read_text())
    assert 'content' not in raw['records'][0]
    assert hydrate_manifest(raw)['records'][0]['content']==content
    project_batch(db,ImportBatchRequest.model_validate(hydrate_manifest(raw)))
    assert db.execute(text('SELECT count(*) FROM source_observations')).scalar_one()==2
    assert db.execute(text('SELECT count(*) FROM social_post_revisions')).scalar_one()==1


def test_membership_rejoin_is_history_and_type_change_is_rejected(db):
    from app.repositories import create_profile
    group=create_profile(db,'group','Room',None)['id']; person=create_profile(db,'person','Member',None)['id']
    args={'g':group,'p':person}
    db.execute(text("INSERT INTO group_memberships(group_profile_id,person_profile_id) VALUES(:g,:p)"),args)
    db.execute(text("UPDATE group_memberships SET left_at=now() WHERE group_profile_id=:g AND person_profile_id=:p"),args)
    db.execute(text("UPDATE group_memberships SET left_at=NULL WHERE group_profile_id=:g AND person_profile_id=:p"),args)
    db.execute(text("UPDATE group_memberships SET left_at=NULL WHERE group_profile_id=:g AND person_profile_id=:p"),args)
    assert db.execute(text('SELECT count(*) FROM group_membership_revisions')).scalar_one()==3
    with pytest.raises(Exception,match='repair memberships'),db.begin_nested():
        db.execute(text("UPDATE profiles SET profile_type='person' WHERE id=:g"),args)


def test_graph_uses_resolved_interactions_without_name_matching(db,tmp_path):
    from app.graph import graph_projection
    from app.imports import project_batch
    from app.repositories import create_profile
    first=create_profile(db,'person','Same name',None)['id']; second=create_profile(db,'person','Same name',None)['id']
    db.execute(text("INSERT INTO identities(provider,external_id,profile_id,status) VALUES('wechat','wx-first',:a,'active'),('wechat','wx-second',:b,'active')"),{'a':first,'b':second})
    project_batch(db,ImportBatchRequest(source='wechat',stream='posts',batch_id='g',idempotency_key='g',observed_at=datetime.now(UTC),records=[{
        'external_id':'post','profile_id':first,'entity_type':'post','content':{'text':'Hello','interactions':[
            {'external_id':'like','interaction_type':'like','author_external_id':'wx-second','author_name':'Same name'}]}}]))
    result=graph_projection(db,tmp_path)
    assert {n['profile_id'] for n in result['nodes']}=={str(first),str(second)}
    assert len(result['edges'])==1 and result['edges'][0]['direct']==1
    fan=create_profile(db,'person','Same name',None)['id']
    db.execute(text("INSERT INTO identities(provider,external_id,profile_id,status) VALUES('wechat','wx-fan',:p,'active')"),{'p':fan})
    for number,author in enumerate([first,second]):
        project_batch(db,ImportBatchRequest(source='wechat',stream='posts',batch_id=f'audience-{number}',
            idempotency_key=f'audience-{number}',observed_at=datetime.now(UTC),records=[{
            'external_id':f'audience-{number}','profile_id':author,'entity_type':'post','content':{'interactions':[
                {'external_id':f'fan-{number}','interaction_type':'like','author_external_id':'wx-fan'}]}}]))
    owners={str(first),str(second)}
    for focus in (None,str(first),str(second)):
        result=graph_projection(db,tmp_path,focus=focus)
        pair=next(edge for edge in result['edges'] if {result['nodes'][edge[k]]['profile_id'] for k in ('a','b')}==owners)
        assert pair['co']==1
        assert all(node['facts'][4]==1 for node in result['nodes'] if node['profile_id'] in owners)


def test_personal_graph_shared_groups_exclude_former_archived_and_large_groups(db,tmp_path):
    from app.graph import graph_projection
    from app.repositories import create_profile
    people=[create_profile(db,'person',f'Person {i}',None)['id'] for i in range(83)]
    focus,peer,former,archived=people[:4]
    groups=[create_profile(db,'group',f'Group {i}',None)['id'] for i in range(4)]
    for group,members in [(groups[0],people[:4]),(groups[1],[focus,peer]),
                          (groups[2],[focus,*people[3:]]),(groups[3],[peer,people[-1]])]:
        for member in members:
            db.execute(text('INSERT INTO group_memberships(group_profile_id,person_profile_id) VALUES(:g,:p)'),
                       {'g':group,'p':member})
    db.execute(text('UPDATE group_memberships SET left_at=now() WHERE person_profile_id=:p'),{'p':former})
    db.execute(text('UPDATE profiles SET archived_at=now() WHERE id=:p'),{'p':archived})
    result=graph_projection(db,tmp_path,focus=str(focus))
    assert {node['profile_id'] for node in result['nodes']}=={str(focus),str(peer)}
    assert len(result['edges'])==1
    assert result['edges'][0]['group']==2
    assert result['edges'][0]['direct']==0
    assert all(node['facts'][5]==2 for node in result['nodes'])


def test_typed_field_values_never_share_legacy_scalar_payloads(db):
    from app.content import canonical_json, get_or_create_content
    from app.repositories import append_field_revision, create_profile, list_field_history

    person=create_profile(db,'person','Typed history',None)['id']
    values=['0',0,'false',False,'null',None,'{"a":1}',{'a':1}]
    objects=[]
    for value in values:
        oid,digest=get_or_create_content(db,value=value,content_kind='field_value')
        objects.append(oid)
        stored=db.execute(text('SELECT payload_json FROM content_objects WHERE id=:id'),{'id':oid}).scalar_one()
        assert canonical_json(stored)==canonical_json(value)
        assert get_or_create_content(db,value=value,content_kind='field_value')==(oid,digest)
    assert len(set(objects))==len(values)
    for value in ['0',0,'0','0']:
        append_field_revision(db,profile_id=person,field_key='counter',value=value,source_type='wechat',operation='import')
    history=list_field_history(db,person,'counter')
    assert [canonical_json(r['value']) for r in history]==[b'"0"',b'0',b'"0"']
    assert db.execute(text("SELECT count(*) FROM content_objects WHERE content_kind='field_value'")).scalar_one()==len(values)


def test_chatlog_search_chinese_and_retry(db,tmp_path):
    import hashlib
    import sqlite3

    from app.import_messages import ingest
    from app.repositories import create_profile
    from app.routers.messages import profile_messages
    person=create_profile(db,'person','Person',None)['id']
    db.execute(text("INSERT INTO identities(provider,external_id,profile_id,status) VALUES('wechat','wx-sample',:p,'active')"),{'p':person})
    db.commit()
    table='Msg_'+hashlib.md5(b'wx-sample').hexdigest()
    with sqlite3.connect(tmp_path/'message_0.db') as source:
        source.execute('CREATE TABLE Name2Id(user_name TEXT)');source.execute("INSERT INTO Name2Id VALUES('wx-sample')")
        source.execute(f'CREATE TABLE {table}(local_id INTEGER PRIMARY KEY,server_id INTEGER,create_time INTEGER,local_type INTEGER,real_sender_id INTEGER,message_content TEXT,compress_content TEXT)')
        source.executemany(f'INSERT INTO {table} VALUES(?,?,?,?,?,?,NULL)',[(1,1,1767225600,1,1,'明天上海见'),(2,2,1767225600,1,1,'hello'),
            (3,3,1546300800,1,1,'以前去上海')])
    assert ingest(db,tmp_path)['inserted']==3
    conversation_versions=db.execute(text('SELECT id,xmin::text FROM conversations ORDER BY id')).all()
    db.commit()
    assert ingest(db,tmp_path)['inserted']==0
    assert db.execute(text('SELECT id,xmin::text FROM conversations ORDER BY id')).all()==conversation_versions
    result=profile_messages(person,q='上海',db=db,_user={})
    assert [row['text'] for row in result]==['明天上海见','以前去上海']
    assert [row['occurred_at'].astimezone(UTC).year for row in result]==[2026,2019]
    assert [row['text'] for row in profile_messages(person,q='上海',month='2019-01',db=db,_user={})]==['以前去上海']
    first=profile_messages(person,limit=1,db=db,_user={})[0]
    second=profile_messages(person,before=first['occurred_at'],before_id=first['id'],limit=1,db=db,_user={})[0]
    assert first['id']!=second['id']
    with sqlite3.connect(tmp_path/'message_0.db') as source:
        source.execute(f'INSERT INTO {table} VALUES(4,4,1514764800,1,1,?,NULL)',('新增的旧年消息',))
    assert ingest(db,tmp_path)['inserted']==1
    assert ingest(db,tmp_path)['inserted']==0
    assert profile_messages(person,q='旧年',db=db,_user={})[0]['occurred_at'].astimezone(UTC).year==2018
    content_count=db.execute(text("SELECT count(*) FROM content_objects WHERE content_kind='message'")).scalar_one()
    with sqlite3.connect(tmp_path/'message_0.db') as source:
        source.execute(f'INSERT INTO {table} VALUES(5,5,1767225601,1,1,?,NULL)',('明天上海见',))
    assert ingest(db,tmp_path)['inserted']==1
    assert db.execute(text("SELECT count(*) FROM content_objects WHERE content_kind='message'")).scalar_one()==content_count
    assert len(profile_messages(person,q='明天上海',db=db,_user={}))==2
    from app.routers.messages import conversation_messages, list_conversations
    db.execute(text('UPDATE conversations SET profile_id=NULL'))
    choices=list_conversations(db=db,_user={})
    assert choices[0]['profile_id'] is None
    assert conversation_messages(choices[0]['id'],q='上海',db=db,_user={})[0]['text']=='明天上海见'


def test_message_import_checkpoint_stop_and_small_batch_resume(db,tmp_path,monkeypatch):
    import hashlib
    import select
    import signal
    import sqlite3
    import subprocess
    import sys

    from app import import_messages, runtime_status
    saved_progress = []
    monkeypatch.setattr(runtime_status, 'progress', lambda **fields: saved_progress.append((fields, db.in_transaction())))
    table='Msg_'+hashlib.md5(b'wx-checkpoint').hexdigest()
    with sqlite3.connect(tmp_path/'message_0.db') as source:
        source.execute('CREATE TABLE Name2Id(user_name TEXT)')
        source.execute("INSERT INTO Name2Id VALUES('wx-checkpoint')")
        source.execute(f'CREATE TABLE {table}(local_id INTEGER PRIMARY KEY,server_id INTEGER,create_time INTEGER,local_type INTEGER,real_sender_id INTEGER,message_content TEXT,compress_content TEXT)')
        source.executemany(f'INSERT INTO {table} VALUES(?,?,?,?,?,?,NULL)',
                           [(n,n,1767225600+n,1,1,f'Message {n}') for n in range(1,6)])
    stopping=False
    def yield_after_commit(_delay):
        nonlocal stopping
        assert not db.in_transaction()
        stopping=True
    monkeypatch.setattr(import_messages.time,'sleep',yield_after_commit)
    result=import_messages.ingest(db,tmp_path,batch_size=2,stop_requested=lambda:stopping)
    assert result['stopped_at_checkpoint'] and result['inserted']==2
    assert saved_progress[0][0]['messages_inserted'] == 2
    assert not saved_progress[0][1], 'progress must describe a committed batch, never an in-flight transaction'
    assert db.execute(text('SELECT last_local_id FROM message_source_cursors')).scalar_one()==2
    assert db.execute(text('SELECT count(*) FROM message_search')).scalar_one()==2
    assert import_messages.ingest(db,tmp_path,batch_size=1,batch_pause=0)['inserted']==3
    assert import_messages.ingest(db,tmp_path,batch_size=3,batch_pause=0)['inserted']==0
    assert db.execute(text('SELECT count(*) FROM message_search')).scalar_one()==5
    assert db.execute(text("SELECT count(*) FROM content_objects WHERE content_kind='message'")).scalar_one()==5
    # A real SIGTERM must finish its committed cursor and signal incomplete
    # work to a shell pipeline, rather than starting the next migration stage.
    schema=db.execute(text('SELECT current_schema()')).scalar_one()
    url=db.get_bind().engine.url.update_query_dict({'options':f'-csearch_path={schema},public'})
    db.commit()
    with sqlite3.connect(tmp_path/'message_0.db') as source:
        source.executemany(f'INSERT INTO {table} VALUES(?,?,?,?,?,?,NULL)',
                           [(n,n,1767225600+n,1,1,f'Message {n}') for n in (6,7)])
    process=subprocess.Popen([sys.executable,'-m','app.import_messages','--root',str(tmp_path),
                              '--batch-size','1','--batch-pause','1'],
                             env={**os.environ,'DATABASE_URL':url.render_as_string(hide_password=False)},
                             stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
    try:
        assert select.select([process.stdout],[],[],15)[0], 'Importer did not commit its first batch'
        assert json.loads(process.stdout.readline())['batch_cursor_committed']
        process.send_signal(signal.SIGTERM)
        output,error=process.communicate(timeout=15)
        assert process.returncode==75,error
        assert json.loads(output.strip())['stopped_at_checkpoint']
    finally:
        if process.poll() is None:process.kill();process.wait()
    assert db.execute(text('SELECT last_local_id FROM message_source_cursors')).scalar_one()==6
    assert import_messages.ingest(db,tmp_path,batch_size=1,batch_pause=0)['inserted']==1


def test_message_search_extracts_readable_xml_without_cdn_signatures():
    from app.import_messages import search_text
    original='<msg><appmsg><title>上海约饭</title><des>明天下午</des><cdnthumburl>https://cdn.example/long-signature</cdnthumburl><md5>opaquehash</md5></appmsg></msg>'
    assert search_text(original)=='上海约饭\n明天下午'
    assert search_text('wxid_sender:\n'+original)=='上海约饭\n明天下午'
    assert search_text('plain 上海原文')=='plain 上海原文'
    assert search_text('<not valid xml')=='<not valid xml'


def test_existing_xml_search_projection_compacts_without_changing_message_archive(db,monkeypatch):
    from contextlib import nullcontext

    from app import compact_message_search
    conversation=db.execute(text("INSERT INTO conversations(conversation_type,external_id) VALUES('direct','test-xml') RETURNING id")).scalar_one()
    body='<msg><appmsg><title>上海约饭</title><md5>opaque</md5></appmsg></msg>'
    db.execute(text('INSERT INTO message_search(message_id,occurred_at,conversation_id,body) VALUES(:id,now(),:conversation,:body)'),
               {'id':uuid4(),'conversation':conversation,'body':body})
    monkeypatch.setattr(compact_message_search,'SessionLocal',lambda:nullcontext(db))
    compact_message_search.main();compact_message_search.main()
    assert db.execute(text('SELECT body FROM message_search')).scalar_one()=='上海约饭'


def test_archive_watcher_snapshots_changes_and_skips_unchanged(db,tmp_path,monkeypatch):
    monkeypatch.setattr("app.runtime_status.record", lambda *args, **kwargs: None)
    import sqlite3
    from contextlib import nullcontext

    from app import sync_archives
    source=tmp_path/'chatlog';(source/'sns').mkdir(parents=True)
    with sqlite3.connect(source/'sns/sns.db') as snapshot:
        snapshot.execute('CREATE TABLE SnsTimeLine(tid TEXT,user_name TEXT,content TEXT)')
        snapshot.execute('INSERT INTO SnsTimeLine VALUES(?,?,?)',('p','wx','<root><TimelineObject><id>p</id><contentDesc>Hello</contentDesc><createTime>1767225600</createTime></TimelineObject></root>'))
    monkeypatch.setenv('CHATLOG_DB_ROOT',str(source));monkeypatch.setenv('STAGING_ROOT',str(tmp_path/'staging'))
    monkeypatch.setenv('MEDIA_ROOT',str(tmp_path/'media'));monkeypatch.setenv('SNS_CACHE_ROOT',str(tmp_path/'cache'))
    monkeypatch.setattr(sync_archives,'SessionLocal',lambda:nullcontext(db))
    sync_archives.cycle()
    assert db.execute(text('SELECT count(*) FROM social_posts')).scalar_one()==1
    batches=db.execute(text('SELECT count(*) FROM import_batches')).scalar_one()
    sync_archives.cycle()
    assert db.execute(text('SELECT count(*) FROM import_batches')).scalar_one()==batches


def test_single_and_bulk_post_projection_match(db):
    from app.imports import project_batch
    content={'text':'post','interactions':[{'interaction_type':'comment','external_id':'c1','author_external_id':'unknown','text':'正文','occurred_at':1767225600}]}
    request=ImportBatchRequest(source='wechat',stream='moments',batch_id='one',idempotency_key='one',observed_at=datetime.now(UTC),records=[{'external_id':'p','entity_type':'post','content':content}])
    project_batch(db,request);db.commit()
    expected=db.execute(text('SELECT interaction_type,external_id,author_external_id,metadata,current_content_hash FROM social_interactions')).mappings().one()
    project_moments(db,request.model_copy(update={'batch_id':'two','idempotency_key':'two'}));db.commit()
    actual=db.execute(text('SELECT interaction_type,external_id,author_external_id,metadata,current_content_hash FROM social_interactions')).mappings().one()
    assert dict(expected)==dict(actual)
    assert db.execute(text('SELECT count(*) FROM social_post_revisions')).scalar_one()==1


def test_totp_setup_requires_verification_before_enabling(db):
    import pyotp

    from app.routers.auth import totp_setup, totp_verify
    from app.schemas import TotpCode
    user_id=db.execute(text("INSERT INTO app_users(email,password_hash) VALUES('totp@example.test','test') RETURNING id")).scalar_one()
    user={'id':user_id,'email':'totp@example.test'}
    setup=totp_setup(db,user)
    assert db.execute(text('SELECT totp_secret FROM app_users')).scalar_one() is None
    result=totp_verify(TotpCode(code=pyotp.TOTP(setup['secret']).now()),db,user)
    assert len(result['recovery_codes'])==8
    assert db.execute(text('SELECT totp_secret IS NOT NULL AND pending_totp_secret IS NULL FROM app_users')).scalar_one()
    assert db.execute(text('SELECT count(*) FROM auth_recovery_codes')).scalar_one()==8


def test_same_url_media_changes_preserve_versions_and_reuse_bytes(db, tmp_path):
    from app.remote_media import needs_download
    payload={'url':'https://media.licdn.com/stable-avatar','entity_type':'profile','entity_id':str(uuid4()),'role':'linkedin_avatar','revalidate':True,'check_day':'2030-01-02'}
    ids=[]
    for sha in ('a'*64,'b'*64,'a'*64):
        ids.append(materialize_image(db,payload,tmp_path,(sha,str(tmp_path/sha),10,'image/jpeg')))
    assert ids[0]==ids[2] and ids[0]!=ids[1]
    assert db.execute(text('SELECT count(*) FROM media_assets')).scalar_one()==2
    assert db.execute(text('SELECT count(*) FROM media_source_revisions')).scalar_one()==3
    assert db.execute(text('SELECT count(*) FROM media_links')).scalar_one()==1
    assert db.execute(text('SELECT media_id FROM media_manifest')).scalar_one()==UUID(ids[2])
    assert needs_download(db,payload)
    db.execute(text("UPDATE remote_media_sources SET checked_at='2030-01-02T12:00:00Z'"))
    assert not needs_download(db,payload)


def test_identity_repair_removes_valid_looking_memberships_before_correcting_type(db,tmp_path):
    from app.repair_identity import repair
    from app.repositories import append_field_revision
    actual_group=db.execute(text("INSERT INTO profiles(profile_type,display_name) VALUES('person','Legacy group') RETURNING id")).scalar_one()
    container_group=db.execute(text("INSERT INTO profiles(profile_type,display_name) VALUES('group','Existing group') RETURNING id")).scalar_one()
    person=db.execute(text("INSERT INTO profiles(profile_type,display_name) VALUES('person','Member') RETURNING id")).scalar_one()
    db.execute(text("INSERT INTO group_memberships(group_profile_id,person_profile_id,role) VALUES(:g,:p,'member')"),{'g':container_group,'p':actual_group})
    append_field_revision(db,profile_id=actual_group,field_key='monica.custom_fields',value={'微信群 ID':'123@chatroom'},source_type='monica',operation='import')
    db.execute(text("INSERT INTO identities(provider,external_id,profile_id) VALUES('wechat','wxid_member',:p)"),{'p':person})
    (tmp_path/'chatlog_extract.json').write_text(json.dumps({'contacts':[{'username':'wxid_member'}],'rooms':[{'id':'123@chatroom','members':{'wxid_member':'Member'}}]}))
    repair(db,tmp_path,True)
    assert db.execute(text('SELECT profile_type FROM profiles WHERE id=:id'),{'id':actual_group}).scalar_one()=='group'
    assert db.execute(text('SELECT person_profile_id FROM group_memberships WHERE group_profile_id=:id'),{'id':actual_group}).scalar_one()==person
    assert db.execute(text('SELECT count(*) FROM group_memberships WHERE person_profile_id=:id'),{'id':actual_group}).scalar_one()==0
    before=db.execute(text('SELECT count(*) FROM group_membership_revisions')).scalar_one()
    repair(db,tmp_path,True)
    assert db.execute(text('SELECT count(*) FROM group_membership_revisions')).scalar_one()==before


def test_source_aggregate_snapshots_do_not_become_homepage_events(db):
    from app.routers.timeline import timeline
    db.execute(text("""INSERT INTO activities(activity_type,occurred_at,title,body) VALUES
        ('wechat_direct_summary',now(),'Private message aggregate',:body),
        ('wechat_group_summary',now(),'Group aggregate',:body),
        ('note',now(),'Lunch together','A real personal note')"""),{'body':'{"n":0}'})
    rows=timeline(db=db,_user={})
    assert [r['title'] for r in rows]==['Lunch together']
    assert db.execute(text('SELECT count(*) FROM activities')).scalar_one()==3
def test_manual_records_preserve_revisions_participants_and_task_state(db):
    from app.main import create_activity, edit_activity, list_activities, set_activity_state
    from app.repositories import create_profile
    from app.schemas import ActivityCreate, ActivityState
    one=create_profile(db,'person','One',None)['id'];two=create_profile(db,'person','Two',None)['id']
    value=ActivityCreate(activity_type='task',occurred_at=datetime.now(UTC),title='Follow up',body='A',
                         participant_profile_ids=[one,two],due_at=datetime.now(UTC))
    activity=create_activity(value,db=db,user={'id':None})['id']
    assert len(list_activities(profile_id=one,db=db,_user={})[0]['participants'])==2
    for body in ['B','A','A']:
        edit_activity(UUID(activity),value.model_copy(update={'body':body}),db=db,_user={})
    assert db.execute(text('SELECT count(*) FROM activity_revisions')).scalar_one()==3
    set_activity_state(UUID(activity),ActivityState(state='done'),db=db,_user={})
    set_activity_state(UUID(activity),ActivityState(state='done'),db=db,_user={})
    assert db.execute(text('SELECT count(*) FROM activity_revisions')).scalar_one()==4
    assert list_activities(profile_id=two,db=db,_user={})[0]['state']=='done'


def test_header_manual_field_history_and_group_relation_guard(db):
    from fastapi import HTTPException

    from app.main import create_relationship, edit_field
    from app.repositories import create_profile
    from app.schemas import FieldEdit, RelationshipCreate
    person=create_profile(db,'person','Name',None)['id'];group=create_profile(db,'group','Room',None)['id']
    for value in ['Name','Changed','Name']:
        edit_field(person,FieldEdit(field_key='display_name',value=value),db=db,user={'id':None})
    assert db.execute(text('SELECT display_name FROM profiles WHERE id=:id'),{'id':person}).scalar_one()=='Name'
    assert db.execute(text("SELECT count(*) FROM profile_field_revisions WHERE field_key='display_name'")).scalar_one()==3
    with pytest.raises(HTTPException) as invalid:
        create_relationship(person,RelationshipCreate(to_profile_id=group,relationship_type='朋友'),db=db,user={'id':None})
    assert invalid.value.status_code==422


def test_social_statistics_uses_metadata_without_loading_message_bodies(db):
    from app.social_analytics import social_analytics
    result=social_analytics(db)
    assert set(result['providers'])=={'wechat','instagram','linkedin'}
    assert result['providers']['wechat']['posts']==0
    assert result['providers']['wechat']['indexed_messages']==0
    assert result['providers']['linkedin']['monthly']==[]


def test_reconciliation_retains_return_to_previous_post_value(db):
    from app.reconcile_sources import project_differences
    for value in ('A','B','A'):
        report=project_differences(db,'instagram',[{'external_id':'returning','entity_type':'post','content':{'text':value}}],True)
        assert report['differences']==1
    assert db.execute(text('SELECT count(*) FROM social_post_revisions')).scalar_one()==3
    assert db.execute(text("SELECT count(*) FROM content_objects WHERE content_kind='post_body'")).scalar_one()==2
    assert project_differences(db,'instagram',[{'external_id':'returning','entity_type':'post','content':{'text':'A'}}],True)['differences']==0


def test_instagram_reconciliation_reports_independent_owner_gaps(db, tmp_path, monkeypatch):
    from app.reconcile_sources import instagram

    path=tmp_path/'missing'/'post_UTC_2026.json'
    path.parent.mkdir()
    path.write_text('{}')
    monkeypatch.setattr('app.source_projection.enabled',lambda *_:True)
    monkeypatch.setattr('app.instagram_posts.read_post',lambda _:
        ({'id':'post-1','owner':{'id':'123','username':'missing'}},
         {'published_at':'2026-09-20T00:00:00Z','text':'post'}))
    report=instagram(db,tmp_path)
    assert report['missing_profile']==1
    assert report['missing_owner_ids']==['123']


def test_wechat_reply_target_and_mentions_keep_source_comment_identity(db, tmp_path):
    import xml.etree.ElementTree as ET

    from app.graph import circle_projection
    from app.moments_posts import moments_interactions as _moments_interactions

    people = {}
    for external in ('owner', 'commenter', 'recipient'):
        people[external] = db.execute(text("INSERT INTO profiles(profile_type,display_name) VALUES('person',:name) RETURNING id"), {'name':external}).scalar_one()
        db.execute(text("INSERT INTO identities(provider,external_id,profile_id) VALUES('wechat',:external,:profile)"), {'external':external,'profile':people[external]})
    root = ET.fromstring('''<SnsDataItem><TimelineObject><username>owner</username><createTime>1700000000</createTime></TimelineObject>
        <LocalExtraInfo><comment_user_list><user_comment><username>commenter</username>
        <comment_64id>0</comment_64id><comment_id>8</comment_id><content>Hello</content>
        <ref_comment_64id>0</ref_comment_64id><ref_comment_id>7</ref_comment_id><ref_username>recipient</ref_username>
        </user_comment></comment_user_list><with_user_list><user_comment><username>recipient</username>
        <create_time>0</create_time></user_comment></with_user_list></LocalExtraInfo></SnsDataItem>''')
    rows = _moments_interactions(root, 'source-post')
    assert rows[0]['interaction_type'] == 'reply'
    assert rows[0]['ref_external_id'] == '7'
    assert rows[0]['external_id'] == 'source-post:comment:8'
    assert rows[1]['author_external_id'] == 'owner'
    assert rows[1]['target_external_id'] == 'recipient'
    assert rows[1]['occurred_at'].startswith('2023-11-14')
    old_comment = {**rows[0], 'interaction_type':'comment', 'ref_external_id':'0'}
    old_comment.pop('ref_author_external_id')
    for batch, interactions in [('old',[old_comment]), ('fixed',rows), ('retry',rows)]:
        project_moments(db,ImportBatchRequest(source='wechat',stream='moments',batch_id=batch,
            idempotency_key=batch,observed_at=datetime.now(UTC),records=[{'external_id':'source-post',
            'profile_id':str(people['owner']),'entity_type':'post','content':{'interactions':interactions}}]))
        db.commit()
    assert db.execute(text('SELECT count(*) FROM social_interactions')).scalar_one() == 2
    assert db.execute(text('SELECT count(*) FROM social_post_revisions')).scalar_one() == 2
    graph = circle_projection(db, tmp_path, people['recipient'])
    neighbors = {n['id']:n for n in graph['neighbors']}
    assert neighbors[str(people['commenter'])]['in'] == [0,0,1,0]
    assert neighbors[str(people['owner'])]['in'] == [0,0,0,1]
    owner_graph = circle_projection(db,tmp_path,people['owner'])
    commenter = next(n for n in owner_graph['neighbors'] if n['id']==str(people['commenter']))
    assert commenter['in'] == [0,1,0,0]


def test_archive_snapshot_survives_collector_cache_changes(db, tmp_path, monkeypatch):
    from app import archive_media
    from app.archive_media import register_file

    source = tmp_path / 'collector.bin'
    first_bytes = b'\xff\xd8\xff' + b'first-photo' * 100
    source.write_bytes(first_bytes)
    first = register_file(db, source, tmp_path / 'media', 'wechat')
    db.commit()
    # A checkpoint reuses the immutable copy without reopening the collector's
    # image. If that copy goes missing, the next scan must restore it.
    with monkeypatch.context() as context:
        context.setattr(archive_media,'copy_media_snapshot',lambda *args:pytest.fail('Unchanged cache image copied again'))
        assert register_file(db, source, tmp_path / 'media', 'wechat') == first
    first_path = db.execute(text('SELECT object_path FROM media_assets WHERE id=:id'), {'id':first}).scalar_one()
    Path(first_path).unlink()
    assert register_file(db, source, tmp_path / 'media', 'wechat') == first
    assert Path(first_path).read_bytes()==first_bytes
    source.write_bytes(b'\xff\xd8\xff' + b'second-photo' * 100)
    second = register_file(db, source, tmp_path / 'media', 'wechat')
    db.commit()
    assert second != first
    source.unlink()
    first_path = db.execute(text('SELECT object_path FROM media_assets WHERE id=:id'), {'id':first}).scalar_one()
    assert Path(first_path).read_bytes() == first_bytes
    assert db.execute(text('SELECT count(*) FROM media_assets')).scalar_one() == 2


def test_moments_cache_audit_resume_and_original_upgrade(db,tmp_path):
    import hashlib
    import sqlite3

    from app.archive_media import moments_cache
    from app.imports import project_batch

    sns=tmp_path/'sns.db';cache=tmp_path/'cache';cache.mkdir()
    with sqlite3.connect(sns) as source:
        source.execute('CREATE TABLE SnsTimeLine(tid TEXT,content TEXT)')
        for n in range(2):
            source.execute('INSERT INTO SnsTimeLine VALUES(?,?)',(str(n),f'<root><TimelineObject><id>p{n}</id><ContentObject><mediaList><media><url key="key">https://media.test/original-{n}</url><thumb>https://media.test/thumb-{n}</thumb></media></mediaList></ContentObject></TimelineObject></root>'))
    project_batch(db,ImportBatchRequest(source='wechat',stream='moments',batch_id='cache',idempotency_key='cache',observed_at=datetime.now(UTC),
        records=[{'external_id':f'p{n}','entity_type':'post','content':{'text':f'Photo {n}'}} for n in range(2)]));db.commit()
    def photo(url,value):
        (cache/(hashlib.md5((url+'|key').encode()).hexdigest()+'.bin')).write_bytes(b'\xff\xd8\xff'+value)
    photo('https://media.test/thumb-0',b'thumbnail')
    photo('https://media.test/original-1',b'original one')
    report=moments_cache(db,sns,cache,tmp_path/'media',audit=True)
    assert report['source_media']==report['cache_matches']==2
    assert db.execute(text('SELECT count(*) FROM media_assets')).scalar_one()==0
    assert not (tmp_path/'media').exists()
    assert moments_cache(db,sns,cache,tmp_path/'media',limit=1)['added_links']==1
    assert moments_cache(db,sns,cache,tmp_path/'media')['added_links']==1
    assert db.execute(text('SELECT count(*) FROM media_assets')).scalar_one()==2
    assert db.execute(text('SELECT count(*) FROM media_links')).scalar_one()==2
    repeated=moments_cache(db,sns,cache,tmp_path/'media')
    assert repeated['linked_media']==2 and repeated['added_links']==0
    assert db.execute(text('SELECT count(*) FROM media_links')).scalar_one()==2
    photo('https://media.test/original-0',b'original zero')
    moments_cache(db,sns,cache,tmp_path/'media')
    original_hash=hashlib.sha256(b'\xff\xd8\xfforiginal zero').hexdigest()
    assert db.execute(text('SELECT count(*) FROM media_assets WHERE sha256=:hash'),{'hash':original_hash}).scalar_one()==1
    assert db.execute(text('SELECT count(*) FROM media_links')).scalar_one()==2
    assert db.execute(text('SELECT count(*) FROM media_assets')).scalar_one()==3
    assert db.execute(text("SELECT count(*) FROM media_source_revisions WHERE source_url='https://media.test/original-0'")).scalar_one()==2


def test_lightweight_social_counters_follow_source_changes(db):
    from app.reconcile_sources import project_differences
    from app.social_analytics import social_analytics

    for likes, comments in [(8,2),(9,3),(8,2)]:
        project_differences(db,'instagram',[{'external_id':'counted','entity_type':'post',
            'content':{'text':'Photo','like_count':likes,'comment_count':comments}}],True)
        stats=social_analytics(db)['providers']['instagram']
        assert (stats['posts'],stats['likes'],stats['comments']) == (1,likes,comments)
    assert db.execute(text('SELECT count(*) FROM social_post_revisions')).scalar_one() == 3


def test_tag_directory_counts_distinct_active_profiles_and_types(db):
    from app.main import list_tags
    people=[uuid4() for _ in range(3)];tag=uuid4()
    for pid,kind,archived in zip(people,['person','group','person'],[False,False,True]):
        db.execute(text("INSERT INTO profiles(id,profile_type,display_name,archived_at) VALUES(:id,:kind,'Same name',CASE WHEN :archived THEN now() END)"),{'id':pid,'kind':kind,'archived':archived})
    db.execute(text("INSERT INTO tags(id,name) VALUES(:id,'School')"),{'id':tag})
    for pid in people:db.execute(text("INSERT INTO tag_memberships(tag_id,profile_id,source_type) VALUES(:tag,:pid,'manual')"),{'tag':tag,'pid':pid})
    assert list_tags(None,db,{})[0]['people']==2
    assert list_tags('person',db,{})[0]['people']==1
    assert list_tags('group',db,{})[0]['people']==1


def test_location_index_tracks_source_edits_and_rebuilds_without_source_changes(db,tmp_path):
    from app.graph import graph_projection
    from app.location_atlas import atlas
    from app.location_index import rebuild
    from app.routers.locations import profile_locations
    person=db.execute(text("INSERT INTO profiles(profile_type,display_name) VALUES('person','Map person') RETURNING id")).scalar_one()
    for label,lat,lon in [('A',7.6178,47.4845),('B',-4.15626,55.92043)]:
        project_moments(db,ImportBatchRequest(source='wechat',stream='moments',batch_id=label,idempotency_key=label,observed_at=datetime.now(UTC),
            records=[{'external_id':'map-post','profile_id':str(person),'entity_type':'post','content':{'raw_xml_sha256':'original-xml','text':'A real place','location':{'latitude':lat,'longitude':lon}}}]))
        db.commit()
        locations=profile_locations(person,db,None)
        assert locations['points'][0]['latitude']==lon
        assert locations['points'][0]['longitude']==lat
        assert locations['points'][0]['events'][0]['url'].startswith(f'/profiles/{person}?tab=timeline&event=') if locations['points'][0]['events'] else True
    revisions=db.execute(text('SELECT count(*) FROM social_post_revisions')).scalar_one()
    db.execute(text('DELETE FROM social_post_locations'));db.commit()
    assert rebuild(db,batch_size=1)==1
    assert rebuild(db,batch_size=1)==0
    assert db.execute(text('SELECT count(*) FROM social_post_revisions')).scalar_one()==revisions
    result=atlas(db,provider='wechat',kind='post')
    assert result['post_records']==1 and result['points'][0]['latitude']==55.92043
    assert result['months']==list(result['points'][0]['months'])
    # Map projection does not create relationship edges.
    assert graph_projection(db,tmp_path)['edges']==[]
    for name,latitude,longitude in [('empty','0','0'),('equator','31','0'),('meridian','0','51')]:
        project_moments(db,ImportBatchRequest(source='wechat',stream='moments',batch_id=name,idempotency_key=name,observed_at=datetime.now(UTC),
            records=[{'external_id':name,'profile_id':str(person),'entity_type':'post','content':{'raw_xml_sha256':name,
                'location':{'latitude':latitude,'longitude':longitude}}}]))
        db.commit()
    points=atlas(db,provider='wechat',kind='post')['points']
    assert {(p['latitude'],p['longitude']) for p in points}=={(55.92043,-4.15626),(0,31),(51,0)}
    assert profile_locations(person,db,None)['place_count']==3
    import json

    from app.routers.locations import location_metrics
    response=location_metrics('wechat',None,'post',db,None)
    assert json.loads(response.body)['points']==points


def test_repeated_post_observation_keeps_location_row_until_content_changes(db):
    from app.imports import project_batch
    def observe(batch,latitude):
        project_batch(db,ImportBatchRequest(source='linkedin',stream='posts',batch_id=batch,
            idempotency_key=batch,observed_at=datetime.now(UTC),records=[{
                'external_id':'observed-location','entity_type':'post',
                'content':{'text':'A real post','location':{'latitude':latitude,'longitude':20}}}]))
        db.commit()
        return db.execute(text('SELECT xmin::text,location FROM social_post_locations')).one()
    first=observe('first',10)
    assert observe('same',10)==first
    second=observe('changed',11)
    assert second.location['latitude']==11 and second.xmin!=first.xmin
    third=observe('returned',10)
    assert third.location==first.location and third.xmin!=second.xmin
    assert db.execute(text('SELECT count(*) FROM social_post_revisions')).scalar_one()==3
    assert db.execute(text('SELECT count(*) FROM source_observations')).scalar_one()==4


def test_linkedin_watermark_uses_the_same_snapshot_when_collector_replaces_file(db, monkeypatch, tmp_path):
    from contextlib import nullcontext

    from app import sync_linkedin

    monkeypatch.setattr(sync_linkedin, 'SessionLocal', lambda: nullcontext(db))
    path = tmp_path / 'linkedin.json'
    original = {'profiles': [{'provider_user_id': 'stable-account', 'profile': {'about': 'A'}}]}
    replacement = {'profiles': [{'provider_user_id': 'stable-account', 'profile': {'about': 'B'}}]}
    path.write_text(json.dumps(original))
    loader = sync_linkedin.load_linkedin
    seen = []
    def replace_during_import(*args, **kwargs):
        seen.append(kwargs['export_snapshot'])
        path.write_text(json.dumps(replacement))
        return loader(*args, **kwargs)
    monkeypatch.setattr(sync_linkedin, 'load_linkedin', replace_during_import)
    sync_linkedin.sync(path)
    sync_linkedin.sync(path)
    assert seen == [original, replacement]
    sync_linkedin.sync(path)
    assert len(seen) == 2


def test_instagram_sparse_carousel_files_keep_source_slots_and_replay(db,tmp_path,monkeypatch):
    from app.archive_media import instagram_archive
    from app.imports import project_batch
    from app.remote_media import linked_media, merge_media

    monkeypatch.setattr('app.remote_media.download_image',lambda *args:pytest.fail('Archive must use local files'))
    directory=tmp_path/'collector'/'account';directory.mkdir(parents=True)
    metadata=directory/'2026-01-01_00-00-00_UTC_shortcode.json'
    children=[{'display_url':f'https://cdninstagram.com/image-{n}'} for n in range(1,13)]
    node={'id':'carousel','shortcode':'shortcode','edge_sidecar_to_children':{'edges':[{'node':child} for child in children]}}
    metadata.write_text(json.dumps({'node':node}))
    unused=directory/'2026-02-01_00-00-00_UTC_no-media.json';unused.write_text('{}')
    read_text=Path.read_text
    def read_only_media_metadata(path,*args,**kwargs):
        assert path!=unused, 'Media scan reopened a post with no local files'
        return read_text(path,*args,**kwargs)
    monkeypatch.setattr(Path,'read_text',read_only_media_metadata)
    for slot in (2,10):
        metadata.with_name(metadata.stem+f'_{slot}.jpg').write_bytes(b'\xff\xd8\xff'+str(slot).encode())
    metadata.with_name(metadata.stem+'_unproven.jpg').write_bytes(b'\xff\xd8\xffunknown')
    remote=[{'source_url':child['display_url'],'media_type':'image'} for child in children]
    project_batch(db,ImportBatchRequest(source='instagram',stream='posts',batch_id='carousel',idempotency_key='carousel',observed_at=datetime.now(UTC),
        records=[{'external_id':'carousel','entity_type':'post','content':{'text':'Carousel','media':remote}}]));db.commit()
    first=instagram_archive(db,tmp_path/'collector',tmp_path/'media')
    assert first['linked_media']==first['added_links']==2
    assert first['unmapped_files']==1
    links=db.execute(text('SELECT role FROM media_links')).scalars().all()
    assert set(links)=={'media-2','media-10'}
    post=db.execute(text("SELECT id FROM social_posts WHERE external_id='carousel'")).scalar_one()
    merged=merge_media(remote,linked_media(db,[post])[str(post)])
    assert len(merged)==12
    assert [i+1 for i,item in enumerate(merged) if item.get('id')]==[2,10]
    for item in merged:
        if item.get('id'):
            path=db.execute(text('SELECT object_path FROM media_assets WHERE id=:id'),{'id':item['id']}).scalar_one()
            assert Path(path).is_file()
    assert instagram_archive(db,tmp_path/'collector',tmp_path/'media')['added_links']==0
    assert db.execute(text('SELECT count(*) FROM media_assets')).scalar_one()==2


def test_local_archive_stops_only_after_committed_post_and_resumes(db,tmp_path,monkeypatch):
    from app import archive_media
    from app.imports import project_batch

    directory=tmp_path/'collector'/'account';directory.mkdir(parents=True)
    for n in range(2):
        metadata=directory/f'2026-01-0{n+1}_00-00-00_UTC_code.json'
        metadata.write_text(json.dumps({'node':{'id':f'post-{n}','display_url':f'https://cdninstagram.com/photo-{n}'}}))
        metadata.with_suffix('.jpg').write_bytes(b'\xff\xd8\xff'+str(n).encode())
    project_batch(db,ImportBatchRequest(source='instagram',stream='posts',batch_id='stop-photos',idempotency_key='stop-photos',observed_at=datetime.now(UTC),
        records=[{'external_id':f'post-{n}','entity_type':'post','content':{'text':'Photo'}} for n in range(2)]));db.commit()
    stopping=False;commit=db.commit
    def committed():
        nonlocal stopping
        commit();stopping=True
    with monkeypatch.context() as patch:
        patch.setattr(db,'commit',committed)
        patch.setattr(archive_media,'ARCHIVE_COMMIT_POSTS',1)
        first=archive_media.instagram_archive(db,tmp_path/'collector',tmp_path/'media',lambda:stopping)
    assert first['stopped_at_checkpoint'] and first['added_links']==1
    assert not db.in_transaction()
    assert db.execute(text('SELECT count(*) FROM media_links')).scalar_one()==1
    assert archive_media.instagram_archive(db,tmp_path/'collector',tmp_path/'media')['added_links']==1
    assert archive_media.instagram_archive(db,tmp_path/'collector',tmp_path/'media')['added_links']==0


def test_local_archive_batches_durable_commits_across_posts(db,tmp_path,monkeypatch):
    from app import archive_media
    from app.imports import project_batch

    directory=tmp_path/'collector'/'account';directory.mkdir(parents=True)
    for n in range(6):
        metadata=directory/f'2026-01-0{n+1}_00-00-00_UTC_code.json'
        metadata.write_text(json.dumps({'node':{'id':f'batch-{n}','display_url':f'https://cdninstagram.com/batch-{n}'}}))
        metadata.with_suffix('.jpg').write_bytes(b'\xff\xd8\xff'+str(n).encode())
    project_batch(db,ImportBatchRequest(source='instagram',stream='posts',batch_id='batch-photos',idempotency_key='batch-photos',observed_at=datetime.now(UTC),
        records=[{'external_id':f'batch-{n}','entity_type':'post','content':{'text':'Photo'}} for n in range(6)]));db.commit()
    commits=[];commit=db.commit
    monkeypatch.setattr(db,'commit',lambda:(commits.append(1),commit()))
    monkeypatch.setattr(archive_media,'ARCHIVE_COMMIT_POSTS',4)
    report=archive_media.instagram_archive(db,tmp_path/'collector',tmp_path/'media')
    assert report['added_links']==6 and len(commits)==2


def test_recovered_moments_keeps_raw_hash_and_local_media_without_replay_duplicates(db,tmp_path):
    import hashlib
    import sqlite3

    from app.archive_media import moments_cache
    from app.reconcile_sources import moments

    xml='<Sns><TimelineObject><id>recovered</id><username>unlinked-author</username><createTime>1700000000</createTime><contentDesc>A&#3;B</contentDesc><ContentObject><mediaList><media><id>photo</id><type>2</type><url key="key">https://example.test/photo</url></media></mediaList></ContentObject></TimelineObject></Sns>'
    sns=tmp_path/'sns.db'
    with sqlite3.connect(sns) as source:
        source.execute('CREATE TABLE SnsTimeLine(tid TEXT,user_name TEXT,content TEXT)')
        source.execute('INSERT INTO SnsTimeLine VALUES(?,?,?)',('recovered','unlinked-author',xml))
    cache=tmp_path/'cache';cache.mkdir()
    cache.joinpath(hashlib.md5(b'https://example.test/photo|key').hexdigest()+'.bin').write_bytes(b'\xff\xd8\xffphoto')
    report=moments(db,sns,apply=True)
    assert report['recovered_xml']==1 and report['invalid_xml']==0 and report['differences']==1
    payload=db.execute(text("SELECT c.payload_json FROM social_posts p JOIN content_objects c ON c.id=p.content_object_id WHERE p.external_id='recovered'")).scalar_one()
    assert payload['text']=='A&#3;B'
    assert payload['raw_xml_sha256']==hashlib.sha256(xml.encode()).hexdigest()
    assert payload['xml_literal_character_references']=={'&#3;':1}
    assert db.execute(text('SELECT count(*) FROM profiles')).scalar_one()==0
    assert moments_cache(db,sns,cache,tmp_path/'media')['added_links']==1
    assert moments(db,sns)['differences']==0
    assert moments_cache(db,sns,cache,tmp_path/'media')['added_links']==0
    assert db.execute(text('SELECT count(*) FROM social_post_revisions')).scalar_one()==1


def test_moments_link_repair_preserves_content_is_idempotent_and_exposes_card(db,tmp_path):
    import hashlib
    import sqlite3

    from app.content import get_or_create_content
    from app.repair_moments_links import repair
    from app.routers.timeline import timeline, timeline_detail
    xml='<Sns><TimelineObject><ContentObject><title>Article</title><contentUrl>https://example.test/article</contentUrl><type>3</type></ContentObject></TimelineObject></Sns>'
    sns=tmp_path/'sns.db'
    with sqlite3.connect(sns) as conn:
        conn.execute('CREATE TABLE SnsTimeLine(tid TEXT,content TEXT)')
        conn.execute('INSERT INTO SnsTimeLine VALUES(?,?)',('1',xml))
    original={'text':'My comment','media':[], 'source_tid':'1','raw_xml_sha256':hashlib.sha256(xml.encode()).hexdigest()}
    cid,digest=get_or_create_content(db,value=original,content_kind='post_body')
    pid=db.execute(text("INSERT INTO social_posts(provider,external_id,content_object_id,current_content_hash) VALUES('wechat','one',:c,:h) RETURNING id"),{'c':cid,'h':digest}).scalar_one()
    db.commit()
    assert repair(db,sns)['matched']==1
    assert repair(db,sns,apply=True)['updated']==1
    assert repair(db,sns,apply=True)['updated']==0
    detail=timeline_detail(pid,db=db,_user={})
    assert detail['content']['shared_link']['title']=='Article'
    assert {k:v for k,v in detail['content'].items() if k!='shared_link'}==original
    assert timeline(db=db,_user={})[0]['shared_link']['url']=='https://example.test/article'
    assert db.execute(text('SELECT count(*) FROM social_post_revisions')).scalar_one()==1
    assert db.execute(text('SELECT payload_json FROM content_objects WHERE id=:id'),{'id':cid}).scalar_one()==original
    with sqlite3.connect(sns) as conn:
        conn.execute("UPDATE SnsTimeLine SET content=replace(content,'Article','Changed')")
    assert repair(db,sns,apply=True)['source_mismatch']==1


def test_cached_video_poster_keeps_video_identity():
    from app.remote_media import merge_media
    items=merge_media([{'media_type':'video','source_url':'https://example.test/v','thumbnail_url':'https://example.test/p'}],
                      [{'id':'poster','media_type':'image/jpeg','source_url':'https://example.test/p'}])
    assert items[0]['source_media_type']=='video'
    assert items[0]['media_type']=='image/jpeg'


def test_data_versions_advance_only_when_rows_change(db):
    from app.data_versions import current
    before=current(db,('profiles','tags'))
    person=db.execute(text("INSERT INTO profiles(profile_type,display_name) VALUES('person','Versioned') RETURNING id")).scalar_one()
    after_insert=current(db,('profiles','tags'))
    assert after_insert['profiles']==before['profiles']+1 and after_insert['tags']==before['tags']
    db.execute(text("UPDATE profiles SET display_name='Versioned' WHERE id=:id AND display_name<>'Versioned'"),{'id':person})
    assert current(db,('profiles',))['profiles']==after_insert['profiles']
    db.execute(text("UPDATE profiles SET display_name='Renamed' WHERE id=:id"),{'id':person})
    assert current(db,('profiles',))['profiles']==after_insert['profiles']+1


def test_contact_stats_refresh_only_dirty_conversations_and_follow_relinks(db):
    from app.contact_stats import refresh
    them,other=uuid4(),uuid4()
    for pid in (them,other):db.execute(text("INSERT INTO profiles(id,profile_type,display_name) VALUES(:id,'person','Stats')"),{'id':pid})
    account=db.execute(text("INSERT INTO source_accounts(id,provider,external_id,object_kind,display_name) VALUES(gen_random_uuid(),'wechat','wxid_stats','person','Stats') RETURNING id")).scalar_one()
    me=db.execute(text("INSERT INTO source_accounts(id,provider,external_id,object_kind,display_name) VALUES(gen_random_uuid(),'wechat','wxid_me','person','Me') RETURNING id")).scalar_one()
    conversation=db.execute(text("""INSERT INTO conversations(conversation_type,profile_id,external_id,source_account_id)
        VALUES('direct',:pid,'direct:wxid_stats',:account) RETURNING id"""),{'pid':them,'account':account}).scalar_one()
    rows=[('2026-01-05T12:00:00Z',account,'hello'),('2026-01-06T12:00:00Z',me,'hi back'),('2026-03-01T12:00:00Z',account,'  latest\n  words  ')]
    for n,(at,sender,body) in enumerate(rows):
        message=db.execute(text("""INSERT INTO message_events(conversation_id,occurred_at,message_type,source_record_id,sender_account_id)
            VALUES(:c,:at,'1',:r,:s) RETURNING id"""),{'c':conversation,'at':at,'r':f'r{n}','s':sender}).scalar_one()
        db.execute(text("INSERT INTO message_search(message_id,occurred_at,conversation_id,body) VALUES(:m,:at,:c,:b)"),{'m':message,'at':at,'c':conversation,'b':body})
    assert refresh(db)>=1 and refresh(db)==0
    months=db.execute(text("SELECT to_char(month,'YYYY-MM'),sent,received FROM profile_message_months WHERE profile_id=:p ORDER BY month"),{'p':them}).all()
    assert [tuple(m) for m in months]==[('2026-01',1,1),('2026-03',0,1)]
    stats=db.execute(text("SELECT direct_message_count,sent_count,received_count FROM profile_contact_stats WHERE profile_id=:p"),{'p':them}).one()
    assert tuple(stats)==(3,1,2)
    summary=db.execute(text("SELECT message_count,last_message_preview FROM conversations WHERE id=:c"),{'c':conversation}).one()
    assert tuple(summary)==(3,'latest words')
    db.execute(text("UPDATE conversations SET profile_id=:p WHERE id=:c"),{'p':other,'c':conversation})
    assert db.execute(text("SELECT count(*) FROM profile_contact_stats WHERE profile_id=:p"),{'p':other}).scalar_one()==1
    assert refresh(db)==1


def test_remote_revalidation_uses_utc_day_across_database_timezones(db, tmp_path):
    from app.remote_media import materialize_image, needs_download
    image_path = tmp_path/'daily-image'
    image_path.write_bytes(b'image')
    payload = {'url': 'https://media.licdn.com/utc-day', 'entity_type': 'social_post',
               'entity_id': uuid4(), 'role': 'media-1', 'revalidate': True, 'check_day': '2026-10-17'}
    materialize_image(db, payload, tmp_path, ('c'*64, str(image_path), 5, 'image/jpeg'))
    db.execute(text("UPDATE remote_media_sources SET checked_at='2026-10-17T00:05:00Z' WHERE source_url=:url"), payload)
    assert not needs_download(db, payload)
    assert needs_download(db, {**payload, 'check_day': '2026-10-18'})
