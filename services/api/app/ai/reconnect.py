"""Reconnect: people you used to talk to a lot and haven't in a while, with a reason and an opener.

Every request draws a fresh weighted shuffle of the whole pool, so the home page keeps changing while
closer, longer-quiet people come up more often. Reasons are generated for the whole pool, one job per person.
"""

import random
from datetime import UTC, datetime, timedelta
from math import sqrt
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
from .client import input_hash, structured
from .payloads import VISUAL_RULES, reconnect_candidates, reconnect_payload
from .prompts import CONTEXT_RULES
from .queue import enqueue_spaced
from .schemas import Reconnect
from .store import current, generate, outputs

router = APIRouter()
KIND = "reconnect"
PEAK = 100
DORMANT_DAYS = 60
POSTS_DAYS = 90
SHOW = 30
DAILY_CALLS = 120
# One small model call each; the gap lets imports and downloads queued meanwhile run in between.
SPACING_SECONDS = 30
SNOOZE_DAYS = {"2w": 14, "1m": 30}
POST_BOOST = 1.5

SYSTEM = """你在帮用户维护私人关系。输入是一位曾经和用户联系很多、但已经几个月没联系的人：
TA 的名字、标签、公司、学校，用户和 TA 每年的消息数量（只有数量），以及 TA 最近 90 天发的动态。
请用中文写：
- reason：一句话（30 字以内）说明为什么现在值得联系，优先引用 TA 最近动态里的具体事情；没有动态时根据交往历史。
- opener：一句自然、口语化的开场白，像老朋友发微信，不要客套，不要提“好久不见”以外的套话，不要编造输入里没有的事。
- sources：reason 引用的动态（kind=post，id=post_id）；没引用动态时引用这个人（kind=profile，id=profile_id）。"""


SYSTEM += "\n" + VISUAL_RULES + "\n" + CONTEXT_RULES

from ..job_queue import commit_result


def _today():
    return datetime.now(ZoneInfo(get_settings().crm_timezone)).date()


def _since():
    return datetime.now(ZoneInfo(get_settings().crm_timezone)) - timedelta(days=POSTS_DAYS)


def weight(person: dict, now: datetime) -> float:
    """How often someone should come up: closeness, times how overdue they are, more if they just posted.

    Overdue rises over the first year of quiet, then fades to 0.4 by year seven, so the long-lost still appear.
    """
    days = max((now - person["last_contact_at"]).days, 1)
    overdue = days / 365 if days <= 365 else max(0.4, 1 - (days - 365) / (365 * 10))
    return sqrt(person["peak"]) * overdue * (POST_BOOST if person["recent_posts"] else 1)


def shuffled(people: list[dict], now: datetime, rng: random.Random) -> list[dict]:
    """A weighted draw without replacement (Efraimidis–Spirakis): sort by u^(1/w)."""
    return sorted(people, key=lambda person: rng.random() ** (1 / weight(person, now)), reverse=True)


def refresh(db, _payload: dict) -> dict:
    """Queue a model call for each candidate whose reason is missing or out of date, missing ones first."""
    model = get_settings().ai_batch_model
    since, today = _since(), _today()
    missing, stale = [], []
    candidates = reconnect_candidates(db, PEAK, DORMANT_DAYS, POSTS_DAYS)
    for person in candidates:
        stored = current(db, KIND, person["profile_id"])
        if not stored:
            missing.append(person["profile_id"])
        elif stored["input_hash"] != input_hash(model, SYSTEM, reconnect_payload(db, person["profile_id"], today, since)):
            stale.append(person["profile_id"])
    due = (missing + stale)[:DAILY_CALLS]
    queued = enqueue_spaced(db, "ai.reconnect_one", [{"profile_id": pid} for pid in due], SPACING_SECONDS)
    return {"candidates": len(candidates), "missing": len(missing), "stale": len(stale), "queued": queued}


def refresh_one(db, payload: dict) -> dict:
    profile_id = payload["profile_id"]
    model_input = reconnect_payload(db, profile_id, _today(), _since())
    called = generate(db, KIND, profile_id, get_settings().ai_batch_model, SYSTEM, model_input, Reconnect, call=structured)
    commit_result(db)
    return {"profile_id": profile_id, "generated": called}


def _sources(output: dict, profile_id: str, posts: dict[str, str]) -> list[dict]:
    """Only sources that are this person's posts, or the person."""
    return [source for source in output.get("sources", [])
            if (source["kind"] == "post" and posts.get(source["id"]) == profile_id)
            or (source["kind"] == "profile" and source["id"] == profile_id)]


def cited_posts(db, ids: list[str]) -> dict[str, str]:
    """Post id -> author for the cited ids that exist."""
    valid = [value for value in ids if _is_uuid(value)]
    return {row[0]: row[1] for row in db.execute(text(
        """SELECT id::text, profile_id::text FROM social_posts WHERE id = ANY(CAST(:ids AS uuid[]))
        UNION ALL SELECT id::text, profile_id::text FROM social_stories WHERE id = ANY(CAST(:ids AS uuid[]))"""), {"ids": valid})}


def _is_uuid(value: str) -> bool:
    try:
        UUID(value)
    except ValueError:
        return False
    return True


@router.get("/api/v1/reconnect")
def list_reconnect(db: Session = Depends(get_db), _user=Depends(current_user)):
    if not get_settings().ai_enabled:
        return {"enabled": False, "people": []}
    people = shuffled(reconnect_candidates(db, PEAK, DORMANT_DAYS, POSTS_DAYS), datetime.now(UTC), random.Random())[:SHOW]
    generated = outputs(db, KIND)
    stored = {person["profile_id"]: generated[person["profile_id"]] for person in people if person["profile_id"] in generated}
    posts = cited_posts(db, [source["id"] for output in stored.values() for source in output.get("sources", []) if source["kind"] == "post"])
    result = []
    for person in people:
        output = stored.get(person["profile_id"])
        suggestion = {"reason": output["reason"], "opener": output["opener"],
                      "sources": _sources(output, person["profile_id"], posts)} if output else None
        result.append({**person, "suggestion": suggestion})
    return {"enabled": True, "people": result}


class ReconnectAction(BaseModel):
    state: Literal["done", "snoozed", "dismissed"]
    snooze: Literal["2w", "1m"] | None = None


@router.post("/api/v1/reconnect/{profile_id}")
def act_on_reconnect(profile_id: UUID, body: ReconnectAction, db: Session = Depends(get_db), _user=Depends(current_user)):
    if (body.state == "snoozed") != (body.snooze is not None):
        raise HTTPException(422, "snooze is required exactly when state is snoozed")
    until = datetime.now(ZoneInfo(get_settings().crm_timezone)) + timedelta(days=SNOOZE_DAYS[body.snooze]) if body.snooze else None
    db.execute(text("""INSERT INTO reconnect_states(profile_id, state, until) VALUES(:id, :state, :until)
                       ON CONFLICT (profile_id) DO UPDATE SET state = EXCLUDED.state, until = EXCLUDED.until, updated_at = now()"""),
               {"id": profile_id, "state": body.state, "until": until})
    db.commit()
    return {"profile_id": str(profile_id), "state": body.state, "until": until}


@router.delete("/api/v1/reconnect/{profile_id}")
def undo_reconnect(profile_id: UUID, db: Session = Depends(get_db), _user=Depends(current_user)):
    """Undo: candidates shown have no active state, so removing the row restores them."""
    db.execute(text("DELETE FROM reconnect_states WHERE profile_id = :id"), {"id": profile_id})
    db.commit()
    return {"profile_id": str(profile_id), "state": None}
