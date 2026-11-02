#!/usr/bin/env python3

from __future__ import annotations

import argparse
import fcntl
import json
import os
import random
import shutil
import time
from collections.abc import Iterator
from datetime import timezone
from pathlib import Path
from typing import Any

import instaloader

from conservative import ConservativeRateController
from story_schedule import wait_for_story_run
from public_accounts import write_public_accounts
from query_audit import install_query_audit


SOURCE_DIR = Path(__file__).resolve().parent
BASE_DIR = Path(os.environ["MONICA_INSTAGRAM_ROOT"])
STORIES_DIR = BASE_DIR / "data" / "stories"
STATE_DIR = BASE_DIR / "state"
LOG_DIR = BASE_DIR / "logs"
EFFECT_FIELD_MARKERS = (
    "attribution",
    "effect",
    "filter",
    "interactive",
    "music",
    "sticker",
)
MEDIA_SUFFIXES = {".jpg", ".jpeg", ".mp4", ".webp"}
STORY_BATCH_SIZE = 30


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Archive every currently visible story from followed profiles."
    )
    parser.add_argument("--login", required=True)
    return parser.parse_args()


def env_float(name: str, default: float) -> float:
    return float(os.environ.get(name, default))


def story_item_limit_reached(downloaded: int, maximum: int) -> bool:
    return downloaded >= maximum


def load_followee_ids(path: Path, self_username: str) -> list[int]:
    userids: list[int] = []
    seen: set[int] = set()
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            row = json.loads(line)
            if row["username"] == self_username:
                continue
            userid = int(row["userid"])
            if userid in seen:
                continue
            seen.add(userid)
            userids.append(userid)
    return userids


def load_story_queue(
    path: Path,
    followee_ids: list[int],
    last_run_path: Path,
    batch_size: int = STORY_BATCH_SIZE,
) -> list[int]:
    """Keep unfinished accounts first and reconcile queue membership."""
    ordered = list(followee_ids)
    if path.exists():
        document = json.loads(path.read_text(encoding="utf-8"))
        if document.get("version") != 1 or not isinstance(
            document.get("userids"), list
        ):
            raise ValueError("Invalid Story account queue")
        ordered = [int(userid) for userid in document["userids"]]
    elif last_run_path.exists():
        previous = json.loads(last_run_path.read_text(encoding="utf-8"))
        scanned = min(int(previous.get("followees_scanned") or 0), len(ordered))
        if scanned and (
            previous.get("stopped_item_limit")
            or previous.get("stopped_low_disk")
        ):
            retry_start = max(0, scanned - batch_size)
            ordered = (
                ordered[retry_start:scanned]
                + ordered[scanned:]
                + ordered[:retry_start]
            )

    current = set(followee_ids)
    reconciled: list[int] = []
    seen: set[int] = set()
    for userid in [*ordered, *followee_ids]:
        if userid in current and userid not in seen:
            reconciled.append(userid)
            seen.add(userid)
    return reconciled


def reorder_story_queue(
    userids: list[int],
    queried_userids: list[int],
    retry_userids: set[int],
) -> list[int]:
    """Put unfinished queries first and completed queries at the back."""
    queried = set(queried_userids)
    if not retry_userids <= queried:
        raise ValueError("Story retry accounts must have been queried")
    retry = [userid for userid in userids if userid in retry_userids]
    unqueried = [userid for userid in userids if userid not in queried]
    completed = [
        userid
        for userid in userids
        if userid in queried and userid not in retry_userids
    ]
    return retry + unqueried + completed


def write_story_queue(path: Path, userids: list[int]) -> None:
    write_json(
        path,
        {"version": 1, "updated_at": int(time.time()), "userids": userids},
    )


def requires_cooldown(error: BaseException) -> bool:
    normalized = f"{type(error).__name__}: {error}".lower()
    return any(
        marker in normalized
        for marker in (
            "403 forbidden",
            "429",
            "feedback_required",
            "loginrequiredexception",
            "too many requests",
        )
    )


def effect_metadata(value: Any, path: str = "") -> dict[str, Any]:
    found: dict[str, Any] = {}
    if isinstance(value, dict):
        for key, child in value.items():
            child_path = f"{path}.{key}" if path else key
            if any(marker in key.lower() for marker in EFFECT_FIELD_MARKERS):
                found[child_path] = child
            else:
                found.update(effect_metadata(child, child_path))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            found.update(effect_metadata(child, f"{path}[{index}]"))
    return found


def manifest_complete(path: Path) -> bool:
    if not path.exists():
        return False
    manifest = json.loads(path.read_text(encoding="utf-8"))
    files = manifest["files"]
    return bool(files) and all((path.parent / name).is_file() for name in files)


def has_downloaded_media(files: list[str]) -> bool:
    return any(Path(name).suffix.lower() in MEDIA_SUFFIXES for name in files)


