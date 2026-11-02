"""Read each post's images once: visible text, a short description and a category per image, and one line
for the post, so later model features can work from structured facts instead of pixels.

A video is read from its poster, or from a frame of the video when it has none. Live Photo motion is skipped:
its still is already a `media-N` image. When the paced image reader is enabled, photos and posters reuse
its combined result without another call. Chat-image consent is handled by that reader separately.
"""

import base64
import io
import json
import re
from datetime import datetime, time
from hashlib import sha256
from pathlib import Path
from zoneinfo import ZoneInfo

import av
from PIL import Image, ImageOps
from sqlalchemy import text

from ..config import get_settings
from ..post_text import excerpt
from .client import vision
from .queue import enqueue_spaced
from .schemas import PostMediaReading

SLOT = re.compile(r"media-(\d+)")
POSTER = re.compile(r"(?:media-(\d+)-poster|thumbnail-(?:media-)?(\d+))")
MEDIA_ROLE = r"^(media-[0-9]+(-poster)?|thumbnail-(media-)?[0-9]+)$"
SOURCES = {
    "social_post": ("social_posts", "social_post_media_readings", "post_id", "ai.post_media_one"),
    "social_story": ("social_stories", "social_story_media_readings", "story_id", "ai.story_media_one"),
}
MAX_IMAGES = 10
MAX_SIDE = 1280
JPEG_QUALITY = 80
FRAME_AT_US = 1_000_000
CAPTION_LENGTH = 400
TEXT_LENGTH = 200
DESCRIPTION_LENGTH = 40
SUMMARY_LENGTH = 60
# Small calls; the gap lets imports and downloads queued meanwhile run in between.
SPACING_SECONDS = 12
FAILED_RETRY_DAYS = 7

TEXT_SOURCES = """有后期叠字或不同文字来源时，用简短标签区分，如「叠字：…；邮件通知：…；网页：…；瓶身标签：…；杯垫：…」。
后期贴纸、字幕（如悬浮的黑底白字）单独标「叠字」，地点贴纸标「定位贴纸」，不能并入实物印字；每段只放同一来源，来源改变必须重新加标签。
按实际可见的媒介命名；多个物品可用「奶瓶标签」「莓盒标签」区分。同一来源合并，不必为每行加标签。
只有一种普通文字来源时不强行分类。位置仅在消除歧义时简短标注；文字来源标签不解释字体、排版或分类理由。
text 字段中，标签以外只抄可读原文，不把包装、邮件或网页上的话当成发帖者自述；不猜被遮挡的内容。
这些文字来源规则只约束 text；summary 和 description 仍须单独描述画面内容。"""

SYSTEM = """你在为私人通讯录整理朋友发的动态里的图片。结果只给程序做后续分析，必须简短，不要长篇大论。
输入：配文（可能为空），以及按顺序的图「图 N」；视频给的是封面或截帧。
每张图输出一项：
- category：从给定类别里选一个。
- text：图里有意义的文字，照抄原文，最多 120 字：招牌、海报、菜单、票据要点、截图里的内容、地名。
  忽略水印、App 按钮、导航栏、状态栏、相机参数；邮件通知和网页正文属于内容，需要保留。没有就留空。
- description：看得到的内容，最多 25 字。text 已经说清楚时可以很短。
summary：结合配文和所有图，一句话说明这条动态，最多 40 字。
规则：配文和图结合起来看；只写看得到的或配文写明的。不要猜人名、身份、关系、地点、年龄或情绪，看不清就不写。
""" + TEXT_SOURCES

from ..job_queue import commit_result


def _zone() -> ZoneInfo:
    return ZoneInfo(get_settings().crm_timezone)


def _media(db, entity_id: str, entity_type: str) -> list[dict]:
    return db.execute(text("""SELECT l.role, a.id::text AS id, a.media_type, a.object_path FROM media_links l
        JOIN media_assets a ON a.id = l.media_id
        WHERE l.entity_type = :type AND l.entity_id = CAST(:id AS uuid) AND l.role ~ :role
          AND (a.media_type LIKE 'image/%' OR a.media_type LIKE 'video/%')"""),
                      {"id": entity_id, "type": entity_type, "role": MEDIA_ROLE}).mappings().all()


