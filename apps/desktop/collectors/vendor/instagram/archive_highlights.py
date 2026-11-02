#!/usr/bin/env python3

from __future__ import annotations

import argparse
import fcntl
import json
import os
import random
import shutil
import time
from datetime import timezone
from pathlib import Path
from typing import Any

import instaloader

from archive_stories import (
    append_journal,
    effect_metadata,
    has_downloaded_media,
    manifest_complete,
    requires_cooldown,
    story_items,
    write_json,
)
from conservative import ConservativeRateController
from query_audit import install_query_audit


SOURCE_DIR = Path(__file__).resolve().parent
BASE_DIR = Path(os.environ["MONICA_INSTAGRAM_ROOT"])
HIGHLIGHTS_DIR = BASE_DIR / "data" / "highlights"
STATE_DIR = BASE_DIR / "state"
LOG_DIR = BASE_DIR / "logs"


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Conservatively archive followed profiles' Highlights."
    )
    parser.add_argument("--login", required=True)
    parser.add_argument(
        "--max-targets",
        type=int,
        default=int(os.environ.get("MAX_HIGHLIGHT_PROFILES_PER_RUN", "2")),
    )
    parser.add_argument(
        "--max-items",
        type=int,
        default=int(os.environ.get("MAX_HIGHLIGHT_ITEMS_PER_RUN", "20")),
    )
    return parser.parse_args()


def env_float(name: str, default: float) -> float:
    return float(os.environ.get(name, default))


def load_followees(path: Path, self_username: str) -> list[dict[str, int | str]]:
    followees: list[dict[str, int | str]] = []
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
            followees.append({"userid": userid, "username": row["username"]})
    return followees


def load_queue(path: Path, followees: list[dict[str, int | str]]) -> dict[str, Any]:
    if path.exists():
        queue = json.loads(path.read_text(encoding="utf-8"))
    else:
        queue = {
            "pending": [],
            "completed": [],
            "failed": [],
            "attempts": {},
            "updated_at": None,
        }

    known = {
        int(row["userid"])
        for key in ("pending", "completed", "failed")
        for row in queue[key]
    }
    queue["pending"].extend(
        row for row in followees if int(row["userid"]) not in known
    )
    return queue


def save_queue(path: Path, queue: dict[str, Any]) -> None:
    queue["updated_at"] = int(time.time())
    write_json(path, queue)


def highlight_group_document(
    highlight: instaloader.Highlight,
    items: list[instaloader.StoryItem],
    captured_at: int,
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "archive_kind": "highlight_group",
        "captured_at": captured_at,
        "highlight_id": str(highlight.unique_id),
        "highlight_title": highlight.title,
        "owner_id": highlight.owner_id,
        "owner_username": highlight.owner_username,
        "item_count": highlight.itemcount,
        "cover_url": highlight.cover_url,
        "cover_cropped_url": highlight.cover_cropped_url,
        "inventory_complete": len(items) == highlight.itemcount,
        "items": [
            {
                "mediaid": item.mediaid,
                "shortcode": item.shortcode,
                "taken_at": item.date_utc.replace(
                    tzinfo=timezone.utc
                ).isoformat(),
                "is_video": item.is_video,
            }
            for item in items
        ],
    }


