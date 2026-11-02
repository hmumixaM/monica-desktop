from __future__ import annotations

import fcntl
import json
import os
import threading
import time
import uuid
from collections.abc import Callable
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlsplit, urlunsplit


SENSITIVE_KEY_MARKERS = (
    "authorization",
    "cookie",
    "csrf",
    "password",
    "secret",
    "session",
    "token",
)
MAX_COLLECTION_ITEMS = 500
MAX_STRING_LENGTH = 1_000
MAX_SUMMARY_NODES = 10_000


def _redacted_key(key: object) -> bool:
    normalized = str(key).lower()
    return any(marker in normalized for marker in SENSITIVE_KEY_MARKERS)


def sanitize_value(value: Any, depth: int = 0) -> Any:
    if depth >= 8:
        return "<max-depth>"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        if value.startswith(("{", "[")):
            try:
                return sanitize_value(json.loads(value), depth + 1)
            except json.JSONDecodeError:
                pass
        return (
            value
            if len(value) <= MAX_STRING_LENGTH
            else value[:MAX_STRING_LENGTH] + "<truncated>"
        )
    if isinstance(value, dict):
        return {
            str(key): (
                "<redacted>"
                if _redacted_key(key)
                else sanitize_value(child, depth + 1)
            )
            for key, child in list(value.items())[:MAX_COLLECTION_ITEMS]
        }
    if isinstance(value, (list, tuple)):
        return [
            sanitize_value(child, depth + 1)
            for child in value[:MAX_COLLECTION_ITEMS]
        ]
    return sanitize_value(str(value), depth + 1)


def sanitize_url(value: object) -> str:
    parsed = urlsplit(str(value))
    query_keys = sorted({key for key, _ in parse_qsl(parsed.query)})
    safe_query = "&".join(f"{key}=<redacted>" for key in query_keys)
    sanitized = urlunsplit(
        (parsed.scheme, parsed.netloc, parsed.path, safe_query, "")
    )
    return sanitize_value(sanitized)


def _story_reels(result: Any) -> list[dict[str, Any]]:
    if not isinstance(result, dict):
        return []
    data = result.get("data")
    if not isinstance(data, dict):
        return []
    reels = data.get("reels_media")
    if not isinstance(reels, list):
        return []

    summary = []
    for reel in reels[:MAX_COLLECTION_ITEMS]:
        if not isinstance(reel, dict):
            continue
        owner = reel.get("owner")
        if not isinstance(owner, dict):
            owner = reel.get("user")
        if not isinstance(owner, dict):
            owner = {}
        items = reel.get("items")
        if not isinstance(items, list):
            items = []
        summary.append(
            {
                "owner_id": owner.get("id") or reel.get("id"),
                "owner_username": owner.get("username"),
                "items": [
                    {
                        "media_id": item.get("id"),
                        "shortcode": item.get("shortcode"),
                        "taken_at": item.get("taken_at_timestamp")
                        or item.get("taken_at"),
                        "expiring_at": item.get("expiring_at_timestamp"),
                        "is_video": item.get("is_video"),
                    }
                    for item in items[:MAX_COLLECTION_ITEMS]
                    if isinstance(item, dict)
                ],
            }
        )
    return summary


def summarize_result(result: Any) -> dict[str, Any]:
    identifiers: dict[str, list[Any]] = {
        "id": [],
        "pk": [],
        "shortcode": [],
        "username": [],
        "user_id": [],
        "owner_id": [],
    }
    seen = {key: set() for key in identifiers}
    counts = {"dicts": 0, "lists": 0}
    visited = 0

    def visit(value: Any, depth: int = 0) -> None:
        nonlocal visited
        if depth >= 12 or visited >= MAX_SUMMARY_NODES:
            return
        visited += 1
        if isinstance(value, dict):
            counts["dicts"] += 1
            for key in identifiers:
                candidate = value.get(key)
                if (
                    isinstance(candidate, (str, int))
                    and candidate not in seen[key]
                    and len(identifiers[key]) < MAX_COLLECTION_ITEMS
                ):
                    seen[key].add(candidate)
                    identifiers[key].append(candidate)
            for child in value.values():
                visit(child, depth + 1)
        elif isinstance(value, list):
            counts["lists"] += 1
            for child in value[:MAX_COLLECTION_ITEMS]:
                visit(child, depth + 1)

    visit(result)
    summary: dict[str, Any] = {
        "result_type": type(result).__name__,
        "structure_counts": counts,
        "summary_truncated": visited >= MAX_SUMMARY_NODES,
        "identifiers": {
            key: values for key, values in identifiers.items() if values
        },
    }
    if isinstance(result, dict):
        summary["top_level_keys"] = sorted(str(key) for key in result)
    reels = _story_reels(result)
    if reels:
        summary["story_reels"] = reels
    status_code = getattr(result, "status_code", None)
    if status_code is not None:
        summary["http_status"] = status_code
    return summary