def _slots(rows) -> list[dict]:
    by_role = {row["role"]: dict(row) for row in rows}
    originals = {int(match[1]): row for role, row in by_role.items() if (match := SLOT.fullmatch(role))}
    posters = {int(match[1] or match[2]): row for role, row in by_role.items()
               if (match := POSTER.fullmatch(role)) and row["media_type"].startswith("image/")}
    found = []
    for index in sorted(originals.keys() | posters.keys()):
        row = originals.get(index) or posters[index]
        video = row["media_type"].startswith("video/") or index not in originals
        poster = next((by_role[name] for name in (f"media-{index}-poster", f"thumbnail-{index}", f"thumbnail-media-{index}")
                       if name in by_role and by_role[name]["media_type"].startswith("image/")), None) if video else None
        found.append({"index": index, "media_id": row["id"], "kind": "video" if video else "photo",
                      "read_from": (poster or row)["id"], "path": (poster or row)["object_path"],
                      "frame": video and poster is None})
    return sorted(found, key=lambda slot: slot["index"])


def slots(db, post_id: str, entity_type: str = "social_post") -> list[dict]:
    """Images, video posters (including poster-only Stories), or video frames in display order."""
    return _slots(_media(db, post_id, entity_type))


def _frame(path: Path) -> Image.Image:
    """A frame about a second in, or the first frame of a shorter video."""
    with av.open(str(path)) as container:
        if (container.duration or 0) > 2 * FRAME_AT_US:
            container.seek(FRAME_AT_US)
        return next(container.decode(video=0)).to_image()


def data_url(path: str, frame: bool, *, max_side: int = MAX_SIDE) -> str:
    with (_frame(Path(path)) if frame else Image.open(path)) as original:
        if not frame:
            original.draft("RGB", (max_side, max_side))
        original.thumbnail((max_side, max_side))
        with ImageOps.exif_transpose(original) as oriented, oriented.convert("RGB") as image, io.BytesIO() as buffer:
            image.save(buffer, "JPEG", quality=JPEG_QUALITY)
            return "data:image/jpeg;base64," + base64.b64encode(buffer.getvalue()).decode()



def _source_caption(db, entity_id: str, entity_type: str) -> str:
    if entity_type == "social_story":
        statement = """SELECT coalesce(co.payload_json->>'text', co.payload_json->>'caption', '')
            FROM social_stories s LEFT JOIN content_objects co ON co.id = s.content_object_id
            WHERE s.id = CAST(:id AS uuid)"""
    else:
        statement = "SELECT search_body FROM social_posts WHERE id = CAST(:id AS uuid)"
    body = db.execute(text(statement), {"id": entity_id}).scalar_one()
    return body or ""


def _caption(db, entity_id: str, entity_type: str) -> str:
    return excerpt(_source_caption(db, entity_id, entity_type), CAPTION_LENGTH)


def _clip(value: str, limit: int) -> str:
    value = re.sub(r"\s+", " ", value).strip()
    return value if len(value) <= limit else value[:limit - 1] + "…"


def tidy(reading: PostMediaReading, shown: list[dict]) -> dict:
    """Keep one entry per shown image, in order, with each field held to its length."""
    by_index = {}
    for item in reading.items:
        by_index.setdefault(item.index, item)
    items = []
    for position, slot in enumerate(shown, 1):
        item = by_index.get(position)
        if item:
            items.append({"index": position, "media_id": slot["media_id"], "kind": slot["kind"], "category": item.category,
                          "text": _clip(item.text, TEXT_LENGTH), "description": _clip(item.description, DESCRIPTION_LENGTH)})
    return {"summary": _clip(reading.summary, SUMMARY_LENGTH), "items": items}


def search_text(result: dict) -> str:
    parts = [result["summary"]] + [part for item in result["items"] for part in (item["text"], item["description"])]
    return " ".join(part for part in parts if part)


def read_post(db, payload: dict) -> dict:
    """Read one post's images unless the stored reading came from the same caption, media and model."""
    return _read(db, payload["post_id"], "social_post")


def read_story(db, payload: dict) -> dict:
    return _read(db, payload["story_id"], "social_story")