def write_json(path: Path, document: dict[str, Any]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(document, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )
    temporary.replace(path)


def append_journal(path: Path, document: dict[str, Any]) -> None:
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(document, ensure_ascii=False, separators=(",", ":")))
        handle.write("\n")


def story_items(
    loader: instaloader.Instaloader,
    story: instaloader.Story,
    cooldown_file: Path,
) -> tuple[list[instaloader.StoryItem], bool]:
    try:
        return list(story.get_items()), True
    except instaloader.InstaloaderException as error:
        if requires_cooldown(error):
            cooldown_file.write_text(
                f"{int(time.time())}\tstories-mobile-metadata\n",
                encoding="utf-8",
            )
            raise
        loader.context.error(
            "Optional mobile Story metadata failed; preserving GraphQL "
            f"content without it: {type(error).__name__}: {error}"
        )
        loader.context.iphone_support = False
        return list(story.get_items()), False


def story_batches(
    loader: instaloader.Instaloader,
    userids: list[int],
    batch_size: int = STORY_BATCH_SIZE,
) -> Iterator[tuple[list[int], Iterator[instaloader.Story]]]:
    for offset in range(0, len(userids), batch_size):
        batch = userids[offset : offset + batch_size]
        yield batch, loader.get_stories(batch)


def archive_visible_stories(
    loader: instaloader.Instaloader,
    userids: list[int],
    cooldown_file: Path,
    item_delay_min: float,
    item_delay_max: float,
    minimum_free_gib: float,
    max_new_items: int,
    queue_file: Path,
) -> dict[str, Any]:
    scan_started_at = int(time.time())
    journal = LOG_DIR / "archive-stories.jsonl"
    owners: set[int] = set()
    visible = 0
    downloaded = 0
    existing = 0
    followees_scanned = 0
    stopped_low_disk = False
    stopped_item_limit = False
    queried_userids: list[int] = []
    retry_userids: set[int] = set()

    for userid_batch, stories in story_batches(loader, userids):
        batch_stories = list(stories)
        queried_userids.extend(userid_batch)
        followees_scanned += len(userid_batch)
        for story_index, story in enumerate(batch_stories):
            owners.add(story.owner_id)
            items, _ = story_items(loader, story, cooldown_file)
            for item_index, item in enumerate(items):
                visible += 1
                owner = item.owner_username
                owner_dir = STORIES_DIR / owner
                manifest_path = owner_dir / f"story-{item.mediaid}.manifest.json"
                if manifest_complete(manifest_path):
                    existing += 1
                    continue

                if story_item_limit_reached(downloaded, max_new_items):
                    stopped_item_limit = True
                    retry_userids.update(
                        int(pending.owner_id)
                        for pending in batch_stories[story_index:]
                    )
                    break
                free_gib = shutil.disk_usage(STORIES_DIR).free / (1024**3)
                if free_gib < minimum_free_gib:
                    stopped_low_disk = True
                    retry_userids.update(
                        int(pending.owner_id)
                        for pending in batch_stories[story_index:]
                    )
                    break
                time.sleep(random.uniform(item_delay_min, item_delay_max))
                loader.download_storyitem(item, owner)
                owner_dir.mkdir(parents=True, exist_ok=True)
                files = sorted(
                    path.name
                    for path in owner_dir.iterdir()
                    if path.is_file()
                    and str(item.mediaid) in path.name
                    and path != manifest_path
                )
                if not has_downloaded_media(files):
                    raise RuntimeError(
                        f"Story {item.mediaid} has no downloaded media"
                    )

                raw_metadata = item._asdict()
                manifest = {
                    "schema_version": 1,
                    "captured_at": int(time.time()),
                    "mediaid": item.mediaid,
                    "shortcode": item.shortcode,
                    "owner_id": item.owner_id,
                    "owner_username": owner,
                    "taken_at": item.date_utc.replace(
                        tzinfo=timezone.utc
                    ).isoformat(),
                    "expiring_at": item.expiring_utc.replace(
                        tzinfo=timezone.utc
                    ).isoformat(),
                    "typename": item.typename,
                    "is_video": item.is_video,
                    "caption": item.caption,
                    "files": files,
                    "mobile_metadata_status": (
                        "complete"
                        if "iphone_struct" in raw_metadata
                        else "pending"
                    ),
                    "effect_metadata": effect_metadata(raw_metadata),
                    "raw_metadata": raw_metadata,
                }
                write_json(manifest_path, manifest)
                append_journal(
                    journal,
                    {
                        "archived_at": manifest["captured_at"],
                        "mediaid": item.mediaid,
                        "owner_id": item.owner_id,
                        "owner_username": owner,
                        "is_video": item.is_video,
                        "files": files,
                        "effects": len(manifest["effect_metadata"]),
                    },
                )
                downloaded += 1
                if story_item_limit_reached(downloaded, max_new_items):
                    stopped_item_limit = True
                    retry_from = (
                        story_index
                        if item_index + 1 < len(items)
                        else story_index + 1
                    )
                    retry_userids.update(
                        int(pending.owner_id)
                        for pending in batch_stories[retry_from:]
                    )
                    break
            if stopped_low_disk or stopped_item_limit:
                break
        if stopped_low_disk or stopped_item_limit:
            break

    next_queue = reorder_story_queue(
        userids,
        queried_userids,
        retry_userids,
    )
    write_story_queue(queue_file, next_queue)
    summary: dict[str, Any] = {
        "scan_started_at": scan_started_at,
        "scan_completed_at": int(time.time()),
        "scan_mode": "full-id",
        "followees_configured": len(userids),
        "followees_scanned": followees_scanned,
        "owners_with_stories": len(owners),
        "visible_items": visible,
        "downloaded_items": downloaded,
        "existing_items": existing,
        "mobile_metadata_requested": False,
        "stopped_low_disk": stopped_low_disk,
        "stopped_item_limit": stopped_item_limit,
        "retry_accounts_next_run": len(retry_userids),
        "max_new_items_per_run": max_new_items,
        "minimum_free_gib": minimum_free_gib,
        "free_gib": round(shutil.disk_usage(STORIES_DIR).free / (1024**3), 2),
    }
    write_json(STATE_DIR / "story-last-run.json", summary)
    return summary


