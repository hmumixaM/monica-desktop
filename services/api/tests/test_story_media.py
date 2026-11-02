from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import text
from test_post_media import asset, photo, video

from app import archive_media
from app.ai import post_media
from app.ai.queue import enqueue_spaced, enqueue_story_media
from app.ai.schemas import ImageReading, PostMediaReading
from app.config import get_settings
from app.routers.timeline import timeline_detail


def story(db, name, days=1):
    sha = name.encode().hex().ljust(64, '0')
    content = db.execute(text("""INSERT INTO content_objects(sha256,content_kind,byte_length,payload_json)
        VALUES(:sha,'json',1,'{"text":"Story 配文"}') RETURNING id"""), {"sha": sha}).scalar_one()
    return str(db.execute(text("""INSERT INTO social_stories(provider,external_id,content_object_id,occurred_at,current_content_hash)
        VALUES('instagram',:name,:content,:at,:sha) RETURNING id"""),
                          {"name": name, "content": content, "at": datetime.now(UTC) - timedelta(days=days),
                           "sha": sha}).scalar_one())


def test_import_queues_async_and_changed_story_is_read_once(crm_db, tmp_path, monkeypatch):
    monkeypatch.setenv('AI_ENABLED', 'true')
    get_settings.cache_clear()
    calls = []

    def vision(model, system, prompt, images, schema):
        calls.append(images)
        assert 'Story 配文' in prompt
        return PostMediaReading(summary='网球日常', items=[ImageReading(index=1, category='其他', text='cuuute', description='手拿网球')])

    monkeypatch.setattr(post_media, 'vision', vision)
    sid = story(crm_db, 'tennis')
    first = asset(crm_db, photo(tmp_path, 'tennis', 'yellow'), 'image/jpeg', 'tennis')
    archive_media.link(crm_db, first, sid, 'media-1', entity_type='social_story', retain=False)
    archive_media.link(crm_db, first, sid, 'media-1', entity_type='social_story', retain=False)
    assert calls == []
    assert crm_db.execute(text("SELECT count(*) FROM jobs WHERE job_type='ai.story_media_one'")).scalar_one() == 1
    crm_db.rollback()
    assert crm_db.execute(text('SELECT count(*) FROM social_stories')).scalar_one() == 0
    assert crm_db.execute(text('SELECT count(*) FROM jobs')).scalar_one() == 0

    sid = story(crm_db, 'tennis')
    first = asset(crm_db, tmp_path / 'tennis.jpg', 'image/jpeg', 'tennis')
    archive_media.link(crm_db, first, sid, 'media-1', entity_type='social_story', retain=False)
    crm_db.commit()
    assert post_media.read_story(crm_db, {'story_id': sid})['read'] is True
    assert post_media.read_story(crm_db, {'story_id': sid})['read'] is False
    assert len(calls) == 1
    detail = timeline_detail(UUID(sid), provider=None, db=crm_db, _user=None)
    assert detail['event_type'] == 'story'
    assert detail['media_reading']['summary'] == '网球日常'
    assert detail['media_reading']['items'][0]['text'] == 'cuuute'
    since = datetime.now(UTC) - timedelta(days=30)
    assert post_media.unread(crm_db, since, 'social_story') == []
    crm_db.execute(text("UPDATE jobs SET status='done' WHERE job_type='ai.story_media_one'"))
    second = asset(crm_db, photo(tmp_path, 'new', 'red'), 'image/jpeg', 'new')
    archive_media.link(crm_db, second, sid, 'media-2', entity_type='social_story', retain=False)
    crm_db.commit()
    assert crm_db.execute(text("SELECT count(*) FROM jobs WHERE status='queued' AND job_type='ai.story_media_one'")).scalar_one() == 1
    assert post_media.unread(crm_db, since, 'social_story') == [sid]
    assert post_media.read_story(crm_db, {'story_id': sid})['read'] is True
    assert len(calls) == 2


