#!/usr/bin/env python3

from __future__ import annotations

import argparse
import fcntl
import json
import os
import random
import signal
import time
from pathlib import Path

import instaloader
from instaloader import FrozenNodeIterator, resumable_iteration

from conservative import ConservativeRateController
from query_audit import install_query_audit
from instagram_relationships import (
    post_is_eligible_for_collection,
    post_relationship_document,
    profile_relationship_document,
    write_document,
)


SOURCE_DIR = Path(__file__).resolve().parent
BASE_DIR = Path(os.environ["MONICA_INSTAGRAM_ROOT"])


class PlannedStop(instaloader.AbortDownloadException):
    pass


def stop_at_checkpoint(_: int, __: object) -> None:
    raise KeyboardInterrupt("Archive termination requested")


def env_float(name: str, default: float) -> float:
    return float(os.environ.get(name, default))


def env_int(name: str, default: int) -> int:
    return int(os.environ.get(name, default))


def load_frozen_iterator(
    _: instaloader.InstaloaderContext,
    path: str,
) -> FrozenNodeIterator:
    with open(path, encoding="utf-8") as handle:
        return FrozenNodeIterator(**json.load(handle))


def save_frozen_iterator(iterator: FrozenNodeIterator, path: str) -> None:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(iterator._asdict(), handle, separators=(",", ":"))
    temporary.replace(destination)


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Conservatively archive one Instagram profile."
    )
    parser.add_argument("--login", required=True, help="Logged-in Instagram username")
    parser.add_argument("--target", required=True, help="Profile to archive")
    parser.add_argument(
        "--media",
        action="store_true",
        help="Download pictures and videos in addition to metadata",
    )
    parser.add_argument(
        "--max-posts",
        type=int,
        default=env_int("MAX_POSTS_PER_RUN", 60),
        help="Maximum posts processed in this run",
    )
    return parser.parse_args()


def can_use_staged_profile(error: Exception) -> bool:
    message = str(error)
    return "web_profile_info" in message and (
        "feedback_required" in message
        or "laser.provider/ig_business_category_subvertical" in message
    )


def load_profile(
    context: instaloader.InstaloaderContext,
    username: str,
) -> instaloader.Profile:
    try:
        return instaloader.Profile.from_username(context, username)
    except (
        instaloader.AbortDownloadException,
        instaloader.QueryReturnedBadRequestException,
    ) as error:
        if not can_use_staged_profile(error):
            raise
        followees_file = BASE_DIR / "data" / "network" / "followees.jsonl"
        with followees_file.open(encoding="utf-8") as handle:
            staged = next(
                row
                for row in (json.loads(line) for line in handle)
                if row["username"] == username
            )
        context.error(
            f"Profile web endpoint failed for {username}; "
            "retrying with the staged user ID."
        )
        profile = instaloader.Profile(
            context,
            {
                "id": staged["userid"],
                "username": username,
            },
        )
        profile._obtain_metadata()
        return profile


