from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy import text
from test_ai import post
from test_post_media import asset, link, photo
from test_ui_endpoints import person

from app.ai import image_ocr, payloads
from app.config import get_settings


def setup(db, tmp_path, count=3):
    friend = person(db, 'OCR 朋友')
    pid = post(db, friend, 'ocr-post', '', days_ago=800)
    mids = []
    for n in range(count):
        mid = asset(db, photo(tmp_path, f'ocr{n}', 'white', (20, 20)), 'image/jpeg', f'ocr{n}')
        link(db, mid, pid, f'media-{n+1}')
        mids.append(mid)
    return friend, pid, mids


def enable(monkeypatch):
    monkeypatch.setenv('AI_ENABLED', 'true')
    get_settings.cache_clear()


def test_scope_dedup_resume_and_chat_consent(crm_db, tmp_path, monkeypatch):
    enable(monkeypatch)
    _friend, _pid, mids = setup(crm_db, tmp_path, 1)
    chat = asset(crm_db, photo(tmp_path, 'chat', 'blue'), 'image/jpeg', 'chat')
    avatar = asset(crm_db, photo(tmp_path, 'avatar', 'red'), 'image/jpeg', 'avatar')
    for mid, kind in ((mids[0], 'message_event'), (chat, 'message_event'), (avatar, 'profile')):
        crm_db.execute(text("INSERT INTO media_links(media_id,entity_type,entity_id,role) VALUES(:m,:t,:id,'attachment')"), {'m': mid, 't': kind, 'id': uuid4()})
    first = image_ocr.configure(crm_db, enabled=True, include_chat=False, daily_limit=2)
    assert first['total'] == 1
    image_ocr.enqueue_media(crm_db, chat, 'message_event')
    assert crm_db.execute(text('SELECT count(*) FROM image_ocr_readings')).scalar_one() == 1
    second = image_ocr.configure(crm_db, enabled=True, include_chat=True, daily_limit=2)
    assert second['total'] == 2 and second['added'] == 1
    assert image_ocr.configure(crm_db, enabled=True, include_chat=True, daily_limit=2)['added'] == 0
    assert crm_db.execute(text('SELECT media_id::text FROM image_ocr_readings WHERE media_id=:id'), {'id': avatar}).first() is None
    assert image_ocr.settings(crm_db)['model'] == 'gemini-3.5-flash-lite'


def test_daily_limit_pacing_restart_midnight_and_empty_results(crm_db, tmp_path, monkeypatch):
    enable(monkeypatch)
    _friend, _pid, _mids = setup(crm_db, tmp_path, 3)
    image_ocr.configure(crm_db, enabled=True, daily_limit=2)
    # Stay ahead of real seed timestamps regardless of when the suite runs.
    at = datetime.now(UTC).replace(hour=12, minute=0, second=0, microsecond=0) + timedelta(days=2)
    one = image_ocr.claim(crm_db, at)
    assert 'media_id' in one
    assert image_ocr.claim(crm_db, at+timedelta(seconds=1))['reason'] == 'paced'
    image_ocr.finish(crm_db, one, output='')
    two = image_ocr.claim(crm_db, at+timedelta(hours=12))
    image_ocr.finish(crm_db, two, output='正文\n第二行')
    assert crm_db.execute(text('SELECT sum(attempted) FROM image_ocr_daily_usage')).scalar_one() == 2
    # Reconfiguration/restarts must not reset today's durable usage.
    image_ocr.configure(crm_db, enabled=True, daily_limit=2)
    assert image_ocr.claim(crm_db, at+timedelta(hours=13))['reason'] == 'daily_limit'
    three = image_ocr.claim(crm_db, at+timedelta(days=2))
    assert 'media_id' in three and three['media_id'] not in (one['media_id'], two['media_id'])
    image_ocr.finish(crm_db, three, output='')
    assert image_ocr.claim(crm_db, at+timedelta(days=3))['reason'] == 'idle'
    assert image_ocr.configure(crm_db, enabled=True, daily_limit=2)['remaining'] == 0


def test_expired_leases_stale_results_and_retry_budget(crm_db, tmp_path, monkeypatch):
    enable(monkeypatch)
    _friend, _pid, _mids = setup(crm_db, tmp_path, 1)
    image_ocr.configure(crm_db, enabled=True, daily_limit=100)
    at = datetime.now(UTC)+timedelta(days=1)
    one = image_ocr.claim(crm_db, at)
    two = image_ocr.claim(crm_db, at+timedelta(minutes=16))
    assert one['media_id'] == two['media_id'] and one['token'] != two['token']
    image_ocr.finish(crm_db, one, output='过期结果')
    assert crm_db.execute(text('SELECT status FROM image_ocr_readings')).scalar_one() == 'running'
    image_ocr.finish(crm_db, two, error='network error')
    three = image_ocr.claim(crm_db, at+timedelta(hours=2))
    image_ocr.finish(crm_db, three, error='network error')
    assert crm_db.execute(text('SELECT status FROM image_ocr_readings')).scalar_one() == 'failed'
    assert image_ocr.claim(crm_db, at+timedelta(days=1))['reason'] == 'idle'


