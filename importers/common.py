import hashlib
import json
from datetime import UTC, datetime
from typing import Any


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def record_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode()).hexdigest()


def normalize_records(
    records: list[dict[str, Any]],
    *,
    id_keys: tuple[str, ...] = ("external_id", "id", "uuid", "key"),
    entity_type: str | None = None,
) -> list[dict[str, Any]]:
    """Wrap source-native dictionaries in the Import API record contract.

    Existing collectors often emit their complete provider object directly,
    while the API requires a stable external ID plus a ``content`` member.
    The full source dictionary remains the content payload; normalized routing
    fields already present on the record are copied alongside it.
    """
    normalized: list[dict[str, Any]] = []
    for source_record in records:
        item = dict(source_record)
        content = item.get("content", item)
        external_id = next((item.get(key) for key in id_keys if item.get(key)), None)
        if external_id is None:
            external_id = record_hash(content)
        record: dict[str, Any] = {"external_id": str(external_id), "content": content}
        if entity_type and "entity_type" not in item:
            record["entity_type"] = entity_type
        for key in (
            "profile_id",
            "sender_profile_id",
            "entity_type",
            "provider",
            "conversation_external_id",
            "conversation_type",
            "message_type",
            "occurred_at",
            "source_updated_at",
            "media",
        ):
            if key in item:
                record[key] = item[key]
        normalized.append(record)
    return normalized


def make_batch(
    source: str,
    stream: str,
    batch_id: str,
    cursor_before: str | None,
    cursor_after: str | None,
    records: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "source": source,
        "stream": stream,
        "schema_version": 1,
        "batch_id": batch_id,
        "idempotency_key": f"{source}:{stream}:{batch_id}",
        "cursor_before": cursor_before,
        "cursor_after": cursor_after,
        "observed_at": datetime.now(UTC).isoformat(),
        "records": records,
        "raw_objects": [],
        "media_manifest": [],
    }
