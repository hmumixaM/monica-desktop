from typing import Any

from importers.common import make_batch, normalize_records


def contacts_batch(
    batch_id: str,
    contacts: list[dict[str, Any]],
    cursor_before: str | None = None,
    cursor_after: str | None = None,
) -> dict[str, Any]:
    return make_batch(
        "wechat",
        "contacts",
        batch_id,
        cursor_before,
        cursor_after,
        normalize_records(contacts, id_keys=("external_id", "u", "wxid", "id")),
    )


def profile_field_record(
    external_id: str, profile_id: str, field_key: str, value: Any
) -> dict[str, Any]:
    return {
        "external_id": external_id,
        "profile_id": profile_id,
        "content": {"field_key": field_key, "value": value},
    }
