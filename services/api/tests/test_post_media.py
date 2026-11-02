import base64
import io
from datetime import UTC, datetime, timedelta

import av
from PIL import Image
from sqlalchemy import text
from test_ai import post
from test_ui_endpoints import person

from app.ai import post_media
from app.ai.schedule import SCHEDULES
from app.ai.schemas import ImageReading, PostMediaReading
from app.config import get_settings


def asset(db, path, media_type, name):
    sha = name.encode().hex().ljust(64, "0")[:64]
    content = db.execute(text("""INSERT INTO content_objects(sha256,content_kind,byte_length,storage_uri)
        VALUES(:sha,'media',1,'x') RETURNING id"""), {"sha": sha}).scalar_one()
    return str(db.execute(text("""INSERT INTO media_assets(content_object_id,sha256,media_type,byte_length,object_path)
        VALUES(:c,:sha,:type,1,:path) RETURNING id"""), {"c": content, "sha": sha, "type": media_type, "path": str(path)}).scalar_one())


def link(db, media_id, post_id, role):
    db.execute(text("INSERT INTO media_links(media_id,entity_type,entity_id,role) VALUES(:m,'social_post',:p,:r)"),
               {"m": media_id, "p": post_id, "r": role})


def photo(tmp_path, name, color, size=(2000, 1500)):
    path = tmp_path / f"{name}.jpg"
    Image.new("RGB", size, color).save(path)
    return path


def video(tmp_path, name, seconds=3):
    path = tmp_path / f"{name}.mp4"
    with av.open(str(path), "w") as container:
        stream = container.add_stream("mpeg4", rate=10)
        stream.width, stream.height, stream.pix_fmt = 64, 48, "yuv420p"
        for _ in range(seconds * 10):
            container.mux(stream.encode(av.VideoFrame.from_image(Image.new("RGB", (64, 48), "green"))))
        container.mux(stream.encode())
    return path


def decoded_size(url):
    return Image.open(io.BytesIO(base64.b64decode(url.split(",", 1)[1]))).size


def test_posts_are_read_once_from_images_posters_and_video_frames(crm_db, tmp_path, monkeypatch):
    friend = person(crm_db, "爱拍照")
    post_id = post(crm_db, friend, "moment-ocr", "周末去了新开的咖啡店", days_ago=10)
    link(crm_db, asset(crm_db, photo(tmp_path, "menu", "white"), "image/jpeg", "menu"), post_id, "media-1")
    link(crm_db, asset(crm_db, video(tmp_path, "clip"), "video/mp4", "clip"), post_id, "media-2")
    link(crm_db, asset(crm_db, photo(tmp_path, "poster", "blue", (640, 360)), "image/jpeg", "poster"), post_id, "thumbnail-2")
    link(crm_db, asset(crm_db, video(tmp_path, "bare", seconds=1), "video/mp4", "bare"), post_id, "media-3")
    link(crm_db, asset(crm_db, video(tmp_path, "live"), "video/mp4", "live"), post_id, "live-media-1")
    since = datetime.now(UTC) - timedelta(days=30)
    assert post_media.unread(crm_db, since) == [post_id]

    calls = []

    def fake(model, system, prompt, images, schema):
        calls.append((model, prompt, images))
        return PostMediaReading(summary="  在咖啡店   拍的菜单和店内视频  ", items=[
            ImageReading(index=1, category="文档票据", text="拿铁 32 " * 40, description="一张菜单"),
            ImageReading(index=2, category="室内", text="", description="店内"),
            ImageReading(index=3, category="其他", text="", description="绿色画面"),
            ImageReading(index=9, category="其他", text="", description="不存在的图"),
        ])

    monkeypatch.setattr(post_media, "vision", fake)
    get_settings.cache_clear()
    assert post_media.read_post(crm_db, {"post_id": post_id}) == {"post_id": post_id, "images": 3, "read": True}
    assert post_media.read_post(crm_db, {"post_id": post_id})["read"] is False
    assert len(calls) == 1

    model, prompt, images = calls[0]
    assert model == get_settings().ai_vision_model and "周末去了新开的咖啡店" in prompt
    assert [label for label, _ in images] == ["图 1（照片）", "图 2（视频封面）", "图 3（视频封面）"]
    assert decoded_size(images[0][1]) == (1280, 960)
    assert decoded_size(images[1][1]) == (640, 360) and decoded_size(images[2][1]) == (64, 48)

    row = crm_db.execute(text("SELECT summary, items, search_text FROM social_post_media_readings WHERE post_id = :p"),
                         {"p": post_id}).mappings().one()
    assert row["summary"] == "在咖啡店 拍的菜单和店内视频"
    assert [(item["index"], item["kind"], item["category"]) for item in row["items"]] == [
        (1, "photo", "文档票据"), (2, "video", "室内"), (3, "video", "其他")]
    assert len(row["items"][0]["text"]) == post_media.TEXT_LENGTH and row["items"][0]["text"].endswith("…")
    assert "一张菜单" in row["search_text"]
    assert post_media.unread(crm_db, since) == []

    link(crm_db, asset(crm_db, photo(tmp_path, "later", "red"), "image/jpeg", "later"), post_id, "media-4")
    assert post_media.unread(crm_db, since) == [post_id]
    get_settings.cache_clear()


