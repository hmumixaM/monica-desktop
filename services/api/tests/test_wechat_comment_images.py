"""Comment pictures must survive import without becoming the post's own photos."""
import hashlib
import sqlite3
from datetime import UTC, datetime

from sqlalchemy import text
from test_wechat_prefetch_policy import prefetch

from app.archive_media import moments_cache
from app.imports import project_batch
from app.moments_posts import moments_record
from app.routers.timeline import timeline_detail
from app.schemas import ImportBatchRequest

XML = '''<root><TimelineObject><id>post</id><username>owner</username>
<createTime>1792285046</createTime><ContentObject><type>2</type></ContentObject></TimelineObject>
<LocalExtraInfo><comment_user_list><user_comment><comment_id>227</comment_id>
<username>owner</username><content>精选答案</content><ref_comment_id>12</ref_comment_id>
<create_time>1792285876</create_time><imagelist><imageinfo>
<url>http://szmmsns.qpic.cn/mmcomment/example/0</url><token>token</token><key>123</key>
<thumb_url>http://szmmsns.qpic.cn/mmcomment/example/60</thumb_url>
<thumb_url_token>thumb-token</thumb_url_token><thumb_key>123</thumb_key>
<width>1178</width><height>180</height><media_id>photo-id</media_id>
</imageinfo></imagelist></user_comment></comment_user_list></LocalExtraInfo></root>'''


def test_comment_parser_and_prefetch_support_text_only_post_and_replies(tmp_path):
    record = moments_record(-42, 'owner', XML, None)
    assert record['content']['media'] == []
    reply = record['content']['interactions'][0]
    assert reply['external_id'] == 'post:comment:227'
    assert reply['interaction_type'] == 'reply'
    assert reply['media'][0]['width'] == 1178
    assert reply['media'][0]['height'] == 180
    assert reply['media'][0]['source_media_id'] == 'photo-id'
    database = tmp_path / 'sns.db'
    with sqlite3.connect(database) as db:
        db.execute('CREATE TABLE SnsTimeLine(tid INTEGER,user_name TEXT,content TEXT)')
        db.execute('INSERT INTO SnsTimeLine VALUES(?,?,?)', (-42, 'owner', XML))
    resources = prefetch.read_media(database, {'1','6','7','15','54'})
    assert len(resources) == 1
    media = resources[0]
    assert media.tids == (-42,)
    assert media.url == 'http://szmmsns.qpic.cn/mmcomment/example/0?token=token&idx=1'
    assert media.key == '123'
    assert list(prefetch.media_in_xml(-42, 'owner', XML.replace('<imagelist>', '<b_deleted>1</b_deleted><imagelist>'), '2')) == []
    assert prefetch.read_media(database, {'1'}, since=1792285047) == []


def test_cached_comment_image_repairs_old_import_and_stays_out_of_post_gallery(crm_db, tmp_path):
    db = crm_db
    record = moments_record(-42, 'owner', XML, None)
    # Simulate an existing import from the old parser, which omitted images.
    record.pop('repairs')
    record['content']['interactions'][0].pop('media')
    project_batch(db, ImportBatchRequest(source='wechat', stream='moments', batch_id='comments',
        idempotency_key='comments', observed_at=datetime.now(UTC), records=[record]))
    db.commit()
    source, cache = tmp_path/'sns.db', tmp_path/'cache'
    cache.mkdir()
    with sqlite3.connect(source) as sns:
        sns.execute('CREATE TABLE SnsTimeLine(tid INTEGER,content TEXT)')
        sns.execute('INSERT INTO SnsTimeLine VALUES(?,?)', (-42, XML))
    resource = next(prefetch.media_in_xml(-42,'owner',XML,'2'))
    name = hashlib.md5((resource.url+'|123').encode()).hexdigest()+'.bin'
    from PIL import Image
    Image.new('RGB',(1178,180),'white').save(cache/name,format='PNG')
    result = moments_cache(db,source,cache,tmp_path/'media', tids={-42},cache_names={name})
    assert result['added_links'] == 1
    post = db.execute(text("SELECT id FROM social_posts WHERE external_id='post'")).scalar_one()
    detail = timeline_detail(post,provider='wechat',db=db,_user={})
    assert detail['media'] == []
    assert detail['interactions'][0]['content']['text'] == '精选答案'
    image = detail['interactions'][0]['media'][0]
    assert image['id']
    assert image['media_type'] == 'image/png'
    assert moments_cache(db,source,cache,tmp_path/'media', tids={-42},cache_names={name})['added_links'] == 0


def test_comment_picture_thumbnail_fallback_uses_its_own_token():
    xml = XML.replace('<url>http://szmmsns.qpic.cn/mmcomment/example/0</url>', '')
    media = next(prefetch.media_in_xml(-42, 'owner', xml, '2'))
    assert media.url == 'http://szmmsns.qpic.cn/mmcomment/example/60?token=thumb-token&idx=1'
    assert media.key == '123'
    import xml.etree.ElementTree as ET

    from app.archive_media import fix_url
    from app.moments_posts import comment_images
    node = comment_images(ET.fromstring(xml).find('LocalExtraInfo/comment_user_list/user_comment'))[0]
    thumb = node.find('thumb')
    assert node.find('url') is None
    assert fix_url(thumb.text, thumb.get('token')) == media.url
