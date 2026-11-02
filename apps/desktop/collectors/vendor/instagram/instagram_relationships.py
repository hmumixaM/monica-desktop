from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


COMMENT_COUNT_LIMIT = int(os.environ.get("RELATIONSHIP_COMMENT_LIMIT", "50"))
LIKE_COUNT_LIMIT = int(os.environ.get("RELATIONSHIP_LIKE_LIMIT", "50"))
MENTION_PATTERN = re.compile(r"(?<![A-Za-z0-9._])@([A-Za-z0-9._]+)")


def extract_mentions(text: str | None) -> list[str]:
    """Return unique, normalized Instagram usernames in textual order."""

    if not text:
        return []
    mentions = []
    for match in MENTION_PATTERN.finditer(text):
        username = match.group(1).lower().strip(".")
        if username and username not in mentions:
            mentions.append(username)
    return mentions


def profile_reference(profile: Any) -> dict[str, int | str]:
    return {
        "userid": int(profile.userid),
        "username": profile.username.lower(),
    }


def post_is_eligible_for_collection(raw_post_path: Path, started_at: int) -> bool:
    """Exclude pre-rollout posts while allowing retries for new posts."""

    if not raw_post_path.exists():
        return True
    with raw_post_path.open(encoding="utf-8") as handle:
        previous_raw = json.load(handle)
    return int(previous_raw["captured_at"]) >= started_at


def profile_relationship_document(
    profile: Any,
    *,
    captured_at: int | None = None,
) -> dict[str, Any]:
    """Normalize Instagram's built-in mutual-friend preview without more queries."""

    mutual = profile._node.get("edge_mutual_followed_by", {})
    preview = []
    for edge in mutual.get("edges", []):
        node = edge["node"]
        reference: dict[str, int | str] = {
            "username": node["username"].lower(),
        }
        if "id" in node:
            reference["userid"] = int(node["id"])
        preview.append(reference)
    preview.sort(key=lambda row: row["username"])
    return {
        "schema_version": 1,
        "captured_at": captured_at or int(time.time()),
        "profile": profile_reference(profile),
        "mutual_followed_by": {
            "count": int(mutual.get("count", 0)),
            "preview": preview,
            "complete": len(preview) == int(mutual.get("count", 0)),
        },
        "collection_policy": {
            "source": "profile_metadata_preview",
            "followers_requested": False,
            "followees_requested": False,
        },
    }


def _comment_record_from_node(node: dict[str, Any]) -> dict[str, Any]:
    owner = node.get("owner") or node["user"]
    created_at = node.get("created_at_utc", node.get("created_at"))
    if isinstance(created_at, (int, float)):
        created_at = datetime.fromtimestamp(created_at, timezone.utc).isoformat()
    likes_count = node.get("comment_like_count")
    if likes_count is None:
        likes_count = node.get("edge_liked_by", {}).get("count", 0)
    return {
        "id": int(node.get("id") or node["pk"]),
        "created_at": str(created_at),
        "owner": _profile_reference_from_node(owner),
        "likes_count": int(likes_count),
        "mentions": extract_mentions(node.get("text")),
    }


def _comment_preview(post: Any, comment_count: int) -> tuple[list[dict], str]:
    nodes = []
    for key in ("edge_media_to_parent_comment", "edge_media_to_comment"):
        preview = post._node.get(key, {})
        if "edges" in preview:
            nodes = [edge["node"] for edge in preview["edges"]]
            break
    if not nodes:
        iphone_node = post._node.get("iphone_struct", {})
        nodes = iphone_node.get("comments") or iphone_node.get(
            "preview_comments",
            [],
        )
    comments = []
    represented = 0
    for node in nodes:
        record = _comment_record_from_node(node)
        threaded = node.get("edge_threaded_comments", {})
        answer_nodes = [
            edge["node"] for edge in threaded.get("edges", [])
        ] or node.get("preview_child_comments", [])
        record["answers"] = [
            _comment_record_from_node(answer) for answer in answer_nodes
        ]
        represented += 1 + len(record["answers"])
        comments.append(record)
    if not comments:
        return [], "unavailable_payload"
    policy = "collected" if represented == comment_count else "partial_preview"
    return comments, policy


def _profile_reference_from_node(node: dict[str, Any]) -> dict[str, int | str]:
    userid = node.get("id", node.get("pk"))
    reference: dict[str, int | str] = {
        "username": node["username"].lower(),
    }
    if userid is not None:
        reference["userid"] = int(userid)
    return reference


def _like_preview(post: Any, like_count: int) -> tuple[list[dict], str]:
    iphone_node = post._node.get("iphone_struct", {})
    nodes = iphone_node.get("likers") or iphone_node.get(
        "facepile_top_likers",
        [],
    )
    if not nodes:
        web_preview = post._node.get("edge_media_preview_like", {})
        nodes = [edge["node"] for edge in web_preview.get("edges", [])]
    profiles = sorted(
        (_profile_reference_from_node(node) for node in nodes),
        key=lambda row: row["username"],
    )
    if not profiles:
        return [], "unavailable_payload"
    policy = "collected" if len(profiles) == like_count else "partial_preview"
    return profiles, policy


def post_relationship_document(
    post: Any,
    *,
    captured_at: int | None = None,
    comment_limit: int = COMMENT_COUNT_LIMIT,
    like_limit: int = LIKE_COUNT_LIMIT,
) -> dict[str, Any]:
    """Collect bounded interaction identities for one newly archived post."""

    comment_count = int(post.comments)
    like_count = int(post.likes)
    comments = []
    comment_policy = "empty" if comment_count == 0 else "skipped_threshold"
    if 0 < comment_count < comment_limit:
        comments, comment_policy = _comment_preview(post, comment_count)

    likes = []
    like_policy = "empty" if like_count == 0 else "skipped_threshold"
    if 0 < like_count <= like_limit:
        likes, like_policy = _like_preview(post, like_count)

    return {
        "schema_version": 1,
        "captured_at": captured_at or int(time.time()),
        "shortcode": post.shortcode,
        "owner_username": post.owner_username.lower(),
        "caption_mentions": extract_mentions(post.caption),
        "tagged_users": sorted({username.lower() for username in post.tagged_users}),
        "comments": {
            "count": comment_count,
            "policy": comment_policy,
            "records": comments,
        },
        "likes": {
            "count": like_count,
            "policy": like_policy,
            "profiles": likes,
        },
        "collection_policy": {
            "comments_less_than": comment_limit,
            "likes_at_most": like_limit,
        },
    }


def write_document(document: dict[str, Any], path: Path) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(document, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    temporary.replace(path)
