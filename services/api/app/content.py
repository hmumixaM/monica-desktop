"""Content-addressed storage primitives shared by import and edit flows."""

import hashlib
import json
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session


def _json_safe(value: Any) -> Any:
    """Remove characters PostgreSQL jsonb cannot represent from imported text.

    Source exports occasionally contain NUL bytes in otherwise valid text. JSON
    permits escaping them, but PostgreSQL text/jsonb rejects the resulting
    character, so normalize it once at the content-addressed boundary.
    """
    if isinstance(value, str):
        return value.replace("\x00", "")
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    if isinstance(value, tuple):
        return [_json_safe(item) for item in value]
    if isinstance(value, dict):
        return {_json_safe(key): _json_safe(item) for key, item in value.items()}
    return value


def canonical_json(value: Any) -> bytes:
    return json.dumps(
        _json_safe(value), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def content_hash(value: Any) -> str:
    if isinstance(value, bytes):
        payload = value
    elif isinstance(value, str):
        payload = value.encode("utf-8")
    else:
        payload = canonical_json(value)
    return hashlib.sha256(payload).hexdigest()


def get_or_create_content(
    db: Session,
    *,
    value: Any,
    content_kind: str,
    storage_uri: str | None = None,
    store_payload: bool = True,
) -> tuple[str, str]:
    """Return `(content_object_id, sha256)` without duplicating payloads."""
    safe_value = _json_safe(value)
    payload = (
        safe_value
        if isinstance(value, bytes)
        else (
            safe_value.encode("utf-8")
            if isinstance(safe_value, str)
            else canonical_json(safe_value)
        )
    )
    digest = hashlib.sha256(payload).hexdigest()
    existing = (
        db.execute(
            text("""
            SELECT id, payload_json, storage_uri FROM content_objects
            WHERE content_kind = :kind AND sha256 = :sha256
        """),
            {"kind": content_kind, "sha256": digest},
        )
        .mappings()
        .one_or_none()
    )
    # Legacy hashes treated a string such as "0" exactly like JSON number 0.
    # Keep every old object immutable and reuse it only for the same typed
    # value. A distinct JSON value gets a separate, domain-prefixed address.
    if (existing and store_payload and not isinstance(value, bytes)
            and not (existing['payload_json'] is None and existing['storage_uri'])
            and canonical_json(existing['payload_json']) != canonical_json(safe_value)):
        digest=hashlib.sha256(b'json\x00'+canonical_json(safe_value)).hexdigest()
        existing=db.execute(text('SELECT id,payload_json,storage_uri FROM content_objects WHERE content_kind=:kind AND sha256=:sha256'),
            {'kind':content_kind,'sha256':digest}).mappings().one_or_none()
        if existing and canonical_json(existing['payload_json']) != canonical_json(safe_value):
            raise ValueError('Content address has conflicting JSON content')
    if existing:
        if store_payload and existing["payload_json"] is None and not isinstance(value, bytes):
            db.execute(
                text("""
                  UPDATE content_objects SET payload_json = CAST(:payload AS jsonb)
                  WHERE id = :id AND (payload_json IS NULL OR payload_json = 'null'::jsonb)
                """),
                {"id": existing["id"], "payload": json.dumps(safe_value, ensure_ascii=False)},
            )
        return str(existing["id"]), digest
    payload_json = safe_value if store_payload and not isinstance(value, bytes) else None
    row = db.execute(
        text("""
            INSERT INTO content_objects
              (sha256, content_kind, byte_length, storage_uri, payload_json)
            VALUES (:sha256, :kind, :byte_length, :storage_uri, CAST(:payload AS jsonb))
            ON CONFLICT(content_kind,sha256) DO NOTHING
            RETURNING id
        """),
        {
            "sha256": digest,
            "kind": content_kind,
            "byte_length": len(payload),
            "storage_uri": storage_uri,
            "payload": json.dumps(payload_json, ensure_ascii=False)
            if payload_json is not None
            else "null",
        },
    ).scalar_one_or_none()
    if row is None:
        # Another writer may have inserted the legacy address after our read.
        # Recheck its typed value before returning its immutable object ID.
        return get_or_create_content(db,value=value,content_kind=content_kind,
                                     storage_uri=storage_uri,store_payload=store_payload)
    return str(row), digest


def media_object_path(media_root: Path, digest: str) -> Path:
    return media_root / "sha256" / digest[:2] / digest[2:4] / digest