def main() -> None:
    args = parse_arguments()
    os.umask(0o077)
    signal.signal(signal.SIGTERM, stop_at_checkpoint)

    state_dir = BASE_DIR / "state"
    log_dir = BASE_DIR / "logs"
    session_file = BASE_DIR / "sessions" / f"session-{args.login}"
    output_dir = BASE_DIR / "data" / "profiles"
    for directory in (state_dir, log_dir, session_file.parent, output_dir):
        directory.mkdir(parents=True, exist_ok=True)

    completed_file = state_dir / f"profile-complete-{args.target}.json"
    if completed_file.exists():
        print(f"Historical archive already complete for {args.target}.")
        return

    lock_path = state_dir / "archive.lock"
    with lock_path.open("w", encoding="utf-8") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise RuntimeError("Another Instagram archive process is running") from error

        request_delay_min = env_float("REQUEST_DELAY_MIN", 10)
        request_delay_max = env_float("REQUEST_DELAY_MAX", 25)
        post_delay_min = env_float("POST_DELAY_MIN", 90)
        post_delay_max = env_float("POST_DELAY_MAX", 180)
        batch_size_min = env_int("BATCH_SIZE_MIN", 15)
        batch_size_max = env_int("BATCH_SIZE_MAX", 25)
        batch_pause_min = env_float("BATCH_PAUSE_MIN", 1200)
        batch_pause_max = env_float("BATCH_PAUSE_MAX", 2400)

        loader = instaloader.Instaloader(
            sleep=True,
            quiet=False,
            dirname_pattern=str(output_dir / "{target}"),
            filename_pattern="{date_utc}_UTC_{shortcode}",
            download_pictures=args.media,
            download_videos=args.media,
            download_video_thumbnails=args.media,
            download_geotags=False,
            download_comments=False,
            save_metadata=True,
            compress_json=False,
            post_metadata_txt_pattern="{caption}",
            max_connection_attempts=1,
            request_timeout=120,
            fatal_status_codes=[403],
            iphone_support=False,
            sanitize_paths=True,
            rate_controller=lambda context: ConservativeRateController(
                context,
                request_delay_min,
                request_delay_max,
                state_dir / "COOLDOWN_REQUIRED",
            ),
        )
        install_query_audit(loader, "posts", log_dir)
        loader.load_session_from_file(args.login, str(session_file))
        logged_in_as = loader.test_login()
        if logged_in_as != args.login:
            raise RuntimeError(
                f"Session validation failed: expected {args.login}, got {logged_in_as}"
            )

        relationship_start_file = state_dir / "relationship-collection-started-at"
        if not relationship_start_file.exists():
            relationship_start_file.write_text(
                f"{int(time.time())}\n",
                encoding="utf-8",
            )
        relationship_started_at = int(
            relationship_start_file.read_text(encoding="utf-8").strip()
        )

        profile = load_profile(loader.context, args.target)
        profile_directory = output_dir / args.target
        profile_directory.mkdir(parents=True, exist_ok=True)
        profile_record = {
            "fetched_at": int(time.time()),
            "userid": profile.userid,
            "username": profile.username,
            "full_name": profile.full_name,
            "biography": profile.biography,
            "external_url": profile.external_url,
            "is_private": profile.is_private,
            "is_verified": profile.is_verified,
            "is_business_account": profile.is_business_account,
            "business_category_name": profile.business_category_name,
            "mediacount": profile.mediacount,
            "followers": profile.followers,
            "followees": profile.followees,
            "profile_pic_url": str(profile.profile_pic_url),
        }
        profile_temporary = profile_directory / "profile.json.tmp"
        with profile_temporary.open("w", encoding="utf-8") as handle:
            json.dump(
                profile_record,
                handle,
                ensure_ascii=False,
                indent=2,
            )
            handle.write("\n")
        profile_temporary.replace(profile_directory / "profile.json")

        raw_profile_path = profile_directory / "profile.raw-node.json"
        profile_was_archived = raw_profile_path.exists()
        raw_profile_temporary = raw_profile_path.with_suffix(".json.tmp")
        with raw_profile_temporary.open("w", encoding="utf-8") as handle:
            json.dump(
                {
                    "captured_at": int(time.time()),
                    "instaloader_version": instaloader.__version__,
                    "node": profile._node,
                },
                handle,
                ensure_ascii=False,
                indent=2,
            )
            handle.write("\n")
        raw_profile_temporary.replace(raw_profile_path)
        if not profile_was_archived:
            write_document(
                profile_relationship_document(profile),
                profile_directory / "profile.relationships.json",
            )

        iterator = profile.get_posts()
        processed = 0
        batch_processed = 0
        batch_target = random.randint(batch_size_min, batch_size_max)
        journal = log_dir / f"archive-{args.target}.jsonl"
        resume_pattern = state_dir / f"resume-{args.target}-{{}}.json"

        try:
            with resumable_iteration(
                context=loader.context,
                iterator=iterator,
                load=load_frozen_iterator,
                save=save_frozen_iterator,
                format_path=lambda magic: str(resume_pattern).format(magic),
            ) as (is_resuming, start_index):
                loader.context.log(
                    f"Archive target={args.target}, resuming={is_resuming}, "
                    f"start_index={start_index}, media={args.media}."
                )
                for post in iterator:
                    raw_post_path = (
                        profile_directory / f"post-{post.shortcode}.raw-node.json"
                    )
                    relationship_path = (
                        profile_directory / f"post-{post.shortcode}.relationships.json"
                    )
                    relationship_eligible = post_is_eligible_for_collection(
                        raw_post_path,
                        relationship_started_at,
                    )

                    downloaded = loader.download_post(post, target=args.target)
                    raw_post_temporary = raw_post_path.with_suffix(".json.tmp")
                    with raw_post_temporary.open("w", encoding="utf-8") as handle:
                        json.dump(
                            {
                                "captured_at": int(time.time()),
                                "instaloader_version": instaloader.__version__,
                                "shortcode": post.shortcode,
                                "node": post._node,
                            },
                            handle,
                            ensure_ascii=False,
                            indent=2,
                        )
                        handle.write("\n")
                    raw_post_temporary.replace(raw_post_path)
                    relationship_error = None
                    if relationship_eligible and not relationship_path.exists():
                        try:
                            write_document(
                                post_relationship_document(post),
                                relationship_path,
                            )
                        except (
                            instaloader.AbortDownloadException,
                            instaloader.ConnectionException,
                            KeyError,
                        ) as error:
                            if (
                                (state_dir / "COOLDOWN_REQUIRED").exists()
                                or isinstance(
                                    error,
                                    (
                                        instaloader.LoginRequiredException,
                                        instaloader.TooManyRequestsException,
                                    ),
                                )
                                or "403 Forbidden" in str(error)
                            ):
                                raise
                            relationship_error = (
                                f"{type(error).__name__}: {error}"
                            )
                            loader.context.error(
                                "Optional relationship collection failed for "
                                f"{post.shortcode}; continuing post archive: "
                                f"{relationship_error}"
                            )
                    with journal.open("a", encoding="utf-8") as handle:
                        handle.write(
                            json.dumps(
                                {
                                    "archived_at": int(time.time()),
                                    "shortcode": post.shortcode,
                                    "date_utc": post.date_utc.isoformat(),
                                    "downloaded": downloaded,
                                    "media": args.media,
                                    "relationships": (
                                        relationship_eligible
                                        and relationship_path.exists()
                                    ),
                                    "relationship_error": relationship_error,
                                },
                                separators=(",", ":"),
                            )
                        )
                        handle.write("\n")

                    processed += 1
                    batch_processed += 1
                    if processed >= args.max_posts:
                        raise PlannedStop(
                            f"Planned stop after {processed} posts; resume state saved."
                        )
                    if batch_processed >= batch_target:
                        pause = random.uniform(batch_pause_min, batch_pause_max)
                        loader.context.log(
                            f"Batch pause: {pause / 60:.1f} minutes after "
                            f"{batch_processed} posts."
                        )
                        time.sleep(pause)
                        batch_processed = 0
                        batch_target = random.randint(
                            batch_size_min,
                            batch_size_max,
                        )

                    pause = random.uniform(post_delay_min, post_delay_max)
                    loader.context.log(f"Post pause: {pause:.1f} seconds.")
                    time.sleep(pause)
        except PlannedStop as stop:
            loader.context.log(str(stop))
        else:
            completed_temporary = completed_file.with_suffix(".json.tmp")
            with completed_temporary.open("w", encoding="utf-8") as handle:
                json.dump(
                    {
                        "completed_at": int(time.time()),
                        "username": args.target,
                        "media": args.media,
                    },
                    handle,
                    separators=(",", ":"),
                )
                handle.write("\n")
            completed_temporary.replace(completed_file)


if __name__ == "__main__":
    main()
