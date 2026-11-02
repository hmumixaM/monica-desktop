"""Read one person's group speech from Chatlog Alpha without storing it.

Monica only resolves which WeChat account and which rooms to ask about.
The transcript stays on the NAS, and the database connection is released
before the slow HTTP call.
"""

from __future__ import annotations

import asyncio
import json
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from uuid import UUID

import httpx
from sqlalchemy import text
from sqlalchemy.orm import Session

Opener = Callable[..., object]
FETCH_CAP = 2000


def person_senders(db: Session, profile_id: UUID) -> list[dict]:
    rows = db.execute(
        text(
            """
            SELECT a.external_id, coalesce(a.username, '') AS username,
                   coalesce(nullif(btrim(a.display_name), ''), a.external_id) AS display_name
            FROM source_account_links l
            JOIN source_accounts a ON a.id = l.account_id
            WHERE l.profile_id = :profile
              AND a.provider = 'wechat' AND a.object_kind = 'person'
              AND a.external_id NOT LIKE '%@chatroom'
              AND a.external_id NOT LIKE 'gh_%'
              AND a.external_id NOT LIKE '%@openim'
            ORDER BY a.external_id
            """
        ),
        {"profile": profile_id},
    ).mappings().all()
    return [dict(row) for row in rows]


def person_groups(db: Session, profile_id: UUID, group_id: UUID | None) -> list[dict]:
    clauses = [
        "l.profile_id = :profile",
        "m.left_at IS NULL",
        "g.object_kind = 'group'",
    ]
    params: dict = {"profile": profile_id}
    if group_id is not None:
        clauses.append("gp.id = :group_id")
        params["group_id"] = group_id
    rows = db.execute(
        text(
            f"""
            SELECT DISTINCT gp.id AS group_id, g.display_name AS name, g.external_id AS chat
            FROM source_account_links l
            JOIN source_group_memberships m ON m.person_account_id = l.account_id
            JOIN source_accounts g ON g.id = m.group_account_id
            LEFT JOIN source_account_links gl ON gl.account_id = g.id
            LEFT JOIN profiles gp ON gp.id = gl.profile_id AND gp.profile_type = 'group'
            WHERE {' AND '.join(clauses)}
            ORDER BY g.display_name
            """
        ),
        params,
    ).mappings().all()
    return [dict(row) for row in rows]


def sender_keys(senders: list[dict]) -> set[str]:
    keys = set()
    for sender in senders:
        # Display names collide. Only the WeChat id and alias identify a sender.
        for value in (sender.get("external_id"), sender.get("username")):
            text_value = str(value or "").strip()
            if text_value:
                keys.add(text_value.casefold())
    return keys


def spoken_by(message: dict, keys: set[str]) -> bool:
    if not keys:
        return False
    sender = str(message.get("sender") or "").strip().casefold()
    content = str(message.get("content") or "")
    prefix = content.split(":\n", 1)[0].strip().casefold()
    return sender in keys or prefix in keys


def display_text(content: str) -> str:
    body = content.split(":\n", 1)[-1].strip()
    title_at = body.find("<title>")
    if title_at >= 0:
        end = body.find("</title>", title_at)
        if end > title_at:
            return body[title_at + len("<title>") : end].strip()
    return body


def filter_messages(messages: list[dict], keys: set[str], query: str, limit: int) -> list[dict]:
    needle = query.casefold().strip()
    kept = []
    for message in messages:
        if not spoken_by(message, keys):
            continue
        content = str(message.get("content") or "")
        text_value = display_text(content)
        if needle and needle not in content.casefold() and needle not in text_value.casefold():
            continue
        kept.append(
            {
                "time": message.get("time"),
                "timestamp": message.get("timestamp"),
                "sender": message.get("sender") or "",
                "text": text_value,
                "type": message.get("type") or "",
            }
        )
        if len(kept) >= limit:
            break
    return kept


SENDER_FILTER = """(EXISTS (SELECT 1 FROM source_account_links own
    WHERE own.account_id=m.sender_account_id AND own.profile_id=:profile)
    OR (m.sender_account_id IS NULL AND m.sender_profile_id=:profile))"""


