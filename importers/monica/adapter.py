from typing import Any

from importers.common import make_batch, normalize_records


def export_batch(
    batch_id: str,
    records: list[dict[str, Any]],
    cursor_before: str | None = None,
    cursor_after: str | None = None,
) -> dict[str, Any]:
    return make_batch(
        "monica",
        "legacy",
        batch_id,
        cursor_before,
        cursor_after,
        normalize_records(records, id_keys=("external_id", "id", "uuid", "contact_id")),
    )
