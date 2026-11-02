#!/usr/bin/env python3

from __future__ import annotations

import argparse
import fcntl
import json
import os
import time
from pathlib import Path
from typing import Any

import instaloader

from conservative import ConservativeRateController
from query_audit import install_query_audit


SOURCE_DIR = Path(__file__).resolve().parent
BASE_DIR = Path(os.environ["MONICA_INSTAGRAM_ROOT"])
STATE_DIR = BASE_DIR / "state"
NETWORK_DIR = BASE_DIR / "data" / "network"


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Atomically refresh the complete followed-account snapshot."
    )
    parser.add_argument("--login", required=True)
    parser.add_argument(
        "--minimum-interval-hours",
        type=float,
        default=float(os.environ.get("FOLLOWEE_REFRESH_INTERVAL_HOURS", "336")),
    )
    return parser.parse_args()


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line
    ]


def index_by_userid(records: list[dict[str, Any]]) -> dict[int, dict[str, Any]]:
    return {int(record["userid"]): record for record in records}


def newly_followed(
    previous: list[dict[str, Any]],
    current: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    previous_ids = set(index_by_userid(previous))
    return [
        record
        for record in current
        if int(record["userid"]) not in previous_ids
    ]


def refresh_due(summary_path: Path, now: int, interval_hours: float) -> bool:
    if not summary_path.exists():
        return True
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    return now - int(summary["completed_at"]) >= interval_hours * 60 * 60


def write_json(path: Path, document: dict[str, Any]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(document, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def write_jsonl(path: Path, records: list[dict[str, Any]]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(
                json.dumps(record, ensure_ascii=False, separators=(",", ":"))
                + "\n"
            )
    temporary.replace(path)


def merge_registry(
    registry_path: Path,
    records: list[dict[str, Any]],
    discovered_at: int,
) -> int:
    registry = index_by_userid(load_jsonl(registry_path))
    added = 0
    for record in records:
        userid = int(record["userid"])
        if userid not in registry:
            added += 1
        registry[userid] = {
            **record,
            "userid": userid,
            "auto_import": True,
            "discovered_at": registry.get(userid, {}).get(
                "discovered_at",
                discovered_at,
            ),
        }
    write_jsonl(
        registry_path,
        sorted(registry.values(), key=lambda row: (row["discovered_at"], row["userid"])),
    )
    return added


def profile_record(profile: instaloader.Profile, captured_at: int) -> dict[str, Any]:
    return {
        "exported_at": captured_at,
        "kind": "followees",
        "userid": int(profile.userid),
        "username": profile.username,
        "full_name": profile.full_name,
        "is_private": profile.is_private,
        "profile_pic_url": profile.profile_pic_url,
    }


def cooldown_scope(error: BaseException) -> str | None:
    message = str(error).lower()
    if any(
        marker in message
        for marker in (
            "403 forbidden",
            "429 too many requests",
            "loginrequiredexception",
        )
    ):
        return "global"
    if "challenge_required" in message or "feedback_required" in message:
        return "refresh"
    return None


def run_refresh(login: str, interval_hours: float) -> dict[str, Any]:
    now = int(time.time())
    summary_path = STATE_DIR / "followee-refresh-last-run.json"
    if not refresh_due(summary_path, now, interval_hours):
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
        return {"skipped": True, **summary}

    global_cooldown_file = STATE_DIR / "COOLDOWN_REQUIRED"
    refresh_cooldown_file = STATE_DIR / "FOLLOWEE_REFRESH_COOLDOWN_REQUIRED"
    if global_cooldown_file.exists():
        raise RuntimeError("Cooldown marker exists; refusing to refresh followees")
    if refresh_cooldown_file.exists():
        raise RuntimeError(
            "Followee refresh cooldown exists; refusing to refresh followees"
        )

    previous_path = NETWORK_DIR / "followees.jsonl"
    previous = list(index_by_userid(load_jsonl(previous_path)).values())
    session_file = BASE_DIR / "sessions" / f"session-{login}"
    loader = instaloader.Instaloader(
        sleep=True,
        quiet=False,
        download_pictures=False,
        download_videos=False,
        download_video_thumbnails=False,
        download_geotags=False,
        download_comments=False,
        save_metadata=False,
        max_connection_attempts=1,
        request_timeout=120,
        fatal_status_codes=[403],
        iphone_support=False,
        rate_controller=lambda context: ConservativeRateController(
            context,
            float(os.environ.get("REQUEST_DELAY_MIN", "10")),
            float(os.environ.get("REQUEST_DELAY_MAX", "25")),
            global_cooldown_file,
        ),
    )
    install_query_audit(loader, "followee-refresh", BASE_DIR / "logs")
    loader.load_session_from_file(login, str(session_file))
    if loader.test_login() != login:
        raise instaloader.LoginRequiredException(
            f"Session does not belong to {login}"
        )

    own_profile = instaloader.Profile.own_profile(loader.context)
    profiles = list(own_profile.get_followees())
    current = [profile_record(profile, now) for profile in profiles]
    current_by_id = index_by_userid(current)
    if len(current_by_id) != own_profile.followees:
        raise RuntimeError(
            "Followee refresh was incomplete: "
            f"received {len(current_by_id)} of {own_profile.followees}"
        )

    current = list(current_by_id.values())
    added_records = newly_followed(previous, current)
    removed_ids = set(index_by_userid(previous)) - set(current_by_id)

    NETWORK_DIR.mkdir(parents=True, exist_ok=True)
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    write_jsonl(previous_path, current)
    (STATE_DIR / "expected-followees-count").write_text(
        f"{len(current)}\n",
        encoding="utf-8",
    )
    registered = merge_registry(
        STATE_DIR / "monica-auto-followees.jsonl",
        added_records,
        now,
    )
    summary = {
        "started_at": now,
        "completed_at": int(time.time()),
        "previous_count": len(previous),
        "current_count": len(current),
        "new_count": len(added_records),
        "registered_for_monica": registered,
        "removed_count": len(removed_ids),
        "new_usernames": [record["username"] for record in added_records],
        "removed_userids": sorted(removed_ids),
        "iphone_reels_api_requested": False,
    }
    write_json(summary_path, summary)
    (STATE_DIR / "MONICA_AUTO_IMPORT_DUE").write_text(
        f"{summary['completed_at']}\n",
        encoding="utf-8",
    )
    return summary


def main() -> None:
    args = parse_arguments()
    os.umask(0o077)
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with (STATE_DIR / "network.lock").open("w", encoding="utf-8") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("Another network operation is running.")
            return
        try:
            summary = run_refresh(args.login, args.minimum_interval_hours)
        except BaseException as error:
            scope = cooldown_scope(error)
            marker_name = (
                "COOLDOWN_REQUIRED"
                if scope == "global"
                else "FOLLOWEE_REFRESH_COOLDOWN_REQUIRED"
            )
            cooldown_file = STATE_DIR / marker_name
            if scope is not None and not cooldown_file.exists():
                cooldown_file.write_text(
                    f"{int(time.time())}\tfollowee-refresh\t{scope}-access-error\n",
                    encoding="utf-8",
                )
            raise
    print(
        "FOLLOWEE_REFRESH_DONE "
        + " ".join(f"{key}={value}" for key, value in summary.items()),
        flush=True,
    )


if __name__ == "__main__":
    main()
