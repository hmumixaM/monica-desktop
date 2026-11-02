"""Shared import services; transaction ownership stays with the caller."""
from datetime import UTC, datetime
from typing import Any

from .schemas import ImportBatchRequest


def _record_entity(record, request: ImportBatchRequest, content: Any) -> str | None:
    """Resolve the normalized entity type without requiring provider-specific schemas."""
    if record.entity_type:
        return record.entity_type
    if isinstance(content, dict) and content.get("entity_type"):
        return str(content["entity_type"])
    if request.stream in {"posts", "moments"}:
        return "post"
    if request.stream == "stories":
        return "story"
    return None


def _content_value(content: Any) -> Any:
    return content if isinstance(content, dict) else {"value": content}


def _parse_datetime(value: Any) -> Any:
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(value, UTC)
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(value, str):
        value = value.strip()
        if value.isdigit() or (value.startswith("-") and value[1:].isdigit()):
            try:
                return datetime.fromtimestamp(int(value), UTC)
            except (OverflowError, OSError, ValueError):
                return None
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None
    return value
