import asyncio
import io
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException, UploadFile
from PIL import Image
from sqlalchemy import text
from test_media_integration import db as db  # noqa: PLC0414

from app.main import create_activity, edit_activity, list_activities
from app.routers.media import (
    attach_profile_photos,
    profile_photos,
    remove_profile_photo,
    upload_media,
)
from app.routers.timeline import timeline, timeline_detail
from app.schemas import ActivityCreate, MediaSelection


def test_upload_links_edit_preservation_and_timeline(db,tmp_path,monkeypatch):
    from app.main import settings
    monkeypatch.setattr(settings,'media_root',tmp_path)
    content=io.BytesIO();Image.new('RGB',(12,18),'blue').save(content,format='PNG');content.seek(0)
    asset=asyncio.run(upload_media(UploadFile(filename='photo.png',file=content),db=db,_user={}))
    assert asset['media_type']=='image/png'
    mid=asset['id'];pid=uuid4()
    db.execute(text("INSERT INTO profiles(id,profile_type,display_name) VALUES(:id,'person','Upload test')"),{'id':pid});db.commit()
    attach_profile_photos(pid,MediaSelection(media_ids=[mid,mid]),db,{'id':None})
    assert len(profile_photos(pid,db,{}))==1
    data=ActivityCreate(activity_type='activity',occurred_at=datetime.now(UTC),title='Trip',participant_profile_ids=[pid],media_ids=[mid])
    aid=UUID(create_activity(data,db,{'id':None})['id'])
    assert list_activities(pid,db=db,_user={})[0]['media'][0]['id']==mid
    assert timeline(profile_id=pid,db=db,_user={})[0]['media'][0]['id']==str(mid)
    assert timeline_detail(aid,db=db,_user={})['media'][0]['id']==str(mid)
    edit_activity(aid,data.model_copy(update={'media_ids':None,'body':'edit only'}),db,{'id':None})
    assert len(timeline_detail(aid,db=db,_user={})['media'])==1
    edit_activity(aid,data.model_copy(update={'media_ids':[]}),db,{'id':None})
    assert timeline_detail(aid,db=db,_user={})['media']==[]
    assert profile_photos(pid,db,{})[0]['id']==str(mid)
    remove_profile_photo(pid,mid,db,{'id':None})
    assert profile_photos(pid,db,{})==[]
    assert db.execute(text('SELECT count(*) FROM media_assets WHERE id=:id'),{'id':mid}).scalar_one()==1
    assert list(tmp_path.rglob(asset['sha256']))


def test_documents_attach_to_profiles_and_records_but_only_download(db,tmp_path,monkeypatch):
    from app.main import settings
    from app.routers.media import download_media
    from app.routers.timeline import timeline_detail
    monkeypatch.setattr(settings,'media_root',tmp_path)
    page=asyncio.run(upload_media(UploadFile(filename='合同.html',file=io.BytesIO(b'<script>alert(1)</script>')),db=db,_user={}))
    pdf=asyncio.run(upload_media(UploadFile(filename='dir/简历.pdf',file=io.BytesIO(b'%PDF-1.7 test')),db=db,_user={}))
    blob=asyncio.run(upload_media(UploadFile(filename='notes',file=io.BytesIO(b'\x00\x01 raw')),db=db,_user={}))
    assert (page['media_type'],page['file_name'])==('text/html','合同.html')
    assert (pdf['media_type'],pdf['file_name'])==('application/pdf','简历.pdf')
    assert blob['media_type']=='application/octet-stream'
    photo=io.BytesIO();Image.new('RGB',(4,4),'red').save(photo,format='PNG');photo.seek(0)
    image=asyncio.run(upload_media(UploadFile(filename='face.png',file=photo),db=db,_user={}))
    pid=uuid4()
    db.execute(text("INSERT INTO profiles(id,profile_type,display_name) VALUES(:id,'person','Files')"),{'id':pid});db.commit()
    attach_profile_photos(pid,MediaSelection(media_ids=[page['id'],image['id']]),db,{'id':None})
    roles=dict(db.execute(text("SELECT media_id::text,role FROM media_links WHERE entity_id=:id"),{'id':pid}).all())
    assert roles=={str(page['id']):'manual_file',str(image['id']):'manual_photo'}
    assert {item['file_name'] for item in profile_photos(pid,db,{})}=={'合同.html','face.png'}
    remove_profile_photo(pid,page['id'],db,{'id':None})
    assert [item['id'] for item in profile_photos(pid,db,{})]==[str(image['id'])]
    data=ActivityCreate(activity_type='note',occurred_at=datetime.now(UTC),title='Contract',participant_profile_ids=[pid],media_ids=[pdf['id'],image['id']])
    aid=UUID(create_activity(data,db,{'id':None})['id'])
    assert [item['file_name'] for item in list_activities(pid,db=db,_user={})[0]['media']]==['简历.pdf','face.png']
    assert [item['id'] for item in timeline_detail(aid,db=db,_user={})['media']]==[str(image['id'])]
    request=type('Request',(),{'headers':{}})()
    assert download_media(page['id'],request,db).headers['content-disposition'].startswith('attachment;')
    assert download_media(pdf['id'],request,db).headers['content-disposition'].startswith('inline;')
    assert 'content-disposition' not in download_media(image['id'],request,db).headers


def test_reject_fake_photo_and_unknown_attachment(db,tmp_path,monkeypatch):
    from app.main import settings
    monkeypatch.setattr(settings,'media_root',tmp_path)
    with pytest.raises(HTTPException) as error:
        asyncio.run(upload_media(UploadFile(filename='bad.jpg',file=io.BytesIO(b'<script>alert(1)</script>')),db=db,_user={}))
    assert error.value.status_code==422
    assert not list((tmp_path/'.staging').glob('*'))
    with pytest.raises(HTTPException):
        create_activity(ActivityCreate(activity_type='activity',occurred_at=datetime.now(UTC),title='Bad',media_ids=[uuid4()]),db,{'id':None})
    assert db.execute(text('SELECT count(*) FROM activities')).scalar_one()==0
