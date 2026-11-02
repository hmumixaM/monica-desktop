from typing import Any

from importers.common import make_batch, normalize_records


def posts_batch(
    batch_id: str,
    posts: list[dict[str, Any]],
    cursor_before: str | None = None,
    cursor_after: str | None = None,
) -> dict[str, Any]:
    return make_batch(
        "instagram",
        "posts",
        batch_id,
        cursor_before,
        cursor_after,
        normalize_records(posts, id_keys=("external_id", "id", "shortcode"), entity_type="post"),
    )


def stories_batch(
    batch_id: str,
    stories: list[dict[str, Any]],
    cursor_before: str | None = None,
    cursor_after: str | None = None,
) -> dict[str, Any]:
    return make_batch(
        "instagram",
        "stories",
        batch_id,
        cursor_before,
        cursor_after,
        normalize_records(stories, id_keys=("external_id", "id", "story_id"), entity_type="story"),
    )
