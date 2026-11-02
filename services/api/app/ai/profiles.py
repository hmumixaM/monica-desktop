"""Per-person Memory, and suggested additions drawn from recent chats and posts.

Everyone with meaningful saved content can get Memory. Suggestions use recent
chats and posts. Nothing reaches the editable profile until it is accepted.
"""

import json
import re
from collections import Counter
from datetime import date, datetime, time, timedelta
from hashlib import sha256
from typing import Literal
from uuid import UUID
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.orm import Session

from ..auth import current_user
from ..config import get_settings
from ..db import get_db
from ..profile_facts import avatar_map, contact_contexts, field_texts
from ..repositories import append_field_revision, get_profile
from ..significant_dates import DateError, create_date
from . import person_memory
from .chat_excerpts import direct_lines, group_lines, recent_speakers
from .client import structured
from .network_context import network_context
from .payloads import VISUAL_RULES, captions, recent_posters
from .prompts import CONTEXT_RULES
from .queue import enqueue_spaced
from .schemas import ProfileUpdates
from .store import current, generate

router = APIRouter()
OVERVIEW = person_memory.MEMORY
UPDATES = "profile_updates"
WINDOW_DAYS = 90
POST_WINDOW_DAYS = 730
DIRECT_LINES = 300
GROUP_LINES = 150
POSTS = 20
# Enough recent material for a suggestion pass without Memory.
SUGGEST_MIN_DIRECT = 5
SUGGEST_MIN_GROUP = 10
SUGGEST_MIN_POSTS = 3
# Leave gaps between queued people so newly imported content can enter the AI lane.
SPACING_SECONDS = 30
FIELD_KEYS = {
    "residence": "monica.residence", "hometown": "monica.hometown", "company": "monica.company", "job": "monica.job",
    "school": "monica.school", "email": "monica.email", "phone": "monica.phone", "food": "monica.food_preferences",
    "interests": "monica.interests", "family": "monica.family",
}
LABELS = {
    "birthday": "生日", "residence": "常驻地", "hometown": "家乡", "company": "公司", "job": "职位", "school": "学校",
    "email": "邮箱", "phone": "电话", "food": "饮食偏好", "interests": "兴趣", "family": "家人", "meetup": "见面 / 聚会",
}
MEETUP_PAST_DAYS = 120
MEETUP_AHEAD_DAYS = 60

UPDATES_SYSTEM = """你在帮用户补全通讯录。输入有：这个人的长期 Memory（memory，资料少的人可能为空）、资料里现有的值（current）、
用户和 TA 最近 90 天的私聊（direct_chat，speaker 为「我」是用户本人，「TA」是对方）、TA 最近 90 天在群里说的话（group_chat），
以及 TA 近两年的动态（posts）。今天是 today。越早的内容越可能已经过时：常驻地、公司、职位以最新的依据为准。
找出能补充或更正资料的信息，每条一个 item：
- birthday：TA 的生日，value 写 MM-DD，知道年份写 YYYY-MM-DD。必须是 TA 本人的生日，不是别人的。
- residence 常驻地 / hometown 家乡 / company 公司 / job 职位 / school 学校 / email 邮箱 / phone 电话 /
  food 饮食偏好 / interests 兴趣 / family 家人（例如「女儿叫 Mia，今年 3 岁」）。value 写要存的值本身，简洁。
- meetup：用户和 TA 已经或约好见面、吃饭、聚会、开会。value 写一句标题，date 写那天的日期 YYYY-MM-DD；
  聊天里说「明天」「周六」时按那条消息的日期推算。只是说「改天聚」不算。
规则：
- 只写有明确依据的；evidence 列出依据行的 id（d…、g…、p…、c…），quote 摘一句原话。
- 和 current 里一样的不要写；比 current 更新或更正时才写。
- 拿不准的不写；confidence 只用 high 或 medium。
- 不要编造，不要写关于用户本人的信息。没有就返回空列表。"""


UPDATES_SYSTEM += "\n" + VISUAL_RULES + "\n" + CONTEXT_RULES


class ResolvedUpdates(BaseModel):
    """Suggestions checked against what the model was shown, with their evidence attached."""
    items: list[dict]

from ..job_queue import commit_result


def _zone() -> ZoneInfo:
    return ZoneInfo(get_settings().crm_timezone)


def _today() -> date:
    return datetime.now(_zone()).date()


def fingerprint(kind: str, value: str, when: str | None) -> str:
    return sha256(f"{kind}|{_normal(value)}|{when or ''}".encode()).hexdigest()


