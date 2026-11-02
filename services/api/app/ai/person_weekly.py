"""One short, evidence-backed weekly note per active person."""

import json
from datetime import date, datetime, time, timedelta
from uuid import UUID
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Query
from sqlalchemy import text
from sqlalchemy.orm import Session

from ..auth import current_user
from ..config import get_settings
from ..db import get_db
from ..job_queue import commit_result
from . import person_memory
from .client import input_hash, structured
from .payloads import VISUAL_RULES
from .person_context import batches, chat, profile_changes, social
from .prompts import CONTEXT_RULES
from .queue import enqueue_spaced
from .schemas import PersonWeekAnalysis, PersonWeekPart
from .store import current, generate

router = APIRouter()
CHUNK = "profile_week_chunk"
SPACING_SECONDS = 30
CHUNK_SYSTEM = """请给一个人的这组本周来源写一句简短近况，并引用最多 4 个来源 id。
Memory 仅作人物背景，不能作为本周事件的证据；不要引用晚于这周的背景来解释本周。
朋友圈和 Instagram 的日常分享按共同主题归纳，不要逐条复述，不要把旅行照片、截图或转发夸大为重大人生变化。
discovered=true 表示本周新补录/识别的历史内容，须说成新发现，不能说事情发生在本周。只有明确证据才写确定事实。"""
FINAL_SYSTEM = """请结合这个人的长期 Memory 和这一周所有分片摘要，写 2–3 句中文个人近况，最多引用 4 个来源 id。
Memory 只帮助识别人物，所有关于本周发生或本周发现的陈述都必须有本周来源支持。
重点是对日常朋友圈、Instagram 和其他分享作简短归纳；只有明确证据才写工作、搬家、婚育等大事。
有本周新补录的历史资料时说明是“新发现”，不要误称为本周发生。不要逐条列动态，也不要把别人的照片或文字当成此人的经历。"""
CHUNK_SYSTEM += "\n" + VISUAL_RULES + "\n" + CONTEXT_RULES
FINAL_SYSTEM += "\n" + VISUAL_RULES + "\n" + CONTEXT_RULES


def bounds(week_start: date) -> tuple[datetime, datetime]:
    start = datetime.combine(week_start, time(), ZoneInfo(get_settings().crm_timezone))
    return start, start + timedelta(days=7)


def previous_week(period: str) -> date:
    return date.fromisoformat(period) - timedelta(days=7)


def candidates(db, start: datetime, end: datetime) -> list[str]:
    """Source occurrence, import, saved OCR, and profile edits all count."""
    rows = db.execute(text("""WITH changed AS (
      SELECT profile_id FROM social_posts WHERE profile_id IS NOT NULL
        AND (occurred_at>=:start AND occurred_at<:end OR ai_seen_at>=:start AND ai_seen_at<:end)
      UNION SELECT profile_id FROM social_stories WHERE profile_id IS NOT NULL
        AND (occurred_at>=:start AND occurred_at<:end OR ai_seen_at>=:start AND ai_seen_at<:end)
      UNION SELECT p.profile_id FROM social_post_media_readings r JOIN social_posts p ON p.id=r.post_id
        WHERE r.generated_at>=:start AND r.generated_at<:end AND p.profile_id IS NOT NULL
      UNION SELECT s.profile_id FROM social_story_media_readings r JOIN social_stories s ON s.id=r.story_id
        WHERE r.generated_at>=:start AND r.generated_at<:end AND s.profile_id IS NOT NULL
      UNION SELECT p.profile_id FROM image_ocr_readings r JOIN media_links l ON l.media_id=r.media_id
        JOIN social_posts p ON l.entity_type='social_post' AND p.id=l.entity_id
        WHERE r.status='complete' AND r.generated_at>=:start AND r.generated_at<:end AND p.profile_id IS NOT NULL
      UNION SELECT s.profile_id FROM image_ocr_readings r JOIN media_links l ON l.media_id=r.media_id
        JOIN social_stories s ON l.entity_type='social_story' AND s.id=l.entity_id
        WHERE r.status='complete' AND r.generated_at>=:start AND r.generated_at<:end AND s.profile_id IS NOT NULL
      UNION SELECT c.profile_id FROM message_events m JOIN conversations c ON c.id=m.conversation_id
        WHERE c.conversation_type='direct' AND c.profile_id IS NOT NULL
          AND (m.occurred_at>=:start AND m.occurred_at<:end OR m.ai_seen_at>=:start AND m.ai_seen_at<:end)
      UNION SELECT coalesce(l.profile_id,m.sender_profile_id) FROM message_events m
        JOIN conversations c ON c.id=m.conversation_id AND c.conversation_type='group'
        LEFT JOIN source_account_links l ON l.account_id=m.sender_account_id
        WHERE (m.occurred_at>=:start AND m.occurred_at<:end OR m.ai_seen_at>=:start AND m.ai_seen_at<:end)
      UNION SELECT profile_id FROM profile_field_current WHERE updated_at>=:start AND updated_at<:end
      UNION SELECT profile_id FROM profile_structured_current WHERE updated_at>=:start AND updated_at<:end
      UNION SELECT profile_id FROM profile_dates WHERE updated_at>=:start AND updated_at<:end
      UNION SELECT ap.profile_id FROM activities a JOIN activity_participants ap ON ap.activity_id=a.id
        WHERE a.created_at>=:start AND a.created_at<:end
      UNION SELECT profile_id FROM tag_memberships WHERE created_at>=:start AND created_at<:end
      UNION SELECT profile_id FROM source_account_links WHERE linked_at>=:start AND linked_at<:end
      UNION SELECT id FROM profiles WHERE ai_changed_at>=:start AND ai_changed_at<:end
      UNION SELECT person_profile_id FROM group_memberships WHERE joined_at>=:start AND joined_at<:end
      UNION SELECT from_profile_id FROM relationship_edges WHERE evidence_kind='relationship'
        AND created_at>=:start AND created_at<:end
      UNION SELECT to_profile_id FROM relationship_edges WHERE evidence_kind='relationship'
        AND created_at>=:start AND created_at<:end)
      SELECT p.id::text FROM changed c JOIN profiles p ON p.id=c.profile_id
      WHERE p.profile_type='person' AND p.archived_at IS NULL ORDER BY p.id"""),
        {"start": start, "end": end}).scalars()
    return list(rows)