def archive_profile_highlights(
    loader: instaloader.Instaloader,
    followee: dict[str, int | str],
    cooldown_file: Path,
    item_delay_min: float,
    item_delay_max: float,
    minimum_free_gib: float,
    max_new_items: int,
) -> dict[str, int | bool]:
    userid = int(followee["userid"])
    staged_username = str(followee["username"])
    journal = LOG_DIR / "archive-highlights.jsonl"
    highlights_seen = 0
    visible_items = 0
    downloaded_items = 0
    existing_items = 0
    stopped_low_disk = False
    stopped_item_limit = False

    for highlight in loader.get_highlights(userid):
        highlights_seen += 1
        owner = highlight.owner_username or staged_username
        highlight_id = str(highlight.unique_id)
        target = highlight_id
        highlight_dir = HIGHLIGHTS_DIR / owner / highlight_id
        items, _ = story_items(loader, highlight, cooldown_file)
        highlight_dir.mkdir(parents=True, exist_ok=True)
        write_json(
            highlight_dir / "highlight-group.manifest.json",
            highlight_group_document(highlight, items, int(time.time())),
        )

        for item in items:
            visible_items += 1
            manifest_path = (
                highlight_dir / f"highlight-{item.mediaid}.manifest.json"
            )
            if manifest_complete(manifest_path):
                existing_items += 1
                continue
            if downloaded_items >= max_new_items:
                stopped_item_limit = True
                break

            free_gib = shutil.disk_usage(HIGHLIGHTS_DIR).free / (1024**3)
            if free_gib < minimum_free_gib:
                stopped_low_disk = True
                break

            time.sleep(random.uniform(item_delay_min, item_delay_max))
            loader.download_storyitem(item, target)
            highlight_dir.mkdir(parents=True, exist_ok=True)
            files = sorted(
                path.name
                for path in highlight_dir.iterdir()
                if path.is_file()
                and str(item.mediaid) in path.name
                and path != manifest_path
            )
            if not has_downloaded_media(files):
                raise RuntimeError(
                    f"Highlight Story {item.mediaid} has no downloaded media"
                )

            raw_metadata = item._asdict()
            manifest = {
                "schema_version": 1,
                "archive_kind": "highlight",
                "captured_at": int(time.time()),
                "highlight_id": highlight_id,
                "highlight_title": highlight.title,
                "highlight_item_count": highlight.itemcount,
                "highlight_cover_url": highlight.cover_url,
                "mediaid": item.mediaid,
                "shortcode": item.shortcode,
                "owner_id": item.owner_id,
                "owner_username": owner,
                "taken_at": item.date_utc.replace(
                    tzinfo=timezone.utc
                ).isoformat(),
                "typename": item.typename,
                "is_video": item.is_video,
                "caption": item.caption,
                "files": files,
                "effect_metadata": effect_metadata(raw_metadata),
                "raw_metadata": raw_metadata,
            }
            write_json(manifest_path, manifest)
            append_journal(
                journal,
                {
                    "archived_at": manifest["captured_at"],
                    "highlight_id": highlight_id,
                    "highlight_title": highlight.title,
                    "mediaid": item.mediaid,
                    "owner_id": item.owner_id,
                    "owner_username": owner,
                    "is_video": item.is_video,
                    "files": files,
                    "effects": len(manifest["effect_metadata"]),
                },
            )
            downloaded_items += 1

        if stopped_low_disk or stopped_item_limit:
            break

    return {
        "highlights_seen": highlights_seen,
        "visible_items": visible_items,
        "downloaded_items": downloaded_items,
        "existing_items": existing_items,
        "stopped_low_disk": stopped_low_disk,
        "stopped_item_limit": stopped_item_limit,
    }


def run_queue(
    loader: instaloader.Instaloader,
    followees: list[dict[str, int | str]],
    max_targets: int,
    max_items: int,
    cooldown_file: Path,
    item_delay_min: float,
    item_delay_max: float,
    profile_delay_min: float,
    profile_delay_max: float,
    minimum_free_gib: float,
) -> dict[str, int | bool]:
    queue_path = STATE_DIR / "highlight-history-queue.json"
    queue = load_queue(queue_path, followees)
    save_queue(queue_path, queue)
    processed = 0
    downloaded = 0
    stopped_low_disk = False
    stopped_item_limit = False

    while (
        queue["pending"]
        and processed < max_targets
        and downloaded < max_items
    ):
        followee = queue["pending"].pop(0)
        userid_key = str(followee["userid"])
        queue["attempts"][userid_key] = queue["attempts"].get(userid_key, 0) + 1
        save_queue(queue_path, queue)
        print(
            "Starting historical Highlight archive for "
            f"{followee['username']} ({processed + 1}/{max_targets}).",
            flush=True,
        )

        try:
            result = archive_profile_highlights(
                loader,
                followee,
                cooldown_file,
                item_delay_min,
                item_delay_max,
                minimum_free_gib,
                max_items - downloaded,
            )
        except instaloader.InstaloaderException as error:
            if requires_cooldown(error):
                if not cooldown_file.exists():
                    cooldown_file.write_text(
                        f"{int(time.time())}\thighlights-access-error\n",
                        encoding="utf-8",
                    )
                queue["pending"].insert(0, followee)
            elif queue["attempts"][userid_key] >= 3:
                queue["failed"].append(
                    {
                        **followee,
                        "failed_at": int(time.time()),
                        "reason": f"{type(error).__name__}: {error}",
                    }
                )
            else:
                queue["pending"].append(followee)
            save_queue(queue_path, queue)
            raise
        except BaseException:
            queue["pending"].insert(0, followee)
            save_queue(queue_path, queue)
            raise

        downloaded += int(result["downloaded_items"])
        stopped_low_disk = bool(result["stopped_low_disk"])
        stopped_item_limit = bool(result["stopped_item_limit"])
        if stopped_low_disk or stopped_item_limit:
            queue["pending"].insert(0, followee)
            save_queue(queue_path, queue)
            break

        queue["completed"].append(
            {
                **followee,
                "completed_at": int(time.time()),
                **result,
            }
        )
        processed += 1
        save_queue(queue_path, queue)

        if queue["pending"] and processed < max_targets:
            pause = random.uniform(profile_delay_min, profile_delay_max)
            print(f"Highlight profile pause: {pause / 60:.1f} minutes.", flush=True)
            time.sleep(pause)

    return {
        "processed_profiles": processed,
        "completed_profiles": len(queue["completed"]),
        "pending_profiles": len(queue["pending"]),
        "failed_profiles": len(queue["failed"]),
        "downloaded_items": downloaded,
        "stopped_low_disk": stopped_low_disk,
        "stopped_item_limit": stopped_item_limit,
    }