def _normal(value: str | None) -> str:
    return re.sub(r"\s+", "", value or "").casefold()


def birthday_parts(value: str, today: date) -> dict | None:
    """MM-DD or YYYY-MM-DD as significant-date parts; None when it is not a real past birthday."""
    match = re.fullmatch(r"(?:(\d{4})-)?(\d{1,2})-(\d{1,2})", value.strip())
    if not match:
        return None
    year, month, day = (int(part) if part else None for part in match.groups())
    try:
        date(year or 2000, month, day)
    except ValueError:
        return None
    if year and (date(year, month, day) > today or year < 1900):
        return None
    return {"precision": "exact" if year else "month_day", "year": year, "month": month, "day": day}


def already_known(kind: str, value: str, known: dict[str, str], today: date) -> bool:
    """Whether the profile already says this; a meetup is a new record each time."""
    if kind == "meetup":
        return False
    if kind == "birthday":
        parts = birthday_parts(value, today)
        return bool(parts) and known.get("birthday") == f"{parts['month']:02}-{parts['day']:02}"
    return _normal(value) == _normal(known.get(kind))


def _meetup_day(value: str | None, today: date) -> date | None:
    try:
        day = date.fromisoformat(value or "")
    except ValueError:
        return None
    return day if today - timedelta(days=MEETUP_PAST_DAYS) <= day <= today + timedelta(days=MEETUP_AHEAD_DAYS) else None


def current_values(db, profile_ids: list[str] | None) -> dict[str, dict[str, str]]:
    """Per person, the values a suggestion would replace: fields by kind, and the birthday as MM-DD."""
    values: dict[str, dict[str, str]] = {}
    by_key = {key: kind for kind, key in FIELD_KEYS.items()}
    texts = field_texts(db, tuple(FIELD_KEYS.values()), profile_ids[0] if profile_ids and len(profile_ids) == 1 else None)
    for pid, fields in texts.items():
        values[pid] = {by_key[key]: value for key, value in fields.items()}
    rows = db.execute(text("""SELECT profile_id::text, month, day FROM profile_dates
                              WHERE kind = 'birthday' AND (CAST(:ids AS uuid[]) IS NULL OR profile_id = ANY(CAST(:ids AS uuid[])))"""),
                      {"ids": profile_ids}).mappings()
    for row in rows:
        if row["month"] and row["day"]:
            values.setdefault(row["profile_id"], {})["birthday"] = f"{row['month']:02}-{row['day']:02}"
    return values


def updates_payload(db, profile_id: str, memory: dict | None, today: date) -> tuple[dict, dict[str, dict]]:
    """What the model sees, and the evidence each short line id stands for."""
    zone = _zone()
    since = datetime.combine(today - timedelta(days=WINDOW_DAYS), time(), zone)
    refs: dict[str, dict] = {}
    direct, group = [], []
    for index, line in enumerate(direct_lines(db, profile_id, since, DIRECT_LINES), 1):
        local = line["occurred_at"].astimezone(zone)
        refs[f"d{index}"] = {"kind": "direct", "id": line["message_id"], "conversation_id": line["conversation_id"],
                             "at": line["occurred_at"].isoformat(), "speaker": line["speaker"], "text": line["text"]}
        direct.append({"id": f"d{index}", "date": local.strftime("%Y-%m-%d %a %H:%M"), "speaker": line["speaker"], "text": line["text"]})
    for index, line in enumerate(group_lines(db, profile_id, since, GROUP_LINES), 1):
        refs[f"g{index}"] = {"kind": "group", "id": line["message_id"], "conversation_id": line["conversation_id"],
                             "at": line["occurred_at"].isoformat(), "group": line["group"], "text": line["text"]}
        group.append({"id": f"g{index}", "date": line["occurred_at"].astimezone(zone).strftime("%Y-%m-%d %a"),
                      "group": line["group"], "text": line["text"]})
    posts = []
    post_since = datetime.combine(today - timedelta(days=POST_WINDOW_DAYS), time(), zone)
    for index, post in enumerate(captions(db, [profile_id], post_since, POSTS), 1):
        refs[f"p{index}"] = {"kind": "post", "id": post["post_id"], "at": post["date"], "text": post["caption"]}
        if post.get("media_context"):
            visual = post["media_context"]
            parts = [post["caption"], visual.get("summary", "")]
            for image in visual.get("images", []):
                parts.extend([image.get("description"), image.get("text")])
            parts.extend(item["text"] for item in visual.get("ocr", []))
            for key, label in (("confirmed_people", "已确认的人脸"), ("possible_people", "待确认的人脸候选（未确认）")):
                if visual.get(key):
                    parts.append(label + "：" + "、".join(person["name"] for person in visual[key]))
            refs[f"p{index}"]["text"] = "\n".join(part for part in parts if part)
        posts.append({"id": f"p{index}", **{key: value for key, value in post.items() if key not in ("post_id", "profile_id")}})
    context = contact_contexts(db, [profile_id]).get(profile_id, {})
    for index, detail in enumerate([*context.get("details", []), *context.get("notes", [])], 1):
        ref = f"c{index}"
        detail["evidence_id"] = ref
        refs[ref] = {"kind": "profile", "id": profile_id, "at": detail["date"],
                     "text": "\n".join(str(detail[key]) for key in ("label", "title", "value", "body") if detail.get(key))}
    known = current_values(db, [profile_id]).get(profile_id, {})
    payload = {
        "today": today.isoformat(),
        "memory": {key: (memory or {}).get(key) for key in ("summary", "facts", "gaps")},
        "current": {LABELS[kind]: value for kind, value in known.items()},
        "direct_chat": direct, "group_chat": group, "posts": posts,
        "profile_context": context, "network": network_context(db, profile_id, today=today),
    }
    return payload, refs