def test_ocr_reaches_social_context_and_details_but_not_chat(crm_db, tmp_path, monkeypatch):
    enable(monkeypatch)
    friend, pid, _mids = setup(crm_db, tmp_path, 1)
    image_ocr.configure(crm_db, enabled=True, daily_limit=100)
    work = image_ocr.claim(crm_db)
    image_ocr.finish(crm_db, work, output='Brown University\nUCL')
    posts = payloads.captions(crm_db, [str(friend)], datetime(2000, 1, 1, tzinfo=UTC), 30)
    assert len(posts) == 1 and 'UCL' in posts[0]['media_context']['ocr'][0]['text']
    detail = image_ocr.event_reading(crm_db, pid, 'social_post', None)
    assert detail['items'][0]['text'] == 'Brown University\nUCL'
    image_ocr.configure(crm_db, enabled=False, daily_limit=100)
    assert image_ocr.claim(crm_db)['reason'] == 'paused'


def test_daily_reset_uses_chicago_calendar_across_dst(crm_db, tmp_path, monkeypatch):
    enable(monkeypatch)
    setup(crm_db, tmp_path, 1)
    image_ocr.configure(crm_db, enabled=True, daily_limit=1)
    at = datetime(2026, 11, 1, 5, 30, tzinfo=UTC)
    # The fixture may be created after this fixed DST boundary on the host.
    crm_db.execute(text('UPDATE image_ocr_readings SET available_at=:at'), {'at': at})
    crm_db.commit()
    work = image_ocr.claim(crm_db, at)
    image_ocr.finish(crm_db, work, output='')
    assert image_ocr.claim(crm_db, datetime(2026, 11, 2, 5, 59, tzinfo=UTC))['reason'] == 'daily_limit'
    assert image_ocr.claim(crm_db, datetime(2026, 11, 2, 6, 0, tzinfo=UTC))['reason'] == 'idle'
    assert crm_db.execute(text('SELECT count(*) FROM image_ocr_daily_usage')).scalar_one() == 2


def test_newest_source_date_is_processed_first(crm_db, tmp_path, monkeypatch):
    enable(monkeypatch)
    friend = person(crm_db, '按时间补扫')
    expected = []
    for n, days in enumerate((800, 3, 1)):
        pid = post(crm_db, friend, f'chronological{n}', '', days_ago=days)
        mid = asset(crm_db, photo(tmp_path, f'chron{n}', 'white'), 'image/jpeg', f'chron{n}')
        link(crm_db, mid, pid, 'media-1')
        expected.insert(0, mid)
    image_ocr.configure(crm_db, enabled=True, daily_limit=1000)
    now = datetime.now(UTC)+timedelta(minutes=1)
    for n, mid in enumerate(expected):
        work = image_ocr.claim(crm_db, now+timedelta(minutes=n*2))
        assert work['media_id'] == mid
        image_ocr.finish(crm_db, work, output='')


def test_completed_ocr_invalidates_committed_media_context(crm_db, tmp_path, monkeypatch):
    from app.data_versions import current
    enable(monkeypatch)
    setup(crm_db, tmp_path, 1)
    image_ocr.configure(crm_db, enabled=True, daily_limit=100)
    before = current(crm_db, ['post_media'])
    work = image_ocr.claim(crm_db)
    assert current(crm_db, ['post_media']) == before  # Claiming is not new content.
    image_ocr.finish(crm_db, work, output='New OCR text')
    assert current(crm_db, ['post_media'])['post_media'] > before['post_media']


def test_scene_without_text_is_shown_and_used_in_ai_context(crm_db, tmp_path, monkeypatch):
    enable(monkeypatch)
    friend, pid, _mids = setup(crm_db, tmp_path, 1)
    crm_db.execute(text("UPDATE social_posts SET search_body='' WHERE id=:id"), {'id': pid})
    image_ocr.configure(crm_db, enabled=True, daily_limit=100)
    work = image_ocr.claim(crm_db)
    image_ocr.finish(crm_db, work, output='', description='黄昏时人群经过商业街路口', category='城市街景', summary='黄昏商业街景')
    detail = image_ocr.event_reading(crm_db, pid, 'social_post', None)
    assert detail['summary'] == '黄昏商业街景'
    assert detail['items'][0]['description'] == '黄昏时人群经过商业街路口'
    assert detail['items'][0]['text'] == ''
    events = payloads.captions(crm_db, [str(friend)], datetime(2000, 1, 1, tzinfo=UTC), 30)
    assert events[0]['media_context']['ocr'][0]['description'] == '黄昏时人群经过商业街路口'