def main() -> None:
    args = parse_arguments()
    os.umask(0o077)
    HIGHLIGHTS_DIR.mkdir(parents=True, exist_ok=True)
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    lock_path = STATE_DIR / "highlights.lock"
    with lock_path.open("w", encoding="utf-8") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("Another Highlight archive is already running.", flush=True)
            return

        cooldown_file = STATE_DIR / "COOLDOWN_REQUIRED"
        if cooldown_file.exists():
            print("Cooldown marker exists; Highlight archive will not access Instagram.")
            return

        followees = load_followees(
            BASE_DIR / "data" / "network" / "followees.jsonl",
            args.login,
        )
        request_delay_min = env_float("REQUEST_DELAY_MIN", 10)
        request_delay_max = env_float("REQUEST_DELAY_MAX", 25)
        item_delay_min = env_float("HIGHLIGHT_ITEM_DELAY_MIN", 20)
        item_delay_max = env_float("HIGHLIGHT_ITEM_DELAY_MAX", 45)
        profile_delay_min = env_float("HIGHLIGHT_PROFILE_DELAY_MIN", 300)
        profile_delay_max = env_float("HIGHLIGHT_PROFILE_DELAY_MAX", 600)
        minimum_free_gib = env_float("MIN_STORY_FREE_GIB", 2)

        loader = instaloader.Instaloader(
            dirname_pattern=str(
                HIGHLIGHTS_DIR / "{owner_username}" / "{target}"
            ),
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
            sanitize_paths=True,
            rate_controller=lambda context: ConservativeRateController(
                context,
                request_delay_min,
                request_delay_max,
                cooldown_file,
            ),
        )
        install_query_audit(loader, "highlights", LOG_DIR)
        loader.load_session_from_file(
            args.login,
            str(BASE_DIR / "sessions" / f"session-{args.login}"),
        )
        logged_in_as = loader.test_login()
        if logged_in_as != args.login:
            cooldown_file.write_text(
                f"{int(time.time())}\thighlights-login-required\n",
                encoding="utf-8",
            )
            raise instaloader.LoginRequiredException(
                f"Session belongs to {logged_in_as!r}, expected {args.login!r}"
            )

        try:
            summary = run_queue(
                loader,
                followees,
                args.max_targets,
                args.max_items,
                cooldown_file,
                item_delay_min,
                item_delay_max,
                profile_delay_min,
                profile_delay_max,
                minimum_free_gib,
            )
        except BaseException as error:
            if requires_cooldown(error) and not cooldown_file.exists():
                cooldown_file.write_text(
                    f"{int(time.time())}\thighlights-access-error\n",
                    encoding="utf-8",
                )
            raise

        summary["scan_completed_at"] = int(time.time())
        summary["max_profiles_per_run"] = args.max_targets
        summary["max_items_per_run"] = args.max_items
        write_json(STATE_DIR / "highlight-last-run.json", summary)
        print(
            "HIGHLIGHT_ARCHIVE_DONE "
            + " ".join(f"{key}={value}" for key, value in summary.items()),
            flush=True,
        )


if __name__ == "__main__":
    main()
