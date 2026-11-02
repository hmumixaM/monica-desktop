"""Explicit source identifiers; display names and statistics are never identities."""

import re
from urllib.parse import urlsplit


def monica_identities(fields):
    result = []
    keys = {
        "微信内部 ID": ("wechat", "id"), "微信内部ID": ("wechat", "id"),
        "微信群 ID": ("wechat_group", "id"), "微信群ID": ("wechat_group", "id"),
        "微信号": ("wechat", "alias"), "WeChat": ("wechat", "alias"),
        "Instagram": ("instagram", "url"), "LinkedIn": ("linkedin", "url"),
    }
    for label, values in fields.items():
        rule = keys.get(label)
        if not rule:
            continue
        provider, kind = rule
        for value in values if isinstance(values, list) else [values]:
            if not isinstance(value, str):
                continue
            value = value.strip()
            if kind in {"id", "alias"}:
                if not re.fullmatch(r"[A-Za-z0-9_@.\-]+", value):
                    continue
                if provider == "wechat_group" and not value.endswith("@chatroom"):
                    continue
                if provider == "wechat" and value.endswith("@chatroom"):
                    continue
                result.append((provider, value, kind))
            else:
                parsed = urlsplit(value if "://" in value else "https://" + value)
                host = (parsed.hostname or "").lower().removeprefix("www.")
                parts = parsed.path.strip("/").split("/")
                if provider == "instagram" and host == "instagram.com" and len(parts) == 1 and parts[0]:
                    result.append((provider, parts[0].lower(), kind))
                if provider == "linkedin" and host == "linkedin.com" and len(parts) == 2 and parts[0] == "in":
                    result.append((provider, parts[1].lower(), kind))
    return result