def combined_reading(db, shown: list[dict]) -> dict | None:
    """Reuse one Flash Lite result per image/poster; None keeps the video fallback.

    An empty mapping means the paced reader owns these images but is not done.
    Its completion requeues assembly, so waiting never makes a second AI call.
    """
    from . import image_ocr

    config = image_ocr.settings(db)
    photos = [(position, slot) for position, slot in enumerate(shown, 1) if not slot['frame']]
    if not config['enabled'] or not photos:
        return None
    ids = list({slot['read_from'] for _position, slot in photos})
    image_ocr.seed(db, config['include_chat'], ids)
    rows = {str(row['media_id']): row for row in db.execute(text("""SELECT media_id,status,description,category,text,summary
        FROM image_ocr_readings WHERE media_id=ANY(CAST(:ids AS uuid[]))"""), {'ids': ids}).mappings()}
    if any(mid not in rows or rows[mid]['status'] != 'complete' or not rows[mid]['description'] for mid in ids):
        commit_result(db)
        return {}
    items = [{'index': position, 'media_id': slot['media_id'], 'kind': slot['kind'],
              'category': rows[slot['read_from']]['category'], 'description': rows[slot['read_from']]['description'],
              'text': _clip(rows[slot['read_from']]['text'] or '', TEXT_LENGTH)}
             for position, slot in photos]
    summaries = dict.fromkeys(rows[slot['read_from']]['summary'] or rows[slot['read_from']]['description'] for _position, slot in photos)
    return {'summary': _clip('；'.join(summaries), SUMMARY_LENGTH), 'items': items}


def _read(db, entity_id: str, entity_type: str) -> dict:
    _table, readings, key, _job_type = SOURCES[entity_type]
    model = get_settings().ai_vision_model
    media = _media(db, entity_id, entity_type)
    every = _slots(media)
    shown = every[:MAX_IMAGES]
    source_caption = _source_caption(db, entity_id, entity_type)
    caption = excerpt(source_caption, CAPTION_LENGTH)
    media_ids = sorted({row["id"] for row in media})
    if not shown:
        return {key: entity_id, 'images': 0, 'read': False}
    result = combined_reading(db, shown)
    if result == {}:
        return {key: entity_id, 'images': len(shown), 'read': False, 'waiting_for_image_reading': True}
    if result is not None:
        model = db.execute(text('SELECT model FROM image_ocr_settings WHERE id=true')).scalar_one()
    digest = sha256(repr((model, SYSTEM, caption, [(slot["media_id"], slot["read_from"]) for slot in every], result)).encode()).hexdigest()
    stored = db.execute(text(f"SELECT input_hash FROM {readings} WHERE {key} = CAST(:id AS uuid)"),
                        {"id": entity_id}).scalar_one_or_none()
    if stored == digest:
        if stored == digest:
            db.execute(text(f"""UPDATE {readings} SET media_ids = CAST(:media AS uuid[]), source_caption=:caption, prompt_hash=:prompt
                WHERE {key} = CAST(:id AS uuid)"""),
                       {"media": media_ids, "id": entity_id, "caption": source_caption, "prompt": sha256(SYSTEM.encode()).hexdigest()})
            commit_result(db)
        return {key: entity_id, "images": len(shown), "read": False}
    if result is None:
        images = [(f"图 {position}（{'视频封面' if slot['kind'] == 'video' else '照片'}）", data_url(slot["path"], slot["frame"]))
                  for position, slot in enumerate(shown, 1)]
        db.commit()
        result = tidy(vision(model, SYSTEM, f"配文：{caption or '（无）'}", images, PostMediaReading), shown)
    elif frames := [(position, slot) for position, slot in enumerate(shown, 1) if slot['frame']]:
        # A post can mix photos and videos without posters. Only send the video
        # frames to the fallback; its photos already have a combined result.
        images = [(f'图 {n}（视频截帧）', data_url(slot['path'], True)) for n, (_position, slot) in enumerate(frames, 1)]
        db.commit()
        videos = tidy(vision(model, SYSTEM, f"配文：{caption or '（无）'}", images, PostMediaReading), [slot for _position, slot in frames])
        for item in videos['items']:
            item['index'] = frames[item['index']-1][0]
        result['items'] = sorted(result['items']+videos['items'], key=lambda item: item['index'])
        result['summary'] = _clip('；'.join(filter(None, [result['summary'], videos['summary']])), SUMMARY_LENGTH)
    db.execute(text(f"""INSERT INTO {readings}({key}, media_ids, input_hash, model, summary, items, search_text, source_caption, prompt_hash)
        VALUES (CAST(:id AS uuid), CAST(:media AS uuid[]), :hash, :model, :summary, CAST(:items AS jsonb), :search, :caption, :prompt)
        ON CONFLICT ({key}) DO UPDATE SET media_ids = EXCLUDED.media_ids, input_hash = EXCLUDED.input_hash, model = EXCLUDED.model,
          source_caption=EXCLUDED.source_caption, prompt_hash=EXCLUDED.prompt_hash,
          summary = EXCLUDED.summary, items = EXCLUDED.items, search_text = EXCLUDED.search_text, generated_at = now()"""),
               {"id": entity_id, "media": media_ids, "hash": digest, "model": model, "summary": result["summary"],
                "items": json.dumps(result["items"], ensure_ascii=False), "search": search_text(result), "caption": source_caption, "prompt": sha256(SYSTEM.encode()).hexdigest()})
    commit_result(db)
    return {key: entity_id, "images": len(shown), "read": True}