def test_story_videos_use_poster_or_frame_and_scan_finds_backlog(crm_db, tmp_path, monkeypatch):
    monkeypatch.setenv('AI_ENABLED', 'false')
    get_settings.cache_clear()
    sid = story(crm_db, 'video')
    for name, mime, role, path in (
        ('clip', 'video/mp4', 'media-1', video(tmp_path, 'clip')),
        ('poster', 'image/jpeg', 'thumbnail-1', photo(tmp_path, 'poster', 'blue')),
        ('bare', 'video/mp4', 'media-2', video(tmp_path, 'bare', seconds=1)),
        ('only', 'image/jpeg', 'thumbnail-3', photo(tmp_path, 'only', 'green')),
    ):
        archive_media.link(crm_db, asset(crm_db, path, mime, name), sid, role, entity_type='social_story', retain=False)
    selected = post_media.slots(crm_db, sid, 'social_story')
    assert [s['frame'] for s in selected] == [False, True, False]
    assert [s['kind'] for s in selected] == ['video'] * 3
    monkeypatch.setattr(post_media, 'vision', lambda *args: PostMediaReading(summary='视频', items=[]))
    assert post_media.scan(crm_db, {}) == {'unread': 1, 'queued': 1}
    assert post_media.scan(crm_db, {})['queued'] == 0
    assert post_media.read_story(crm_db, {'story_id': sid})['images'] == 3
    assert post_media.scan(crm_db, {})['unread'] == 0


def test_fresh_imports_skip_backfill_tail_and_promote_existing_job(crm_db, monkeypatch):
    monkeypatch.setenv('AI_ENABLED', 'true')
    get_settings.cache_clear()
    old, fresh = story(crm_db, 'backlog'), story(crm_db, 'fresh')
    enqueue_spaced(crm_db, 'ai.story_media_one', [{'story_id': old}], 12, key='story_id')
    crm_db.execute(text("UPDATE jobs SET available_at=now()+interval '10 hours'"))
    assert enqueue_story_media(crm_db, fresh) == 1
    assert enqueue_story_media(crm_db, fresh) == 0
    assert enqueue_story_media(crm_db, old) == 1
    rows = crm_db.execute(text("SELECT available_at, payload FROM jobs ORDER BY available_at")).all()
    assert [r.payload['story_id'] for r in rows] == [fresh, old]
    assert rows[-1].available_at < datetime.now(UTC) + timedelta(minutes=1)
    assert (rows[-1].available_at - rows[0].available_at).total_seconds() == 12
    assert enqueue_story_media(crm_db, story(crm_db, 'ancient', days=800)) == 0
    monkeypatch.setenv('AI_ENABLED', 'false')
    get_settings.cache_clear()
    assert enqueue_story_media(crm_db, story(crm_db, 'disabled')) == 0


def test_imported_story_is_claimed_before_already_due_backfill(crm_db, monkeypatch):
    from contextlib import contextmanager

    from app.job_queue import claim

    class TestEngine:
        @contextmanager
        def begin(self):
            with crm_db.bind.begin():
                yield crm_db.bind

    monkeypatch.setenv('AI_ENABLED', 'true')
    get_settings.cache_clear()
    old, fresh, promoted = (story(crm_db, name) for name in ('due-old', 'due-fresh', 'due-promoted'))
    enqueue_spaced(crm_db, 'ai.story_media_one', [{'story_id': old}, {'story_id': promoted}], 12, key='story_id')
    # This is the production failure: thousands of historical jobs are already
    # due, so an import with an earlier *future* date still waits behind them.
    crm_db.execute(text("UPDATE jobs SET available_at=now()-interval '1 day'"))
    assert enqueue_story_media(crm_db, fresh) == 1
    assert enqueue_story_media(crm_db, promoted) == 1
    crm_db.execute(text("UPDATE jobs SET available_at=available_at-interval '1 minute' WHERE priority=10"))
    crm_db.commit()
    engine = TestEngine()
    assert claim(engine, 'test-story', 'ai')['payload']['story_id'] == fresh
    assert claim(engine, 'test-story', 'ai')['payload']['story_id'] == promoted
    assert claim(engine, 'test-story', 'ai')['payload']['story_id'] == old


