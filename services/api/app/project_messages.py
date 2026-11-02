"""Shared import services; transaction ownership stays with the caller."""
from typing import Any

from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.orm import Session

from .content import get_or_create_content
from .import_common import _content_value, _parse_datetime, _record_entity
from .schemas import ImportBatchRequest


def _project_message_record(db: Session, record, request: ImportBatchRequest, content: Any) -> None:
    entity_type = _record_entity(record, request, content)
    if entity_type != "message" and request.stream not in {"messages", "chat", "conversations"}:
        return
    normalized = _content_value(content)
    conversation_external_id = record.conversation_external_id or str(
        normalized.get("conversation_external_id") or record.external_id
    )
    conversation_type = record.conversation_type or str(
        normalized.get("conversation_type", "direct")
    )
    if conversation_type not in {"direct", "group"}:
        conversation_type = "direct"
    message_type = record.message_type or str(normalized.get("message_type", "text"))
    occurred_at = _parse_datetime(record.occurred_at or normalized.get("occurred_at"))
    if occurred_at is None:
        raise HTTPException(status_code=422, detail="message records require occurred_at")
    content_id, _ = get_or_create_content(db, value=normalized, content_kind="message")
    conversation_id = db.execute(
        text("""
          INSERT INTO conversations(conversation_type, profile_id, external_id)
          VALUES (:conversation_type, :profile_id, :external_id)
          ON CONFLICT (conversation_type, external_id) DO UPDATE SET
            profile_id = COALESCE(EXCLUDED.profile_id, conversations.profile_id)
          RETURNING id
        """),
        {
            "conversation_type": conversation_type,
            "profile_id": record.profile_id,
            "external_id": conversation_external_id,
        },
    ).scalar_one()
    db.execute(
        text("""
          INSERT INTO message_events
            (conversation_id, sender_profile_id, occurred_at, message_type,
             content_object_id, source_record_id)
          VALUES (:conversation_id, :sender_profile_id, :occurred_at, :message_type,
                  :content_id, :source_record_id)
          ON CONFLICT (conversation_id, source_record_id, occurred_at) DO NOTHING
        """),
        {
            "conversation_id": conversation_id,
            "sender_profile_id": record.sender_profile_id,
            "occurred_at": occurred_at,
            "message_type": message_type,
            "content_id": content_id,
            "source_record_id": record.external_id,
        },
    )