class QueryAuditor:
    def __init__(self, log_dir: Path, phase: str) -> None:
        self.log_dir = log_dir
        self.phase = phase
        self._local = threading.local()

    def _write(self, record: dict[str, Any]) -> None:
        self.log_dir.mkdir(parents=True, exist_ok=True)
        date = datetime.now(timezone.utc).date().isoformat()
        path = self.log_dir / f"instagram-query-audit-{date}.jsonl"
        payload = (
            json.dumps(record, ensure_ascii=False, separators=(",", ":"))
            + "\n"
        ).encode()
        descriptor = os.open(
            path,
            os.O_WRONLY | os.O_CREAT | os.O_APPEND,
            0o600,
        )
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            os.write(descriptor, payload)
        finally:
            os.close(descriptor)

    def call(
        self,
        query_type: str,
        query: dict[str, Any],
        function: Callable[[], Any],
    ) -> Any:
        if getattr(self._local, "active", False):
            return function()

        self._local.active = True
        request_id = uuid.uuid4().hex
        started_ns = time.time_ns()
        started_at = datetime.now(timezone.utc).isoformat()
        try:
            result = function()
        except BaseException as error:
            completed_ns = time.time_ns()
            self._write(
                {
                    "schema_version": 1,
                    "request_id": request_id,
                    "phase": self.phase,
                    "query_type": query_type,
                    "query": query,
                    "started_at": started_at,
                    "completed_at": datetime.now(timezone.utc).isoformat(),
                    "duration_ms": round(
                        (completed_ns - started_ns) / 1_000_000,
                        3,
                    ),
                    "status": "error",
                    "error": {
                        "type": type(error).__name__,
                    },
                }
            )
            raise
        else:
            completed_ns = time.time_ns()
            self._write(
                {
                    "schema_version": 1,
                    "request_id": request_id,
                    "phase": self.phase,
                    "query_type": query_type,
                    "query": query,
                    "started_at": started_at,
                    "completed_at": datetime.now(timezone.utc).isoformat(),
                    "duration_ms": round(
                        (completed_ns - started_ns) / 1_000_000,
                        3,
                    ),
                    "status": "success",
                    "result": summarize_result(result),
                }
            )
            return result
        finally:
            self._local.active = False


def _argument(
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
    position: int,
    name: str,
    default: Any = None,
) -> Any:
    return args[position] if len(args) > position else kwargs.get(name, default)


def install_query_audit(
    loader: Any,
    phase: str,
    log_dir: Path,
) -> QueryAuditor:
    auditor = QueryAuditor(log_dir, phase)
    context = loader.context

    def install(
        method_name: str,
        query_type: str,
        describe: Callable[[tuple[Any, ...], dict[str, Any]], dict[str, Any]],
    ) -> None:
        original = getattr(context, method_name)

        @wraps(original)
        def audited(*args: Any, **kwargs: Any) -> Any:
            query = describe(args, kwargs)
            return auditor.call(
                query_type,
                query,
                lambda: original(*args, **kwargs),
            )

        setattr(context, method_name, audited)

    install(
        "graphql_query",
        "graphql",
        lambda args, kwargs: {
            "query_hash": sanitize_value(
                _argument(args, kwargs, 0, "query_hash")
            ),
            "variables": sanitize_value(
                _argument(args, kwargs, 1, "variables", {})
            ),
        },
    )
    install(
        "doc_id_graphql_query",
        "doc_id_graphql",
        lambda args, kwargs: {
            "doc_id": sanitize_value(_argument(args, kwargs, 0, "doc_id")),
            "variables": sanitize_value(
                _argument(args, kwargs, 1, "variables", {})
            ),
        },
    )
    install(
        "get_json",
        "json",
        lambda args, kwargs: {
            "path": sanitize_url(_argument(args, kwargs, 0, "path")),
            "params": sanitize_value(
                _argument(args, kwargs, 1, "params", {})
            ),
            "host": sanitize_value(kwargs.get("host")),
        },
    )
    install(
        "get_iphone_json",
        "iphone_json",
        lambda args, kwargs: {
            "path": sanitize_url(_argument(args, kwargs, 0, "path")),
            "params": sanitize_value(
                _argument(args, kwargs, 1, "params", {})
            ),
        },
    )
    install(
        "get_raw",
        "media",
        lambda args, kwargs: {
            "url": sanitize_url(_argument(args, kwargs, 0, "url")),
        },
    )
    install(
        "head",
        "head",
        lambda args, kwargs: {
            "url": sanitize_url(_argument(args, kwargs, 0, "url")),
        },
    )
    return auditor
