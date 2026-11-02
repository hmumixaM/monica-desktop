"""Per-person evidence used by long-term memory and weekly notes.

Model inputs are page-sized; the page loop has no total-row cap. Only Monica's
stored chat is read. Visual descriptions and OCR come from saved readings.
"""

import json
from datetime import datetime

from sqlalchemy import text

from .chat_excerpts import direct_lines, group_lines
from .payloads import SOCIAL_EVENTS, _social_rows, read

PAGE = 60


def _profile_rows(db, sql: str, params: dict) -> list[dict]:
    """Current profile revisions store their values in content_objects; never query chat here."""
    return [dict(row) for row in db.execute(text(sql), params).mappings()]


def social(db, profile_id: str, start: datetime | None = None, end: datetime | None = None) -> list[dict]:
    """All relevant posts and Stories, including newly recognized old material in a weekly window."""
    week = start is not None and end is not None
    condition = """AND (e.occurred_at >= :start AND e.occurred_at < :end
      OR EXISTS (SELECT 1 FROM social_posts p WHERE e.event_type='post' AND p.id=e.id
                   AND p.ai_seen_at>=:start AND p.ai_seen_at<:end)
      OR EXISTS (SELECT 1 FROM social_stories s WHERE e.event_type='story' AND s.id=e.id
                   AND s.ai_seen_at>=:start AND s.ai_seen_at<:end)
      OR EXISTS (SELECT 1 FROM social_post_media_readings r WHERE e.event_type='post' AND r.post_id=e.id
                   AND r.generated_at>=:start AND r.generated_at<:end)
      OR EXISTS (SELECT 1 FROM social_story_media_readings r WHERE e.event_type='story' AND r.story_id=e.id
                   AND r.generated_at>=:start AND r.generated_at<:end)
      OR EXISTS (SELECT 1 FROM media_links l JOIN image_ocr_readings r ON r.media_id=l.media_id
                   WHERE l.entity_id=e.id AND l.entity_type=CASE WHEN e.event_type='post' THEN 'social_post' ELSE 'social_story' END
                     AND r.status='complete' AND r.generated_at>=:start AND r.generated_at<:end))""" if week else ""
    result = []
    offset = 0
    while True:
        rows = read(db, f"""WITH e AS ({SOCIAL_EVENTS})
          SELECT e.id::text,e.profile_id::text,e.provider,e.occurred_at,e.search_body,e.event_type
          FROM e WHERE e.profile_id=CAST(:id AS uuid) {condition}
          ORDER BY e.occurred_at DESC NULLS LAST,e.id LIMIT :page OFFSET :offset""",
                    {"id": profile_id, "start": start, "end": end, "page": PAGE, "offset": offset})
        occurred = {row["id"]: row["occurred_at"] for row in rows}
        for item in _social_rows(db, rows, full=True):
            happened = occurred[item["post_id"]]
            item["discovered"] = bool(week and (happened is None or not start <= happened < end))
            result.append(item)
        if len(rows) < PAGE:
            break
        offset += PAGE
    return result


def chat(db, profile_id: str, start: datetime, end: datetime | None = None, include_discovered: bool = False) -> list[dict]:
    """Both sides of direct chat and only this person's group speech, without a line cap."""
    seen = start if include_discovered else None
    lines = []
    for kind, reader in (("direct", direct_lines), ("group", group_lines)):
        for row in reader(db, profile_id, start, None, end, seen, full=True):
            happened = row["occurred_at"]
            item = {"kind": kind, "source_id": row["message_id"], "conversation_id": row["conversation_id"],
                    "at": happened.isoformat(), "text": row["text"],
                    "discovered": bool(end and not start <= happened < end)}
            if kind == "direct":
                item["speaker"] = row["speaker"]
            else:
                item["group"] = row["group"]
            lines.append(item)
    return sorted(lines, key=lambda row: (row["at"], row["source_id"]))