def test_scan_skips_old_posts_and_recent_failures(crm_db, tmp_path, monkeypatch):
    friend = person(crm_db, "常发动态")
    fresh, failing = post(crm_db, friend, "fresh", "新的", days_ago=2), post(crm_db, friend, "failing", "坏图", days_ago=3)
    old = post(crm_db, friend, "old", "很早以前", days_ago=800)
    for index, post_id in enumerate((fresh, failing, old)):
        link(crm_db, asset(crm_db, photo(tmp_path, f"p{index}", "white", (8, 8)), "image/jpeg", f"p{index}"), post_id, "media-1")
    crm_db.execute(text("""INSERT INTO jobs(job_type, payload, status) VALUES('ai.post_media_one',
        jsonb_build_object('post_id', CAST(:p AS text)), 'failed')"""), {"p": failing})
    monkeypatch.setenv("AI_POST_MEDIA_SINCE", (datetime.now(UTC) - timedelta(days=30)).date().isoformat())
    get_settings.cache_clear()

    assert post_media.scan(crm_db, {}) == {"unread": 1, "queued": 1}
    assert post_media.scan(crm_db, {}) == {"unread": 1, "queued": 0}
    queued = crm_db.execute(text("SELECT payload->>'post_id' FROM jobs WHERE job_type = 'ai.post_media_one' AND status = 'queued'")).scalars()
    assert list(queued) == [fresh]
    get_settings.cache_clear()


def test_post_media_scan_runs_hourly():
    hourly = next(schedule for schedule in SCHEDULES if schedule.job_type == "ai.post_media")
    at = datetime(2026, 10, 15, 9, 19, tzinfo=UTC)
    assert hourly.period(at) is None
    assert hourly.period(at.replace(minute=20)) == "2026-10-15T09"


def test_caption_model_and_prompt_changes_trigger_candidates(crm_db, tmp_path, monkeypatch):
    friend = person(crm_db, 'Input revisions')
    item = post(crm_db, friend, 'revisions', 'initial caption', days_ago=1)
    link(crm_db, asset(crm_db, photo(tmp_path, 'revision', 'blue', (8,8)), 'image/jpeg', 'revision'), item, 'media-1')
    calls = []
    def vision(*args):
        calls.append(1)
        return PostMediaReading(summary='result', items=[])
    monkeypatch.setattr(post_media, 'vision', vision)
    since = datetime.now(UTC)-timedelta(days=30)
    post_media.read_post(crm_db, {'post_id': item})
    assert post_media.unread(crm_db, since) == []
    crm_db.execute(text('UPDATE social_posts SET search_body=:body WHERE id=:id'), {'body': 'changed caption', 'id': item})
    assert post_media.unread(crm_db, since) == [item]
    post_media.read_post(crm_db, {'post_id': item})
    monkeypatch.setenv('AI_VISION_MODEL', 'new-vision-model')
    get_settings.cache_clear()
    assert post_media.unread(crm_db, since) == [item]
    post_media.read_post(crm_db, {'post_id': item})
    monkeypatch.setattr(post_media, 'SYSTEM', post_media.SYSTEM+' revised')
    assert post_media.unread(crm_db, since) == [item]
    post_media.read_post(crm_db, {'post_id': item})
    assert len(calls) == 4
    assert not post_media.read_post(crm_db, {'post_id': item})['read']
    assert len(calls) == 4
    get_settings.cache_clear()