def spoken_rooms(db, profile_id, group_id=None):
    """Discover historical rooms without fetching their message bodies."""
    return [dict(row) for row in db.execute(text(f"""SELECT DISTINCT g.id::text AS group_id,
        g.display_name AS name,regexp_replace(c.external_id,'^group:','') AS chat
        FROM conversations c JOIN profiles g ON g.id=c.profile_id
        WHERE c.conversation_type='group' AND g.archived_at IS NULL
          AND (CAST(:group_id AS uuid) IS NULL OR g.id=:group_id)
          AND EXISTS (SELECT 1 FROM message_events m WHERE m.conversation_id=c.id AND {SENDER_FILTER})
        ORDER BY name,chat"""), {'profile': profile_id, 'group_id': group_id}).mappings()]


def imported_group_messages(db: Session, profile_id: UUID, group_ids=None) -> list[dict]:
    """Fetch at most 200 stored messages per selected room, before loading bodies."""
    rows = db.execute(text(f"""WITH selected AS (
        SELECT g.id::text AS group_id,g.display_name AS name,
          regexp_replace(c.external_id,'^group:','') AS chat,m.occurred_at,m.content_object_id,
          row_number() OVER(PARTITION BY c.id ORDER BY m.occurred_at DESC,m.id DESC) AS position
        FROM message_events m JOIN conversations c ON c.id=m.conversation_id AND c.conversation_type='group'
        JOIN profiles g ON g.id=c.profile_id AND g.profile_type='group' AND g.archived_at IS NULL
        WHERE {SENDER_FILTER} AND g.id=ANY(CAST(:groups AS uuid[])))
        SELECT group_id,name,chat,to_char(occurred_at AT TIME ZONE 'UTC','YYYY-MM-DD HH24:MI') AS time,
          coalesce(o.payload_json->>'text','') AS content
        FROM selected LEFT JOIN content_objects o ON o.id=content_object_id
        WHERE position<=200 ORDER BY occurred_at"""),
        {'profile': profile_id, 'groups': [str(value) for value in group_ids or [] if value]}).mappings()
    return [dict(row) for row in rows]


def with_spoken_rooms(groups: list[dict], imported: list[dict]) -> list[dict]:
    """Keep current memberships, and add rooms this person has already spoken in."""
    seen = {str(group.get("chat") or "") for group in groups}
    extra = []
    for message in imported:
        chat = str(message.get("chat") or "")
        if not chat.endswith("@chatroom") or chat in seen:
            continue
        seen.add(chat)
        extra.append({"group_id": message.get("group_id"), "name": message.get("name"), "chat": chat})
    return [*groups, *extra]


def fetch_history(base_url: str, chat: str, since: str, until: str, timeout: int, opener: Opener, when: str = "") -> list[dict]:
    params = {"format": "json", "chat": chat, "limit": str(FETCH_CAP)}
    if when:
        params["time"] = when
    else:
        if since:
            params["since"] = since
        if until:
            params["until"] = until
    url = base_url.rstrip("/") + "/api/v1/history?" + urllib.parse.urlencode(params)
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    with opener(request, timeout=timeout) as response:
        payload = json.load(response)
    if isinstance(payload, dict):
        messages = payload.get("messages") or []
    elif isinstance(payload, list):
        messages = payload
    else:
        messages = []
    return [message for message in messages if isinstance(message, dict)]


def _speech_days(imported: list[dict], chat: str) -> list[str]:
    days = sorted({str(message.get("time") or "")[:10] for message in imported if message.get("chat") == chat})
    return [day for day in days if day][-8:]


def _imported_rows(imported: list[dict], chat: str, query: str) -> list[dict]:
    needle = query.casefold().strip()
    rows = []
    for message in imported:
        if message.get("chat") != chat:
            continue
        content = str(message.get("content") or "")
        text_value = display_text(content)
        if needle and needle not in content.casefold() and needle not in text_value.casefold():
            continue
        rows.append({"time": message.get("time"), "timestamp": None, "sender": "", "text": text_value, "type": "text"})
    return rows