def enqueue_week(db, payload: dict) -> dict:
    week_start = previous_week(payload["period"])
    start, end = bounds(week_start)
    ids = candidates(db, start, end)
    jobs = [{"profile_id": pid, "week_start": week_start.isoformat(), "subject_key": f"{pid}:{week_start}"} for pid in ids]
    queued = enqueue_spaced(db, "ai.person_week", jobs, SPACING_SECONDS, key="subject_key", priority=10)
    return {"week_start": week_start.isoformat(), "people": len(ids), "queued": queued}


def _sources(db, profile_id: str, start: datetime, end: datetime) -> tuple[list[dict], dict[str, dict]]:
    evidence = []
    for post in social(db, profile_id, start, end):
        evidence.append({"kind": post.get("event_type", "post"), "source_id": post["post_id"],
                         "at": post["date"], "provider": post["provider"], "discovered": post["discovered"],
                         "text": post["caption"], "media_context": post.get("media_context")})
    evidence += chat(db, profile_id, start, end, include_discovered=True)
    evidence += profile_changes(db, profile_id, start, end)
    evidence.sort(key=lambda row: (row.get("at") or "", row["source_id"], row["kind"]))
    refs = {}
    for index, row in enumerate(evidence, 1):
        short = f"e{index}"
        row["id"] = short
        refs[short] = {"kind": row["kind"], "id": row["source_id"], "at": row.get("at"),
                       "discovered": row.get("discovered", False),
                       "conversation_id": row.get("conversation_id"),
                       "quote": str(row.get("text") or (row.get("media_context") or {}).get("summary") or "")[:160]}
    return evidence, refs


def _part(db, subject: str, payload: dict, model: str, allowed: set[str]) -> dict:
    generate(db, CHUNK, subject, model, CHUNK_SYSTEM, payload, PersonWeekPart, call=structured)
    db.commit()
    result = current(db, CHUNK, subject)["output"]
    return {"summary": result["summary"], "sources": [source for source in result["sources"] if source in allowed][:4]}


