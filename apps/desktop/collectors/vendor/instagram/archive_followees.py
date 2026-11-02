#!/usr/bin/env python3

from __future__ import annotations

import argparse
import fcntl
import json
import os
import random
import subprocess
import sys
import time
from pathlib import Path


SOURCE_DIR = Path(__file__).resolve().parent
BASE_DIR = Path(os.environ["MONICA_INSTAGRAM_ROOT"])


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run a bounded, sequential historical archive of followed profiles."
    )
    parser.add_argument("--login", required=True)
    parser.add_argument(
        "--max-targets",
        type=int,
        default=int(os.environ.get("MAX_FRIENDS_PER_RUN", "5")),
    )
    parser.add_argument(
        "--posts-per-target",
        type=int,
        default=int(os.environ.get("POSTS_PER_FRIEND_PASS", "40")),
    )
    return parser.parse_args()


def load_followees(path: Path, self_username: str) -> list[str]:
    usernames: list[str] = []
    seen: set[str] = set()
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            row = json.loads(line)
            username = row["username"]
            if username == self_username or username in seen:
                continue
            seen.add(username)
            usernames.append(username)
    return usernames


def load_queue(path: Path, usernames: list[str]) -> dict:
    if path.exists():
        with path.open(encoding="utf-8") as handle:
            queue = json.load(handle)
    else:
        queue = {
            "pending": [],
            "completed": [],
            "failed": [],
            "attempts": {},
            "updated_at": None,
        }

    queue.setdefault("failed", [])
    failed_usernames = {row["username"] for row in queue["failed"]}
    known = set(queue["pending"]) | set(queue["completed"]) | failed_usernames
    queue["pending"].extend(username for username in usernames if username not in known)
    return queue


def save_queue(path: Path, queue: dict) -> None:
    queue["updated_at"] = int(time.time())
    temporary = path.with_suffix(".json.tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(queue, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    temporary.replace(path)


def requires_cooldown(output: str) -> bool:
    normalized = "\n".join(output.lower().splitlines()[-20:])
    return any(
        marker in normalized
        for marker in (
            "403 forbidden",
            "feedback_required",
            "loginrequiredexception",
        )
    )


def run_queue(args: argparse.Namespace) -> None:
    followees_file = BASE_DIR / "data" / "network" / "followees.jsonl"
    if not followees_file.exists():
        raise FileNotFoundError("Followees export is not complete or has not started")

    cooldown_file = BASE_DIR / "state" / "COOLDOWN_REQUIRED"
    if cooldown_file.exists():
        raise RuntimeError("Cooldown marker exists; refusing to access Instagram")

    usernames = load_followees(followees_file, args.login)
    queue_path = BASE_DIR / "state" / "friend-history-queue.json"
    queue = load_queue(queue_path, usernames)
    save_queue(queue_path, queue)

    processed_targets = 0
    while queue["pending"] and processed_targets < args.max_targets:
        username = queue["pending"].pop(0)
        completed_file = BASE_DIR / "state" / f"profile-complete-{username}.json"
        if completed_file.exists():
            queue["completed"].append(username)
            save_queue(queue_path, queue)
            continue

        print(
            f"Starting historical metadata pass for {username} "
            f"({processed_targets + 1}/{args.max_targets}).",
            flush=True,
        )
        queue["attempts"][username] = queue["attempts"].get(username, 0) + 1
        save_queue(queue_path, queue)

        result = subprocess.run(
            [
                sys.executable,
                str(SOURCE_DIR / "archive_profile.py"),
                "--login",
                args.login,
                "--target",
                username,
                "--max-posts",
                str(args.posts_per_target),
                *(["--media"] if os.environ.get("DESKTOP_INSTAGRAM_MEDIA") == "1" else []),
            ],
            cwd=BASE_DIR,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            check=False,
        )
        print(result.stdout, end="", flush=True)
        if result.returncode != 0:
            output = result.stdout.lower()
            if cooldown_file.exists() or requires_cooldown(result.stdout):
                if not cooldown_file.exists():
                    cooldown_file.write_text(
                        f"{int(time.time())}\t{username}\taccess-or-session-error\n",
                        encoding="utf-8",
                    )
                queue["pending"].insert(0, username)
            elif (
                "profilenotexistsexception" in output
                or "privateprofilenotfollowedexception" in output
            ):
                queue["failed"].append(
                    {
                        "username": username,
                        "failed_at": int(time.time()),
                        "reason": "profile unavailable to this session",
                    }
                )
            elif queue["attempts"][username] >= 3:
                queue["failed"].append(
                    {
                        "username": username,
                        "failed_at": int(time.time()),
                        "reason": "three failed archive attempts",
                    }
                )
            else:
                queue["pending"].append(username)
            save_queue(queue_path, queue)
            raise RuntimeError(
                f"Historical archive stopped on {username} "
                f"with exit code {result.returncode}"
            )
        if cooldown_file.exists():
            queue["pending"].insert(0, username)
            save_queue(queue_path, queue)
            raise RuntimeError("Instagram rate limit detected; queue stopped")

        if completed_file.exists():
            queue["completed"].append(username)
        else:
            queue["pending"].append(username)
        processed_targets += 1
        save_queue(queue_path, queue)

        if queue["pending"] and processed_targets < args.max_targets:
            pause = random.uniform(600, 1800)
            print(
                f"Friend-to-friend pause: {pause / 60:.1f} minutes.",
                flush=True,
            )
            time.sleep(pause)

    print(
        f"Historical pass finished: {processed_targets} profiles attempted, "
        f"{len(queue['completed'])} complete, "
        f"{len(queue['pending'])} pending, "
        f"{len(queue['failed'])} unavailable.",
        flush=True,
    )


def main() -> None:
    args = parse_arguments()
    os.umask(0o077)
    lock_path = BASE_DIR / "state" / "friend-history.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("w", encoding="utf-8") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("Another friend-history cycle is still running.", flush=True)
            return
        run_queue(args)


if __name__ == "__main__":
    main()