def test_ocr_first_keeps_scene_description_when_story_read_finishes(crm_db, tmp_path, monkeypatch):
    from app.ai import image_ocr

    monkeypatch.setenv('AI_ENABLED', 'true')
    get_settings.cache_clear()
    sid = story(crm_db, 'cats-ocr-first')
    mid = asset(crm_db, photo(tmp_path, 'cats', 'brown'), 'image/jpeg', 'cats')
    archive_media.link(crm_db, mid, sid, 'media-1', entity_type='social_story', retain=False)
    image_ocr.configure(crm_db, enabled=True, daily_limit=100)
    work = image_ocr.claim(crm_db)
    def no_second_call(*args):
        raise AssertionError('Combined reading must not call the model again')

    monkeypatch.setattr(post_media, 'vision', no_second_call)
    assert post_media.read_story(crm_db, {'story_id': sid})['waiting_for_image_reading']
    image_ocr.finish(crm_db, work, output='叠字：成功见面，没有打架',
                     description='一只猫坐在推车里，另一只在旁边', category='宠物')
    # The API can show both fields immediately, even before summary assembly.
    instant = timeline_detail(UUID(sid), provider=None, db=crm_db, _user=None)['media_reading']
    assert instant['items'][0]['description'] == '一只猫坐在推车里，另一只在旁边'
    assert instant['items'][0]['text'] == '叠字：成功见面，没有打架'
    post_media.read_story(crm_db, {'story_id': sid})
    detail = timeline_detail(UUID(sid), provider=None, db=crm_db, _user=None)['media_reading']
    assert detail['summary'] == '一只猫坐在推车里，另一只在旁边'
    assert detail['items'][0]['category'] == '宠物'
    assert detail['items'][0]['description'] == '一只猫坐在推车里，另一只在旁边'
    assert detail['items'][0]['text'] == '叠字：成功见面，没有打架'


def test_upgrade_promotes_only_waiting_imports(crm_db):
    from pathlib import Path

    for status in ('queued', 'running', 'done', 'failed'):
        sid = story(crm_db, 'upgrade-' + status)
        crm_db.execute(text("""INSERT INTO jobs(job_type,payload,status)
            VALUES('ai.story_media_one',jsonb_build_object('story_id',CAST(:id AS text),'ingested',true),:status)"""),
            {'id': sid, 'status': status})
    migration = Path(__file__).parents[3] / 'migrations/072_story_import_priority.sql'
    for _ in range(2):
        crm_db.connection().exec_driver_sql(migration.read_text())
    rows = crm_db.execute(text('SELECT status,priority FROM jobs')).all()
    assert dict(rows) == {'queued': 10, 'running': 0, 'done': 0, 'failed': 0}


def test_mixed_story_only_sends_unread_video_frame(crm_db, tmp_path, monkeypatch):
    from app.ai import image_ocr

    monkeypatch.setenv('AI_ENABLED', 'true')
    get_settings.cache_clear()
    sid = story(crm_db, 'mixed-combined')
    for name, mime, path, role in (
        ('mixed-photo', 'image/jpeg', photo(tmp_path, 'mixed-photo', 'green'), 'media-1'),
        ('mixed-video', 'video/mp4', video(tmp_path, 'mixed-video'), 'media-2'),
    ):
        archive_media.link(crm_db, asset(crm_db, path, mime, name), sid, role, entity_type='social_story', retain=False)
    image_ocr.configure(crm_db, enabled=True, daily_limit=100)
    image_ocr.finish(crm_db, image_ocr.claim(crm_db), output='叠字：周末', description='绿色草地', category='风景', summary='户外草地')
    calls = []
    def read_frame(model, system, prompt, images, schema):
        calls.append(images)
        assert len(images) == 1 and '视频截帧' in images[0][0]
        return PostMediaReading(summary='视频画面', items=[ImageReading(index=1, category='风景', description='绿色视频', text='')])
    monkeypatch.setattr(post_media, 'vision', read_frame)
    assert post_media.read_story(crm_db, {'story_id': sid})['read']
    assert not post_media.read_story(crm_db, {'story_id': sid})['read']
    assert len(calls) == 1
    items = timeline_detail(UUID(sid), provider=None, db=crm_db, _user=None)['media_reading']['items']
    assert [(item['index'], item['description']) for item in items] == [(1, '绿色草地'), (2, '绿色视频')]