def test_repair_keeps_old_text_and_daily_quota(crm_db, tmp_path, monkeypatch):
    enable(monkeypatch)
    _friend, pid, _mids = setup(crm_db, tmp_path, 1)
    image_ocr.configure(crm_db, enabled=True, daily_limit=100)
    work = image_ocr.claim(crm_db)
    image_ocr.finish(crm_db, work, output='旧版文字')
    used = image_ocr.progress(crm_db)['attempted_today']
    assert image_ocr.repair_text_only(crm_db) == 1
    assert image_ocr.repair_text_only(crm_db) == 0

    crm_db.commit()
    assert image_ocr.progress(crm_db)['attempted_today'] == used
    assert image_ocr.event_reading(crm_db, pid, 'social_post', None)['items'][0]['text'] == '旧版文字'
    again = image_ocr.claim(crm_db, datetime.now(UTC)+timedelta(minutes=20))
    image_ocr.finish(crm_db, again, output='叠字：新版文字', description='推车旁的两只猫', category='宠物', summary='两只猫见面')
    # The simulated retry can cross local midnight. Count both reservations;
    # progress() intentionally reports only the real current local day.
    assert crm_db.execute(text('SELECT sum(attempted) FROM image_ocr_daily_usage')).scalar_one() == used+1
    assert image_ocr.repair_text_only(crm_db) == 0


def test_upgrade_requeues_completed_text_without_erasing_it(crm_db, tmp_path, monkeypatch):
    from pathlib import Path

    enable(monkeypatch)
    _friend, pid, _mids = setup(crm_db, tmp_path, 1)
    image_ocr.configure(crm_db, enabled=True, daily_limit=100)
    work = image_ocr.claim(crm_db)
    image_ocr.finish(crm_db, work, output='邮件通知：订单已确认')
    used = image_ocr.progress(crm_db)['attempted_today']
    # Reproduce the pre-upgrade schema and run the real additive migration.
    crm_db.execute(text('ALTER TABLE image_ocr_readings DROP COLUMN description,DROP COLUMN category,DROP COLUMN summary'))
    crm_db.connection().exec_driver_sql((Path(__file__).parents[3] / 'migrations/073_image_ocr_scene.sql').read_text())
    crm_db.commit()
    row = crm_db.execute(text('SELECT status,text,attempts FROM image_ocr_readings')).one()
    assert tuple(row) == ('pending', '邮件通知：订单已确认', 1)
    assert image_ocr.progress(crm_db)['attempted_today'] == used
    assert image_ocr.event_reading(crm_db, pid, 'social_post', None)['items'][0]['text'] == '邮件通知：订单已确认'


def test_one_asset_reused_by_post_and_story_needs_no_second_model_call(crm_db, tmp_path, monkeypatch):
    from test_story_media import story

    from app.ai import post_media

    enable(monkeypatch)
    _friend, pid, mids = setup(crm_db, tmp_path, 1)
    sid = story(crm_db, 'shared-combined')
    crm_db.execute(text("""INSERT INTO media_links(media_id,entity_type,entity_id,role)
        VALUES(:mid,'social_story',:sid,'media-1')"""), {'mid': mids[0], 'sid': sid})
    image_ocr.configure(crm_db, enabled=True, daily_limit=100)
    work = image_ocr.claim(crm_db)
    image_ocr.finish(crm_db, work, output='定位贴纸：港威大厦；叠字：真香',
                     description='炭烤汉堡排、蛋黄饭和配菜', category='食物', summary='分享炭烤汉堡排饭')
    def no_second_call(*args):
        raise AssertionError('The asset has already been read by Flash Lite')
    monkeypatch.setattr(post_media, 'vision', no_second_call)
    assert post_media.read_post(crm_db, {'post_id': pid})['read']
    assert post_media.read_story(crm_db, {'story_id': sid})['read']
    assert not post_media.read_post(crm_db, {'post_id': pid})['read']
    assert image_ocr.progress(crm_db)['attempted_today'] == 1
    for event_id, kind in ((pid, 'social_post'), (sid, 'social_story')):
        item = image_ocr.event_reading(crm_db, event_id, kind, None)['items'][0]
        assert item['description'] == '炭烤汉堡排、蛋黄饭和配菜'
        assert item['text'] == '定位贴纸：港威大厦；叠字：真香'