def _merge_messages(found: list[dict], imported_rows: list[dict], limit: int) -> list[dict]:
    merged = []
    seen = set()
    for message in [*imported_rows, *found]:
        key = (str(message.get("time") or "")[:10], str(message.get("text") or "").strip())
        if key in seen:
            continue
        seen.add(key)
        merged.append(message)
    merged.sort(key=lambda message: str(message.get("time") or ""))
    return merged[:limit]


def group_speech(
    *,
    senders: list[dict],
    groups: list[dict],
    query: str,
    since: str,
    until: str,
    limit: int,
    base_url: str,
    timeout: int,
    opener: Opener = urllib.request.urlopen,
    imported: list[dict] | None = None,
    window_index: int = 0,
    budget: float = 15,
) -> dict:
    if opener is urllib.request.urlopen:
        return asyncio.run(_live_speech(senders, groups, query, since, until, limit, base_url, imported or [], window_index, budget))
    keys = sender_keys(senders)
    stored = imported or []
    rooms = []
    for group in groups:
        chat = str(group.get("chat") or "")
        saved = _imported_rows(stored, chat, query)
        if not chat.endswith("@chatroom"):
            rooms.append({**_room(group, chat), "messages": saved[:limit], "error": None if saved else "这个群没有 Chatlog 房间号"})
            continue
        windows = [""]
        if not since and not until:
            windows.extend(_speech_days(stored, chat))
        collected = []
        error = None
        for when in windows:
            try:
                collected.extend(fetch_history(base_url, chat, since, until, timeout, opener, when))
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as failure:
                error = str(failure)
        found = filter_messages(collected, keys, query, limit)
        messages = _merge_messages(found, saved, limit)
        rooms.append({**_room(group, chat), "messages": messages, "error": None if messages else error})
    return {
        "senders": sorted(keys),
        "groups": rooms,
    }


def _room(group: dict, chat: str) -> dict:
    return {
        "group_id": str(group["group_id"]) if group.get("group_id") else None,
        "name": group.get("name") or chat,
        "chat": chat,
    }


async def _live_speech(senders, groups, query, since, until, limit, base_url, stored, window_index=0, budget=15):
    keys = sender_keys(senders)
    rooms = [{**_room(g, str(g.get("chat") or "")),
              "messages": _imported_rows(stored, str(g.get("chat") or ""), query)[:limit],
              "error": "读取尚未完成", "partial": True, "next_window": window_index} for g in groups]
    capacity = asyncio.Semaphore(2)
    async with httpx.AsyncClient(timeout=15) as client:
        async def read(index, group):
            chat = str(group.get("chat") or "")
            if not chat.endswith("@chatroom"):
                rooms[index].update(error="这个群没有 Chatlog 房间号", partial=False)
                return
            collected = []
            windows = [""] + (_speech_days(stored, chat) if not since and not until else [])
            for position in range(window_index, len(windows)):
                when = windows[position]
                params = {"format": "json", "chat": chat, "limit": str(FETCH_CAP)}
                if when:
                    params["time"] = when
                else:
                    params.update({k: v for k, v in (("since", since), ("until", until)) if v})
                try:
                    async with capacity:
                        response = await client.get(base_url.rstrip("/")+"/api/v1/history", params=params)
                        response.raise_for_status()
                    payload = response.json()
                    collected.extend(payload.get("messages", []) if isinstance(payload, dict) else payload)
                    rooms[index]["messages"] = _merge_messages(
                        filter_messages(collected, keys, query, limit), _imported_rows(stored, chat, query), limit)
                    rooms[index]["next_window"] = position+1 if position+1 < len(windows) else None
                except (httpx.HTTPError, ValueError, TypeError) as exc:
                    rooms[index]["error"] = str(exc)[:200]
                    return
            rooms[index].update(error=None, partial=False)
        try:
            async with asyncio.timeout(max(.01, budget)):
                await asyncio.gather(*(read(i, g) for i, g in enumerate(groups)))
        except TimeoutError:
            for room in rooms:
                if room["partial"]:
                    room["error"] = "已达到读取时限；可选择这个群继续读取"
    return {"senders": sorted(keys), "groups": rooms, "partial": any(r["partial"] for r in rooms)}
