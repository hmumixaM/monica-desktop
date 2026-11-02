from typing import Any

from importers.common import make_batch, normalize_records


def profile_batch(
    batch_id: str,
    profiles: list[dict[str, Any]],
    cursor_before: str | None = None,
    cursor_after: str | None = None,
) -> dict[str, Any]:
    return make_batch(
        "linkedin",
        "profiles",
        batch_id,
        cursor_before,
        cursor_after,
        normalize_records(
            profiles,
            id_keys=("external_id", "id", "public_identifier", "username"),
        ),
    )


def posts_batch(
    batch_id: str,
    posts: list[dict[str, Any]],
    cursor_before: str | None = None,
    cursor_after: str | None = None,
) -> dict[str, Any]:
    return make_batch(
        "linkedin",
        "posts",
        batch_id,
        cursor_before,
        cursor_after,
        normalize_records(posts, id_keys=("external_id", "id", "urn"), entity_type="post"),
    )