def resolve(updates: ProfileUpdates, refs: dict[str, dict], known: dict[str, str], today: date) -> ResolvedUpdates:
    """Keep suggestions whose evidence the model was shown and that would change something."""
    items, seen = [], set()
    for item in updates.items:
        evidence = [refs[ref] for ref in dict.fromkeys(item.evidence) if ref in refs]
        value = item.value.strip()
        if not evidence or not value:
            continue
        when = None
        if item.kind == "birthday" and not birthday_parts(value, today):
            continue
        if item.kind == "meetup":
            day = _meetup_day(item.date, today)
            if not day:
                continue
            when = day.isoformat()
        if already_known(item.kind, value, known, today):
            continue
        key = fingerprint(item.kind, value, when)
        if key in seen:
            continue
        seen.add(key)
        items.append({"fingerprint": key, "kind": item.kind, "value": value, "date": when, "quote": item.quote.strip()[:80],
                      "confidence": item.confidence, "evidence": evidence})
    return ResolvedUpdates(items=items)


def suggestion_candidates(db, described: set[str], today: date) -> list[str]:
    """People outside the Memory set who still have recent chats or posts to suggest from."""
    zone = _zone()
    chat_since = datetime.combine(today - timedelta(days=WINDOW_DAYS), time(), zone)
    post_since = datetime.combine(today - timedelta(days=POST_WINDOW_DAYS), time(), zone)
    people = recent_speakers(db, chat_since, SUGGEST_MIN_DIRECT, SUGGEST_MIN_GROUP) + recent_posters(db, post_since, SUGGEST_MIN_POSTS)
    return [pid for pid in dict.fromkeys(people) if pid not in described]


def enqueue_all(db, _payload: dict) -> dict:
    """Queue one job per person, spaced out after any pass still running."""
    described = person_memory.candidates(db)
    thin = suggestion_candidates(db, set(described), _today())
    payloads = [{"profile_id": pid} for pid in described] + [{"profile_id": pid, "memory": False} for pid in thin]
    queued = enqueue_spaced(db, "ai.profile", payloads, SPACING_SECONDS)
    return {"eligible": len(described), "suggestion_only": len(thin), "queued": queued}


def refresh_one(db, payload: dict) -> dict:
    """Refresh a person's Memory, then suggestions from recent material."""
    profile_id = payload["profile_id"]
    if not person_memory.candidates(db, profile_id):
        return {"profile_id": profile_id, "skipped": "no active person evidence"}
    model = get_settings().ai_writer_model
    today = _today()
    described = False
    if payload.get("memory", payload.get("overview", True)):
        described = person_memory.refresh(db, profile_id)
        db.commit()
    stored = current(db, OVERVIEW, profile_id)
    model_input, refs = updates_payload(db, profile_id, stored["output"] if stored else None, today)
    if not (model_input["direct_chat"] or model_input["group_chat"] or model_input["posts"]
            or model_input["profile_context"].get("details") or model_input["profile_context"].get("notes")):
        db.execute(text("DELETE FROM ai_outputs WHERE kind = :kind AND subject = :id"), {"kind": UPDATES, "id": profile_id})
        commit_result(db)
        return {"profile_id": profile_id, "memory": described, "updates": "nothing recent"}
    known = current_values(db, [profile_id]).get(profile_id, {})
    suggested = generate(db, UPDATES, profile_id, model, UPDATES_SYSTEM, model_input, ResolvedUpdates,
                         call=lambda m, s, p, _schema: resolve(structured(m, s, p, ProfileUpdates), refs, known, today))
    commit_result(db)
    return {"profile_id": profile_id, "memory": described, "updates": suggested}