def unread(db, since: datetime, entity_type: str = "social_post") -> list[str]:
    """Posts since `since` whose media set has no reading yet or changed since it was read, newest first.
    A post whose job failed within the last week waits, so a broken file is not retried every hour."""
    table, readings, key, job_type = SOURCES[entity_type]
    accepted_models = [get_settings().ai_vision_model]
    image_config = db.execute(text('SELECT enabled,model FROM image_ocr_settings WHERE id=true')).mappings().one()
    if image_config['enabled']:
        accepted_models.append(image_config['model'])
    caption_sql = "coalesce(p.search_body, '')" if entity_type == "social_post" else "coalesce((SELECT coalesce(c.payload_json->>'text', c.payload_json->>'caption', '') FROM content_objects c WHERE c.id=p.content_object_id), '')"
    rows = db.execute(text(f"""WITH media AS (
          SELECT l.entity_id, array_agg(DISTINCT l.media_id ORDER BY l.media_id) AS media_ids
          FROM media_links l JOIN {table} p ON p.id = l.entity_id AND p.occurred_at >= :since
          JOIN media_assets a ON a.id = l.media_id AND (a.media_type LIKE 'image/%' OR a.media_type LIKE 'video/%')
          WHERE l.entity_type = :type AND l.role ~ :role GROUP BY l.entity_id)
        SELECT media.entity_id::text FROM media
        JOIN {table} p ON p.id = media.entity_id
        LEFT JOIN {readings} r ON r.{key} = media.entity_id
        WHERE (r.{key} IS NULL OR r.media_ids <> media.media_ids OR coalesce(r.model,'') <> ALL(CAST(:models AS text[]))
          OR r.source_caption IS DISTINCT FROM {caption_sql} OR r.prompt_hash IS DISTINCT FROM :prompt)
          AND NOT EXISTS (SELECT 1 FROM jobs j WHERE j.status = 'failed' AND j.job_type = :job_type
                          AND j.dedupe_key = media.entity_id::text
                          AND j.available_at > now() - make_interval(days => :retry_days))
        ORDER BY p.occurred_at DESC, media.entity_id"""), {"since": since, "retry_days": FAILED_RETRY_DAYS,
                                                         "type": entity_type, "role": MEDIA_ROLE, "key": key,
                                                         "job_type": job_type, "models": accepted_models, "prompt": sha256(SYSTEM.encode()).hexdigest()})
    return [row[0] for row in rows]


def scan(db, _payload: dict) -> dict:
    """Queue a read for every post from the configured day on whose images are unread or changed."""
    since = datetime.combine(get_settings().ai_post_media_since, time(), _zone())
    pending = queued = 0
    for entity_type in ("social_story", "social_post"):
        ids = unread(db, since, entity_type)
        _table, _readings, key, job_type = SOURCES[entity_type]
        pending += len(ids)
        queued += enqueue_spaced(db, job_type, [{key: entity_id} for entity_id in ids], SPACING_SECONDS, key=key)
    return {"unread": pending, "queued": queued}
