"""Weekly digest: SQL facts for the week, and the model's summary, life events and suggestions."""

import json
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException
from fastapi.encoders import jsonable_encoder
from sqlalchemy import text
from sqlalchemy.orm import Session

from ..auth import current_user
from ..config import get_settings
from ..db import get_db
from ..profile_facts import contact_contexts
from ..weekly_facts import week_facts
from .client import structured
from .payloads import VISUAL_RULES, digest_payload, week_captions
from .prompts import CONTEXT_RULES
from .schemas import DigestNarrative

router = APIRouter()
POSTS = 80

SYSTEM = """你为用户的私人 CRM 写每周回顾。输入是上周的统计事实（联系最多的人、新增联系人、新关联的账号、下周的重要日期、刚变得疏远的人）
和上周联系人发布的动态文字（posts，每条有 post_id、profile_id、name）。totals.messages 是所有聊天（含群聊）收发合计的消息条数，
不要说成“你发送了”；totals.people 是有私聊往来的人数；top_contacts 的 messages 是和这个人私聊的收发合计。用中文：
- summary：3 到 5 句话的回顾，像一位细心的朋友在周一早上提醒你，具体、不煽情，只用输入里的事实。
- life_events：从动态文字中识别人生大事（new_job 换工作、move 搬家、travel 旅行、wedding 婚礼、graduation 毕业、new_baby 添丁、other 其他重要的事），
  每条给 profile_id、kind、一句话 text、来源 post_id。没有就返回空列表，宁缺毋滥。
- suggestions：最多 5 位本周值得联系的人（profile_id 必须出现在输入里），每位一句理由。"""


SYSTEM += "\n" + VISUAL_RULES + "\n" + CONTEXT_RULES

from ..job_queue import commit_result


def previous_week(period: str) -> date:
    """A digest run on Monday `period` covers the week before it."""
    return date.fromisoformat(period) - timedelta(days=7)


def _valid(narrative: DigestNarrative, payload: dict) -> dict:
    """Drop anything that points at a person or post the model was not shown."""
    posts = {post["post_id"]: post["profile_id"] for post in payload["posts"]}
    facts = payload["facts"]
    people = set(posts.values()) | {row["profile_id"] for key in ("top_contacts", "new_people", "new_links", "upcoming_dates",
                                                                    "newly_dormant") for row in facts[key]}
    result = narrative.model_dump()
    result["life_events"] = [event for event in result["life_events"] if posts.get(event["post_id"]) == event["profile_id"]]
    result["suggestions"] = [item for item in result["suggestions"] if item["profile_id"] in people]
    return result


def build_week(db, payload: dict) -> dict:
    settings = get_settings()
    week_start = previous_week(payload["period"])
    zone = ZoneInfo(settings.crm_timezone)
    facts = jsonable_encoder(week_facts(db, week_start, settings.crm_timezone))
    db.execute(text("""INSERT INTO weekly_digests(week_start, facts) VALUES(:week, CAST(:facts AS jsonb))
                       ON CONFLICT (week_start) DO UPDATE SET facts = EXCLUDED.facts, generated_at = now()"""),
               {"week": week_start, "facts": json.dumps(facts, ensure_ascii=False)})
    db.commit()
    start = datetime.combine(week_start, time(), zone)
    posts = week_captions(db, start, start + timedelta(days=7), POSTS)
    ids = list(dict.fromkeys(post["profile_id"] for post in posts))[:12]
    model_input = digest_payload(facts, posts, contact_contexts(db, ids, compact=True))
    db.commit()
    narrative = _valid(structured(settings.ai_writer_model, SYSTEM, model_input, DigestNarrative), model_input)
    db.execute(text("""UPDATE weekly_digests SET narrative = CAST(:narrative AS jsonb), model = :model, generated_at = now()
                       WHERE week_start = :week"""),
               {"week": week_start, "narrative": json.dumps(narrative, ensure_ascii=False), "model": settings.ai_writer_model})
    commit_result(db)
    return {"week_start": week_start.isoformat(), "posts": len(model_input["posts"]),
            "life_events": len(narrative["life_events"]), "suggestions": len(narrative["suggestions"])}


def _names(db, ids: set[str]) -> dict[str, dict]:
    rows = db.execute(text("""SELECT id::text, display_name, profile_avatar_media(id, avatar_media_id)::text AS avatar_media_id
                              FROM profiles WHERE id = ANY(CAST(:ids AS uuid[]))"""), {"ids": list(ids)}).mappings()
    return {row["id"]: {"name": row["display_name"], "avatar_media_id": row["avatar_media_id"]} for row in rows}


@router.get("/api/v1/digests")
def list_digests(db: Session = Depends(get_db), _user=Depends(current_user)):
    if not get_settings().ai_enabled:
        return {"enabled": False, "digests": []}
    rows = db.execute(text("""SELECT week_start, generated_at, narrative->>'summary' AS summary,
                                COALESCE(jsonb_array_length(narrative->'life_events'), 0) AS life_events
                              FROM weekly_digests ORDER BY week_start DESC LIMIT 104""")).mappings()
    return {"enabled": True, "digests": [dict(row) for row in rows]}


@router.get("/api/v1/digests/{week_start}")
def get_digest(week_start: date, db: Session = Depends(get_db), _user=Depends(current_user)):
    if not get_settings().ai_enabled:
        raise HTTPException(404, "AI is disabled")
    row = db.execute(text("SELECT week_start, facts, narrative, model, generated_at FROM weekly_digests WHERE week_start = :week"),
                     {"week": week_start}).mappings().one_or_none()
    if row is None:
        raise HTTPException(404, "No digest for this week")
    narrative = row["narrative"] or {}
    people = {item["profile_id"] for key in ("life_events", "suggestions") for item in narrative.get(key, [])}
    posts = {event["post_id"] for event in narrative.get("life_events", [])}
    post_dates = {row[0]: row[1] for row in db.execute(text(
        """SELECT id::text, occurred_at FROM social_posts WHERE id = ANY(CAST(:ids AS uuid[]))
        UNION ALL SELECT id::text, occurred_at FROM social_stories WHERE id = ANY(CAST(:ids AS uuid[]))"""), {"ids": list(posts)})}
    return {**dict(row), "people": _names(db, people), "post_dates": post_dates}
