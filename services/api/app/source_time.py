"""Source timestamps as ISO-8601 strings."""

from datetime import UTC, datetime
from typing import Any


def iso(value: Any) -> str | None:
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        # WeChat/SQLite timestamps are Unix seconds.
        try:
            return datetime.fromtimestamp(value, UTC).isoformat()
        except (OverflowError, OSError, ValueError):
            return None
    value = str(value).strip()
    if not value:
        return None
    # Chatlog exports timestamps as strings even though they are Unix
    # seconds. Treat them exactly like the integer form instead of silently
    # dropping every Moments date.
    if value.isdigit() or (value.startswith("-") and value[1:].isdigit()):
        try:
            return datetime.fromtimestamp(int(value), UTC).isoformat()
        except (OverflowError, OSError, ValueError):
            return None
    try:
        return datetime.fromisoformat(value).isoformat()
    except ValueError:
        return None


def message_key(source_record_id: str, occurred_at: datetime) -> str:
    """Stable source identity independent of PostgreSQL session timezone."""
    if occurred_at.tzinfo is None:
        raise ValueError("Message timestamps must be timezone aware")
    return source_record_id + ":" + occurred_at.astimezone(UTC).isoformat()


def message_identity(value: str):
    """Parse only explicit source-ID + aware ISO time keys; never infer identity."""
    import re
    match = re.fullmatch(r"(.+):(\d{4}-\d{2}-\d{2}T.+)", value)
    if not match:
        return None
    try:
        timestamp = datetime.fromisoformat(match[2])
    except ValueError:
        return None
    if timestamp.tzinfo is None:
        return None
    return match[1], timestamp.astimezone(UTC)