def pending(db, profile_ids: list[str] | None = None) -> dict[str, dict]:
    """Undecided suggestions per person, without ones the profile now already says."""
    rows = db.execute(text("""SELECT o.subject, o.output, o.generated_at, p.display_name FROM ai_outputs o
        JOIN profiles p ON p.id = CAST(o.subject AS uuid) AND p.archived_at IS NULL
        WHERE o.kind = :kind AND (CAST(:ids AS text[]) IS NULL OR o.subject = ANY(CAST(:ids AS text[])))"""),
                      {"kind": UPDATES, "ids": profile_ids}).mappings().all()
    if not rows:
        return {}
    ids = [row["subject"] for row in rows]
    decided = {(str(pid), key) for pid, key in db.execute(text(
        "SELECT profile_id, fingerprint FROM profile_suggestion_decisions WHERE profile_id = ANY(CAST(:ids AS uuid[]))"), {"ids": ids})}
    known = current_values(db, ids)
    today = _today()
    result = {}
    for row in rows:
        mine = known.get(row["subject"], {})
        items = [{**item, "label": LABELS[item["kind"]]} for item in row["output"]["items"]
                 if (row["subject"], item["fingerprint"]) not in decided
                 and not already_known(item["kind"], item["value"], mine, today)]
        if items:
            result[row["subject"]] = {"profile_id": row["subject"], "name": row["display_name"],
                                      "generated_at": row["generated_at"], "items": items}
    return result


@router.get("/api/v1/ai/profiles/{profile_id}")
def profile_ai(profile_id: UUID, db: Session = Depends(get_db), _user=Depends(current_user)):
    if not get_settings().ai_enabled:
        return {"enabled": False}
    memory = current(db, OVERVIEW, str(profile_id))
    queued = db.execute(text("""SELECT EXISTS (SELECT 1 FROM jobs WHERE job_type IN ('ai.profile','ai.person_week') AND status IN ('queued', 'running')
                                AND payload->>'profile_id' = :id AND available_at <= now() + interval '10 minutes')"""),
                        {"id": str(profile_id)}).scalar_one()
    mine = pending(db, [str(profile_id)]).get(str(profile_id))
    weeks = db.execute(text("""SELECT week_start,output,generated_at FROM person_weekly_updates
        WHERE profile_id=:id ORDER BY week_start DESC LIMIT 12"""), {"id": profile_id}).mappings()
    shown = {**memory["output"], "generated_at": memory["generated_at"]} if memory else None
    return {"enabled": True, "queued": queued, "memory": shown,
            "weekly_updates": [{"week_start": row["week_start"], "generated_at": row["generated_at"],
                                **row["output"]} for row in weeks],
            "suggestions": mine["items"] if mine else []}


@router.post("/api/v1/ai/profiles/{profile_id}/refresh", status_code=202)
def refresh_profile(profile_id: UUID, db: Session = Depends(get_db), _user=Depends(current_user)):
    if not get_settings().ai_enabled or not get_profile(db, profile_id):
        raise HTTPException(404, "Unknown profile or model features are off")
    from ..job_queue import enqueue
    enqueue(db, 'ai.profile', {"profile_id": str(profile_id)}, refresh=True)
    latest = _today() - timedelta(days=_today().weekday() + 7)
    enqueue(db, 'ai.person_week', {"profile_id": str(profile_id), "week_start": latest.isoformat(),
                                   "subject_key": f"{profile_id}:{latest}"}, refresh=True)
    db.commit()
    return {"profile_id": str(profile_id), "queued": True}


@router.get("/api/v1/ai/suggestions")
def list_suggestions(kind: str | None = None, offset: int = 0, limit: int = 30, db: Session = Depends(get_db),
                     _user=Depends(current_user)):
    if not get_settings().ai_enabled:
        return {"enabled": False, "total": 0, "people": []}
    everyone = list(pending(db).values())
    counts = Counter(item["kind"] for person in everyone for item in person["items"])
    people = [{**person, "items": [item for item in person["items"] if kind in (None, item["kind"])]} for person in everyone]
    # Newest evidence first, so what just came up in a chat is at the top.
    people = sorted((person for person in people if person["items"]),
                    key=lambda person: max(ref["at"] for item in person["items"] for ref in item["evidence"]), reverse=True)
    page = people[max(offset, 0):max(offset, 0) + min(max(limit, 0), 100)]
    avatars = avatar_map(db, [person["profile_id"] for person in page])
    return {"enabled": True, "total": len(people), "items": sum(len(person["items"]) for person in people), "kinds": dict(counts),
            "people": [{**person, "avatar_media_id": avatars.get(person["profile_id"])} for person in page]}