def build_one(db, payload: dict) -> dict:
    profile_id, week_start = payload["profile_id"], date.fromisoformat(payload["week_start"])
    if not db.execute(text("""SELECT 1 FROM profiles WHERE id=CAST(:id AS uuid)
        AND profile_type='person' AND archived_at IS NULL"""), {'id': profile_id}).first():
        return {"profile_id": profile_id, "skipped": "inactive person"}
    start, end = bounds(week_start)
    evidence, refs = _sources(db, profile_id, start, end)
    if not evidence:
        db.execute(text("DELETE FROM person_weekly_updates WHERE profile_id=CAST(:id AS uuid) AND week_start=:week"),
                   {"id": profile_id, "week": week_start})
        commit_result(db)
        return {"profile_id": profile_id, "week_start": week_start.isoformat(), "skipped": "no sources"}
    person_memory.refresh(db, profile_id)
    db.commit()
    memory = current(db, person_memory.MEMORY, profile_id)["output"]
    model = get_settings().ai_person_model
    parts = []
    for index, group in enumerate(batches(evidence), 1):
        parts.append(_part(db, f"{profile_id}:{week_start}:source:{index}",
                           {"memory": memory, "week_start": week_start.isoformat(), "sources": group}, model,
                           {row["id"] for row in group}))
    level = 0
    while len(parts) > 20:
        level += 1
        parts = [_part(db, f"{profile_id}:{week_start}:reduce:{level}:{index}",
                       {"memory": memory, "week_start": week_start.isoformat(), "parts": group}, model,
                       {source for part in group for source in part["sources"]})
                 for index, group in enumerate((parts[n:n + 20] for n in range(0, len(parts), 20)), 1)]
    model_input = {"memory": memory, "week_start": week_start.isoformat(), "parts": parts,
                   "source_hash": input_hash(model, CHUNK_SYSTEM, {"sources": evidence})}
    digest = input_hash(model, FINAL_SYSTEM, model_input)
    old = db.execute(text("""SELECT input_hash FROM person_weekly_updates
        WHERE profile_id=CAST(:id AS uuid) AND week_start=:week"""), {"id": profile_id, "week": week_start}).scalar_one_or_none()
    if old == digest:
        return {"profile_id": profile_id, "week_start": week_start.isoformat(), "changed": False}
    db.commit()
    draft = structured(model, FINAL_SYSTEM, model_input, PersonWeekAnalysis)
    result = {"summary": draft.summary.strip(), "sources": [refs[source] for source in dict.fromkeys(draft.sources)
                                                     if source in refs][:4]}
    db.execute(text("""INSERT INTO person_weekly_updates(profile_id,week_start,input_hash,model,output)
        VALUES(CAST(:id AS uuid),:week,:hash,:model,CAST(:output AS jsonb))
        ON CONFLICT(profile_id,week_start) DO UPDATE SET input_hash=excluded.input_hash,model=excluded.model,
          output=excluded.output,generated_at=now()"""),
        {"id": profile_id, "week": week_start, "hash": digest, "model": model,
         "output": json.dumps(result, ensure_ascii=False)})
    commit_result(db)
    return {"profile_id": profile_id, "week_start": week_start.isoformat(), "changed": True}


@router.get("/api/v1/ai/weekly-people")
def weekly_people(week: date | None = None, after: UUID | None = None,
                  limit: int = Query(12, ge=1, le=50),
                  db: Session = Depends(get_db), _user=Depends(current_user)):
    """Browse every saved person note, independently of the global digest's shortlist."""
    if not get_settings().ai_enabled:
        return {"enabled": False, "week_start": None, "weeks": [], "total": 0, "people": [], "next_cursor": None}
    local = datetime.now(ZoneInfo(get_settings().crm_timezone)).date()
    selected = week or local - timedelta(days=local.weekday() + 7)
    visible = "p.archived_at IS NULL AND p.profile_type='person'"
    weeks = list(db.execute(text(f"""SELECT DISTINCT w.week_start
        FROM person_weekly_updates w JOIN profiles p ON p.id=w.profile_id
        WHERE {visible} ORDER BY w.week_start DESC""")).scalars())
    total = db.execute(text(f"""SELECT count(*) FROM person_weekly_updates w
        JOIN profiles p ON p.id=w.profile_id WHERE {visible} AND w.week_start=:week"""),
        {"week": selected}).scalar_one()
    rows = list(db.execute(text(f"""SELECT w.profile_id::text,p.display_name AS name,
        profile_avatar_media(p.id,p.avatar_media_id)::text AS avatar_media_id,
        w.week_start,w.generated_at,w.output->>'summary' AS summary,w.output->'sources' AS sources
        FROM person_weekly_updates w JOIN profiles p ON p.id=w.profile_id
        WHERE {visible} AND w.week_start=:week
          AND (CAST(:after AS uuid) IS NULL OR w.profile_id>CAST(:after AS uuid))
        ORDER BY w.profile_id LIMIT :limit"""),
        {"week": selected, "after": str(after) if after else None, "limit": limit + 1}).mappings())
    return {"enabled": True, "week_start": selected, "weeks": sorted(set(weeks + [selected]), reverse=True),
            "total": total, "people": [dict(row) for row in rows[:limit]],
            "next_cursor": rows[limit - 1]["profile_id"] if len(rows) > limit else None}


@router.get("/api/v1/ai/weekly-highlights")
def highlights(db: Session = Depends(get_db), _user=Depends(current_user)):
    if not get_settings().ai_enabled:
        return {"enabled": False, "week_start": None, "people": []}
    local = datetime.now(ZoneInfo(get_settings().crm_timezone)).date()
    latest = local - timedelta(days=local.weekday() + 7)
    rows = db.execute(text("""SELECT w.profile_id::text,p.display_name AS name,
        profile_avatar_media(p.id,p.avatar_media_id)::text AS avatar_media_id,
        w.week_start,w.output->>'summary' AS summary
        FROM person_weekly_updates w JOIN profiles p ON p.id=w.profile_id
        WHERE w.week_start=:week AND p.archived_at IS NULL AND p.profile_type='person'
        ORDER BY random() LIMIT 8"""), {"week": latest}).mappings()
    return {"enabled": True, "week_start": latest, "people": [dict(row) for row in rows]}
