"""Displayable post text: provider placeholders are not content."""

import re

PLACEHOLDER = re.compile(
    r"^(partial|complete|unknown|unavailable|expired|missing"
    r"|(instagram|wechat|linkedin)?\s*(story|stories|post|动态|快拍)"
    r"|instagram 动态|linkedin 动态|朋友圈)$",
    re.IGNORECASE,
)


def display_text(value) -> str | None:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    if not stripped or PLACEHOLDER.fullmatch(stripped):
        return None
    return stripped


def excerpt(value, length: int) -> str | None:
    text = display_text(value)
    if text is None:
        return None
    collapsed = re.sub(r"\s+", " ", text)
    return collapsed if len(collapsed) <= length else collapsed[: length - 1].rstrip() + "…"


def media_ready(item: dict) -> bool:
    """Local bytes exist once a media asset row is linked; remote-only items have no id."""
    return bool(item.get("id"))