class Decision(BaseModel):
    action: Literal["accept", "ignore"]
    value: str | None = None
    date: str | None = None


def _apply(db, profile_id: UUID, item: dict, body: Decision, user) -> dict:
    value = (body.value if body.value is not None else item["value"]).strip()
    if not value:
        raise HTTPException(422, "empty value")
    if item["kind"] in FIELD_KEYS:
        append_field_revision(db, profile_id=profile_id, field_key=FIELD_KEYS[item["kind"]], value=value, source_type="manual",
                              operation="manual_edit", source_record_id=f"ai:{item['fingerprint']}", actor_user_id=user["id"])
        return {"field_key": FIELD_KEYS[item["kind"]], "value": value}
    if item["kind"] == "birthday":
        parts = birthday_parts(value, _today())
        if not parts:
            raise HTTPException(422, "birthday must be MM-DD or YYYY-MM-DD")
        try:
            saved = create_date(db, profile_id, user["id"], kind="birthday", label=None, **parts)
        except DateError as exc:
            raise HTTPException(409 if exc.code == "birthday_exists" else 422, exc.code) from exc
        return {"date_id": str(saved["id"]), **parts}
    day = _meetup_day(body.date or item["date"], _today())
    if not day:
        raise HTTPException(422, "meetup date must be YYYY-MM-DD within the suggestion window")
    activity = db.execute(text("""INSERT INTO activities(activity_type, occurred_at, title, body, source_type, created_by)
        VALUES ('activity', :at, :title, :body, 'manual', :user) RETURNING id"""),
        {"at": datetime.combine(day, time(12), _zone()), "title": value[:255], "body": item["quote"] or None,
         "user": user["id"]}).scalar_one()
    db.execute(text("INSERT INTO activity_participants(activity_id, profile_id) VALUES (:a, :p)"), {"a": activity, "p": profile_id})
    return {"activity_id": str(activity), "date": day.isoformat(), "title": value}


@router.post("/api/v1/ai/suggestions/{profile_id}/{key}")
def decide(profile_id: UUID, key: str, body: Decision, db: Session = Depends(get_db), user=Depends(current_user)):
    item = next((item for item in pending(db, [str(profile_id)]).get(str(profile_id), {}).get("items", [])
                 if item["fingerprint"] == key), None)
    if not item:
        raise HTTPException(404, "No pending suggestion")
    applied = _apply(db, profile_id, item, body, user) if body.action == "accept" else None
    db.execute(text("""INSERT INTO profile_suggestion_decisions(profile_id, fingerprint, kind, decision, applied)
        VALUES (:id, :key, :kind, :decision, CAST(:applied AS jsonb))"""),
               {"id": profile_id, "key": key, "kind": item["kind"], "decision": "accepted" if applied else "ignored",
                "applied": None if applied is None else json.dumps(applied, ensure_ascii=False)})
    db.execute(text("""INSERT INTO audit_events(user_id, action, entity_type, entity_id, metadata)
        VALUES (:user, :action, 'profile', :id, CAST(:metadata AS jsonb))"""),
               {"user": user["id"], "action": f"profile.suggestion.{'accepted' if applied else 'ignored'}", "id": profile_id,
                "metadata": json.dumps({"kind": item["kind"], "fingerprint": key})})
    db.commit()
    return {"profile_id": str(profile_id), "fingerprint": key, "decision": "accepted" if applied else "ignored", "applied": applied}


@router.delete("/api/v1/ai/suggestions/{profile_id}/{key}")
def undo_ignore(profile_id: UUID, key: str, db: Session = Depends(get_db), _user=Depends(current_user)):
    """Undo an ignore; an accepted suggestion is changed on the profile itself."""
    removed = db.execute(text("""DELETE FROM profile_suggestion_decisions
        WHERE profile_id = :id AND fingerprint = :key AND decision = 'ignored'"""), {"id": profile_id, "key": key}).rowcount
    db.commit()
    if not removed:
        raise HTTPException(404, "No ignored suggestion")
    return {"profile_id": str(profile_id), "fingerprint": key, "decision": None}
