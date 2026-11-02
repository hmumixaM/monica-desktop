#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path
from typing import Any


SOURCE_DIR = Path(__file__).resolve().parent
BASE_DIR = Path(os.environ["MONICA_INSTAGRAM_ROOT"])
STATE_DIR = BASE_DIR / "state"
PROFILES_DIR = BASE_DIR / "data" / "profiles"
FOLLOWEES_FILE = BASE_DIR / "data" / "network" / "followees.jsonl"
MANUAL_FILE = STATE_DIR / "manual-followees.jsonl"
AUTO_IMPORT_FILE = STATE_DIR / "monica-auto-followees.jsonl"
POST_QUEUE_FILE = STATE_DIR / "friend-history-queue.json"


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line
    ]


def write_jsonl(path: Path, records: list[dict[str, Any]]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(
                json.dumps(record, ensure_ascii=False, separators=(",", ":"))
                + "\n"
            )
    temporary.replace(path)


def write_json(path: Path, document: dict[str, Any]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(document, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def add_manual_followees(usernames: list[str], now: int) -> list[str]:
    normalized = list(dict.fromkeys(username.strip().lower() for username in usernames))
    if any(not username for username in normalized):
        raise ValueError("Instagram username cannot be empty")

    manual = {
        row["username"].lower(): row
        for row in load_jsonl(MANUAL_FILE)
    }
    added = []
    for username in normalized:
        if username not in manual:
            manual[username] = {
                "username": username,
                "added_at": now,
                "status": "pending_profile",
            }
            added.append(username)
    write_jsonl(MANUAL_FILE, list(manual.values()))

    queue = json.loads(POST_QUEUE_FILE.read_text(encoding="utf-8"))
    known = (
        set(queue["pending"])
        | set(queue["completed"])
        | {row["username"] for row in queue.get("failed", [])}
    )
    new_pending = [username for username in normalized if username not in known]
    queue["pending"] = new_pending + queue["pending"]
    queue["updated_at"] = now
    write_json(POST_QUEUE_FILE, queue)
    return added


def reconcile_manual_followees(now: int) -> list[str]:
    manual = load_jsonl(MANUAL_FILE)
    followees = load_jsonl(FOLLOWEES_FILE)
    followees_by_id = {int(row["userid"]): row for row in followees}
    auto_import = load_jsonl(AUTO_IMPORT_FILE)
    auto_by_id = {int(row["userid"]): row for row in auto_import}
    resolved = []

    for row in manual:
        profile_path = PROFILES_DIR / row["username"] / "profile.json"
        if not profile_path.exists():
            continue
        profile = json.loads(profile_path.read_text(encoding="utf-8"))
        userid = int(profile["userid"])
        record = {
            "exported_at": now,
            "kind": "followees",
            "userid": userid,
            "username": profile["username"],
            "full_name": profile.get("full_name"),
            "is_private": profile.get("is_private"),
            "profile_pic_url": profile.get("profile_pic_url"),
        }
        followees_by_id[userid] = record
        auto_by_id[userid] = {
            **record,
            "auto_import": True,
            "discovered_at": row["added_at"],
        }
        row.update(
            {
                "status": "resolved",
                "userid": userid,
                "resolved_at": now,
                "username": profile["username"],
            }
        )
        resolved.append(profile["username"])

    if resolved:
        write_jsonl(FOLLOWEES_FILE, list(followees_by_id.values()))
        write_jsonl(AUTO_IMPORT_FILE, list(auto_by_id.values()))
        write_jsonl(MANUAL_FILE, manual)
        (STATE_DIR / "expected-followees-count").write_text(
            f"{len(followees_by_id)}\n",
            encoding="utf-8",
        )
        (STATE_DIR / "MONICA_AUTO_IMPORT_DUE").write_text(
            f"{now}\n",
            encoding="utf-8",
        )
    return resolved


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Queue or reconcile manually named followed accounts."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    add_parser = subparsers.add_parser("add")
    add_parser.add_argument("usernames", nargs="+")
    subparsers.add_parser("reconcile")
    return parser.parse_args()


def main() -> None:
    args = parse_arguments()
    os.umask(0o077)
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    now = int(time.time())
    if args.command == "add":
        added = add_manual_followees(args.usernames, now)
        print(f"MANUAL_FOLLOWEES_ADDED count={len(added)} usernames={added}")
    else:
        resolved = reconcile_manual_followees(now)
        print(
            f"MANUAL_FOLLOWEES_RECONCILED "
            f"count={len(resolved)} usernames={resolved}"
        )


if __name__ == "__main__":
    main()