def profile_changes(db, profile_id: str, start: datetime, end: datetime) -> list[dict]:
    """Current values changed this week, plus dated notes, tags and account links."""
    params = {"id": profile_id, "start": start, "end": end}
    result = []
    for row in read(db, """SELECT id::text AS source_id,display_name,summary,ai_changed_at
        FROM profiles WHERE id=CAST(:id AS uuid) AND ai_changed_at>=:start AND ai_changed_at<:end""", params):
        result.append({"kind": "profile", "source_id": row["source_id"], "at": row["ai_changed_at"].isoformat(),
                       "text": f"人物名称／简介更新：{row['display_name']}；{row['summary'] or ''}"})
    fields = _profile_rows(db, """SELECT c.field_key,c.updated_at,c.current_revision_id::text AS source_id,o.payload_json AS value
        FROM profile_field_current c JOIN profile_field_revisions r ON r.id=c.current_revision_id
        JOIN content_objects o ON o.id=r.content_object_id
        WHERE c.profile_id=CAST(:id AS uuid) AND c.updated_at>=:start AND c.updated_at<:end""", params)
    for row in fields:
        result.append({"kind": "profile", "source_id": row["source_id"], "at": row["updated_at"].isoformat(),
                       "text": f"资料 {row['field_key']} 更新为：{json.dumps(row['value'], ensure_ascii=False)}"})
    for row in _profile_rows(db, """SELECT c.fact_type,c.current_revision_id::text AS source_id,c.updated_at,o.payload_json AS value
        FROM profile_structured_current c
        LEFT JOIN profile_address_revisions ar ON c.fact_type='address' AND ar.id=c.current_revision_id
        LEFT JOIN profile_employment_revisions er ON c.fact_type='employment' AND er.id=c.current_revision_id
        LEFT JOIN profile_education_revisions ed ON c.fact_type='education' AND ed.id=c.current_revision_id
        JOIN content_objects o ON o.id=COALESCE(ar.content_object_id,er.content_object_id,ed.content_object_id)
        WHERE c.profile_id=CAST(:id AS uuid) AND c.updated_at>=:start AND c.updated_at<:end""", params):
        result.append({"kind": "profile", "source_id": row["source_id"], "at": row["updated_at"].isoformat(),
                       "text": f"{row['fact_type']} 资料更新：{json.dumps(row['value'], ensure_ascii=False)}"})
    for row in read(db, """SELECT id::text AS source_id,kind,label,year,month,day,updated_at
        FROM profile_dates WHERE profile_id=CAST(:id AS uuid) AND updated_at>=:start AND updated_at<:end""", params):
        result.append({"kind": "profile", "source_id": row["source_id"], "at": row["updated_at"].isoformat(),
                       "text": f"日期资料 {row['kind']} {row['label'] or ''}：{row['year'] or ''}-{row['month'] or ''}-{row['day'] or ''}"})
    for row in read(db, """SELECT a.id::text AS source_id,a.created_at,a.occurred_at,a.title,a.body
        FROM activities a JOIN activity_participants ap ON ap.activity_id=a.id
        WHERE ap.profile_id=CAST(:id AS uuid) AND a.created_at>=:start AND a.created_at<:end""", params):
        result.append({"kind": "profile", "source_id": row["source_id"], "at": row["created_at"].isoformat(),
                       "text": f"新增记录（事情发生于 {row['occurred_at'].date()}）：{row['title']} {row['body'] or ''}"[:1200]})
    for row in read(db, """SELECT t.id::text AS source_id,t.name,m.created_at
        FROM tag_memberships m JOIN tags t ON t.id=m.tag_id
        WHERE m.profile_id=CAST(:id AS uuid) AND m.created_at>=:start AND m.created_at<:end""", params):
        result.append({"kind": "profile", "source_id": row["source_id"], "at": row["created_at"].isoformat(),
                       "text": f"新增标签：{row['name']}"})
    for row in read(db, """SELECT a.id::text AS source_id,a.provider,a.display_name,l.linked_at
        FROM source_account_links l JOIN source_accounts a ON a.id=l.account_id
        WHERE l.profile_id=CAST(:id AS uuid) AND l.linked_at>=:start AND l.linked_at<:end""", params):
        result.append({"kind": "profile", "source_id": row["source_id"], "at": row["linked_at"].isoformat(),
                       "text": f"新关联 {row['provider']} 账号：{row['display_name']}"})
    for row in read(db, """SELECT m.group_profile_id::text AS source_id,g.display_name,m.joined_at
        FROM group_memberships m JOIN profiles g ON g.id=m.group_profile_id
        WHERE m.person_profile_id=CAST(:id AS uuid) AND m.joined_at>=:start AND m.joined_at<:end""", params):
        result.append({"kind": "profile", "source_id": row["source_id"], "at": row["joined_at"].isoformat(),
                       "text": f"加入群组：{row['display_name']}"})
    for row in read(db, """SELECT e.id::text AS source_id,e.created_at,t.name AS relationship,p.display_name AS other
        FROM relationship_edges e JOIN relationship_types t ON t.id=e.relationship_type_id
        JOIN profiles p ON p.id=CASE WHEN e.from_profile_id=CAST(:id AS uuid) THEN e.to_profile_id ELSE e.from_profile_id END
        WHERE (e.from_profile_id=CAST(:id AS uuid) OR e.to_profile_id=CAST(:id AS uuid))
          AND e.evidence_kind='relationship' AND e.created_at>=:start AND e.created_at<:end""", params):
        result.append({"kind": "profile", "source_id": row["source_id"], "at": row["created_at"].isoformat(),
                       "text": f"新记录的关系：与 {row['other']} 是{row['relationship']}"})
    return sorted(result, key=lambda row: (row["at"], row["source_id"]))


def batches(items: list[dict], max_chars: int = 30000) -> list[list[dict]]:
    """Bound each model call while retaining every source, including long OCR readings."""
    groups, group, size = [], [], 0
    for item in items:
        encoded = json.dumps(item, ensure_ascii=False, default=str)
        if len(encoded) > max_chars:
            # Keep every character of an unusually long caption/OCR reading.
            # The fragment is JSON-encoded a second time; reserve room for escaping.
            width = max(1, (max_chars - 500) // 2)
            fragments = [encoded[n:n + width] for n in range(0, len(encoded), width)]
            expanded = [{"kind": item.get("kind"), "id": item.get("id"), "source_id": item.get("source_id"),
                         "part": index, "parts": len(fragments), "fragment": fragment}
                        for index, fragment in enumerate(fragments, 1)]
        else:
            expanded = [item]
        for piece in expanded:
            piece_size = len(json.dumps(piece, ensure_ascii=False, default=str))
            if group and size + piece_size > max_chars:
                groups.append(group)
                group, size = [], 0
            group.append(piece)
            size += piece_size
    if group:
        groups.append(group)
    return groups