def main() -> None:
    args = parse_arguments()
    os.umask(0o077)
    STORIES_DIR.mkdir(parents=True, exist_ok=True)
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    lock_path = STATE_DIR / "stories.lock"
    with lock_path.open("w", encoding="utf-8") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("Another Story archive is already running.", flush=True)
            return

        cooldown_file = STATE_DIR / "COOLDOWN_REQUIRED"
        if cooldown_file.exists():
            print("Cooldown marker exists; Story archive will not access Instagram.")
            return

        if not wait_for_story_run(STATE_DIR, env_float("STORY_RUN_DELAY_MIN", 0), env_float("STORY_RUN_DELAY_MAX", 3600)):
            return
        followees_file = BASE_DIR / "data" / "network" / "followees.jsonl"
        if os.environ.get("STORY_ACCOUNT_SCOPE") == "public":
            public_file = STATE_DIR / "public-followees.jsonl"
            write_public_accounts(followees_file, BASE_DIR / "data" / "profiles", public_file, args.login)
            followees_file = public_file
        followee_ids = load_followee_ids(followees_file, args.login)
        queue_file = STATE_DIR / "story-account-queue.json"
        userids = load_story_queue(
            queue_file,
            followee_ids,
            STATE_DIR / "story-last-run.json",
        )
        session_file = BASE_DIR / "sessions" / f"session-{args.login}"
        request_delay_min = env_float("REQUEST_DELAY_MIN", 10)
        request_delay_max = env_float("REQUEST_DELAY_MAX", 25)
        item_delay_min = env_float("STORY_ITEM_DELAY_MIN", 20)
        item_delay_max = env_float("STORY_ITEM_DELAY_MAX", 45)
        max_new_items = int(env_float("MAX_STORY_ITEMS_PER_RUN", 50))
        minimum_free_gib = env_float("MIN_STORY_FREE_GIB", 10)
        loader = instaloader.Instaloader(
            dirname_pattern=str(STORIES_DIR / "{target}"),
            filename_pattern="{date_utc:%Y-%m-%d_%H-%M-%S}_UTC_{mediaid}",
            download_pictures=True,
            download_videos=True,
            download_video_thumbnails=True,
            save_metadata=True,
            compress_json=False,
            storyitem_metadata_txt_pattern="",
            max_connection_attempts=1,
            request_timeout=90,
            iphone_support=False,
            rate_controller=lambda context: ConservativeRateController(
                context,
                request_delay_min,
                request_delay_max,
                cooldown_file,
            ),
        )
        install_query_audit(loader, "stories", LOG_DIR)
        loader.load_session_from_file(args.login, str(session_file))
        logged_in_as = loader.test_login()
        if logged_in_as != args.login:
            cooldown_file.write_text(
                f"{int(time.time())}\tstories-login-required\n",
                encoding="utf-8",
            )
            raise instaloader.LoginRequiredException(
                f"Session belongs to {logged_in_as!r}, expected {args.login!r}"
            )

        try:
            summary = archive_visible_stories(
                loader,
                userids,
                cooldown_file,
                item_delay_min,
                item_delay_max,
                minimum_free_gib,
                max_new_items,
                queue_file,
            )
        except BaseException as error:
            if requires_cooldown(error) and not cooldown_file.exists():
                cooldown_file.write_text(
                    f"{int(time.time())}\tstories-access-error\n",
                    encoding="utf-8",
                )
            raise

        print(
            "STORY_ARCHIVE_DONE "
            + " ".join(f"{key}={value}" for key, value in summary.items()),
            flush=True,
        )


if __name__ == "__main__":
    main()
