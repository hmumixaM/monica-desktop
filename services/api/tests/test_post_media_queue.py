from test_media_integration import db as db  # noqa: PLC0414

from app import remote_media


def test_video_queue_refreshes_only_its_poster_in_a_separate_slot(monkeypatch):
    queued=[]
    monkeypatch.setattr(remote_media,'enqueue_media',lambda db,**payload:queued.append(payload))
    remote_media.enqueue_post_media(None,media={'media_type':'video','source_url':'https://cdninstagram.com/movie.mp4','thumbnail_url':'https://cdninstagram.com/poster.jpg','role':'video-1'},post_id='post',index=0)
    assert queued==[{'url':'https://cdninstagram.com/poster.jpg','entity_type':'social_post','entity_id':'post','role':'video-1-poster'}]


def test_video_without_poster_does_not_enqueue_video_bytes():
    class NoDatabaseCalls:
        def execute(self,*args,**kwargs):
            raise AssertionError('Missing poster must not queue a download')
    assert remote_media.enqueue_post_media(NoDatabaseCalls(),media={'media_type':'video/mp4','source_url':'https://cdninstagram.com/movie.mp4'},post_id='post',index=0) is False


def test_images_keep_their_existing_slot(monkeypatch):
    queued=[]
    monkeypatch.setattr(remote_media,'enqueue_media',lambda db,**payload:queued.append(payload))
    remote_media.enqueue_post_media(None,media={'media_type':'image','source_url':'https://cdninstagram.com/photo.jpg'},post_id='post',index=2)
    assert queued[0]['url'].endswith('/photo.jpg') and queued[0]['role']=='media-3'


def test_due_refresh_does_not_revalidate_archived_video_bytes(db):
    from uuid import uuid4

    from sqlalchemy import text

    post=uuid4()
    for i,mime in enumerate(('video/mp4','image/jpeg')):
        digest=str(i)*64;url=f'https://cdninstagram.com/asset-{i}'
        cid=db.execute(text("INSERT INTO content_objects(sha256,content_kind) VALUES(:hash,'media') RETURNING id"),{'hash':digest}).scalar_one()
        mid=db.execute(text("INSERT INTO media_assets(content_object_id,sha256,media_type,byte_length,object_path) VALUES(:cid,:hash,:mime,1,'/test') RETURNING id"),{'cid':cid,'hash':digest,'mime':mime}).scalar_one()
        db.execute(text("INSERT INTO media_manifest(source_url,entity_type,entity_id,role,state,media_id,checked_at) VALUES(:url,'social_post',:post,:role,'ready',:mid,now()-interval '8 days')"),{'url':url,'post':post,'role':'video-1' if i==0 else 'video-1-poster','mid':mid})
    assert remote_media.enqueue_due_media(db)==1
    queued=db.execute(text("SELECT payload FROM jobs WHERE job_type='media.download'")).scalars().all()
    assert len(queued)==1 and queued[0]['url']=='https://cdninstagram.com/asset-1'
    assert remote_media.enqueue_due_media(db)==0
