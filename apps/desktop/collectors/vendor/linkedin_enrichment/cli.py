"""Command-line interface for the local collector."""

import argparse
import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import TypeAdapter

from .assets import AssetArchive, is_linkedin_image_url
from .browser import PersistentChromium
from .collector import run_batch, run_once
from .connections import refresh_connections
from .messaging import backfill_messages, scan_messages
from .models import Target
from .storage import Store, atomic_write_json

DEFAULT_DB = Path("data/linkedin-enrichment.sqlite3")
DEFAULT_PROFILE = Path("data/chromium-profile")


async def _interactive_login(
    user_data_dir: Path,
    timeout_minutes: int,
    storage_state_out: Path | None = None,
) -> None:
    """Keep a headed persistent browser open until LinkedIn reaches the feed."""

    async with PersistentChromium(user_data_dir, headless=False) as browser:
        await browser.page.goto(
            "https://www.linkedin.com/login",
            wait_until="domcontentloaded",
        )
        for _ in range(timeout_minutes * 30):
            if "/feed" in browser.page.url:
                if storage_state_out is not None:
                    await browser.export_storage_state(storage_state_out)
                return
            await browser.page.wait_for_timeout(2_000)
    raise TimeoutError("LinkedIn login did not reach /feed before the timeout")


async def _import_session(user_data_dir: Path, storage_state: Path) -> None:
    """Import a local login into the persistent NAS browser profile."""

    async with PersistentChromium(user_data_dir, headless=True) as browser:
        await browser.import_storage_state(storage_state)
        await browser.page.goto(
            "https://www.linkedin.com/feed/",
            wait_until="domcontentloaded",
        )
        if "/feed" not in browser.page.url:
            raise RuntimeError(
                f"imported LinkedIn session was not authenticated: {browser.page.url}"
            )


async def _backfill_assets(
    store: Store,
    user_data_dir: Path,
    asset_dir: Path,
) -> tuple[int, int]:
    urls = [url for url in store.asset_urls() if is_linkedin_image_url(url)]
    async with PersistentChromium(user_data_dir, headless=True) as browser:
        return await AssetArchive(
            store,
            browser.context.request,
            asset_dir,
        ).archive_urls(urls)


def _load_targets(path: Path) -> list[Target]:
    payload: Any = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(payload, dict):
        payload = payload.get("targets")
    if not isinstance(payload, list):
        raise ValueError("targets JSON must be a list or an object with 'targets'")
    return TypeAdapter(list[Target]).validate_python(payload)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="linkedin-enrichment")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--user-data-dir", type=Path, default=DEFAULT_PROFILE)
    parser.add_argument(
        "--asset-dir",
        type=Path,
        default=Path("data/linkedin-assets"),
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("init")
    login = commands.add_parser("login")
    login.add_argument("--timeout-minutes", type=int, default=30)
    login.add_argument("--storage-state-out", type=Path)
    import_session = commands.add_parser("import-session")
    import_session.add_argument("storage_state", type=Path)

    enqueue = commands.add_parser("enqueue")
    enqueue.add_argument("targets_json", type=Path)

    export = commands.add_parser("export")
    export.add_argument("output", type=Path)
    export.add_argument("--as-of")
    export.add_argument("--snapshot-hash")
    export.add_argument("--include-unlinked", action="store_true")

    for name in ("run-once", "run-batch", "canary"):
        command = commands.add_parser(name)
        command.add_argument("--headed", action="store_true")
        command.add_argument("--window-cap", type=int, default=3)
        command.add_argument("--window-hours", type=int, default=2)
        command.add_argument("--rolling-cap", type=int, default=36)
        command.add_argument("--rolling-hours", type=int, default=24)
        command.add_argument("--post-limit", type=int, default=10)
        command.add_argument("--max-scrolls", type=int, default=5)
        command.add_argument(
            "--manual-checkpoint-timeout-minutes",
            type=int,
            default=0,
        )
        if name == "canary":
            command.add_argument("--profile-url", required=True)
        elif name == "run-batch":
            command.add_argument("--max-contacts", type=int, default=3)
            command.add_argument("--profile-url", action="append")

    commands.add_parser("status")
    commands.add_parser("backfill-assets")
    scan_connections = commands.add_parser("scan-connections")
    scan_connections.add_argument("--headed", action="store_true")
    scan_connections.add_argument("--max-scrolls", type=int, default=60)
    scan_messages_command = commands.add_parser("scan-messages")
    scan_messages_command.add_argument("--headed", action="store_true")
    backfill_messages_command = commands.add_parser("backfill-messages")
    backfill_messages_command.add_argument("--headed", action="store_true")
    backfill_messages_command.add_argument(
        "--max-conversations",
        type=int,
        default=500,
    )
    return parser


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    store = Store(args.db)
    store.initialize()

    if args.command == "init":
        print(json.dumps({"database": str(args.db), "initialized": True}))
    elif args.command == "login":
        if args.timeout_minutes < 1:
            raise ValueError("timeout-minutes must be at least 1")
        asyncio.run(
            _interactive_login(
                args.user_data_dir,
                args.timeout_minutes,
                args.storage_state_out,
            )
        )
        print(
            json.dumps(
                {
                    "authenticated": True,
                    "storage_state": (
                        str(args.storage_state_out)
                        if args.storage_state_out is not None
                        else None
                    ),
                }
            )
        )
    elif args.command == "import-session":
        asyncio.run(_import_session(args.user_data_dir, args.storage_state))
        print(json.dumps({"authenticated": True, "imported": True}))
    elif args.command == "enqueue":
        count = store.enqueue(
            _load_targets(args.targets_json),
            reconcile_metadata_key="contact_id",
        )
        print(json.dumps({"enqueued": count}))
    elif args.command == "export":
        as_of = datetime.fromisoformat(args.as_of) if args.as_of else None
        if as_of is not None and as_of.tzinfo is None:
            as_of = as_of.replace(tzinfo=UTC)
        atomic_write_json(
            args.output,
            store.export_payload(
                as_of=as_of,
                snapshot_hash=args.snapshot_hash,
                include_unlinked=args.include_unlinked,
            ),
        )
        print(json.dumps({"exported": str(args.output)}))
    elif args.command == "status":
        print(json.dumps(store.status(), indent=2, sort_keys=True))
    elif args.command == "backfill-assets":
        archived, failed = asyncio.run(
            _backfill_assets(store, args.user_data_dir, args.asset_dir)
        )
        print(json.dumps({"archived": archived, "failed": failed}))
    elif args.command == "scan-connections":
        if args.max_scrolls < 1:
            raise ValueError("max-scrolls must be at least 1")
        result = asyncio.run(
            refresh_connections(
                store,
                args.user_data_dir,
                headless=not args.headed,
                max_scrolls=args.max_scrolls,
            )
        )
        print(json.dumps(result))
    elif args.command == "scan-messages":
        result = asyncio.run(
            scan_messages(
                store,
                args.user_data_dir,
                headless=not args.headed,
            )
        )
        print(json.dumps(result))
    elif args.command == "backfill-messages":
        if args.max_conversations < 1:
            raise ValueError("max-conversations must be at least 1")
        result = asyncio.run(
            backfill_messages(
                store,
                args.user_data_dir,
                headless=not args.headed,
                max_conversations=args.max_conversations,
            )
        )
        print(json.dumps(result))
    elif args.command == "canary":
        if args.manual_checkpoint_timeout_minutes and not args.headed:
            raise ValueError("manual checkpoint waiting requires --headed")
        if min(
            args.window_cap,
            args.window_hours,
            args.rolling_cap,
            args.rolling_hours,
        ) < 1:
            raise ValueError("window and rolling limits must be at least 1")
        target = Target(profile_url=args.profile_url)
        store.enqueue([target])
        processed = asyncio.run(
            run_once(
                store,
                user_data_dir=args.user_data_dir,
                window_cap=args.window_cap,
                window_hours=args.window_hours,
                rolling_cap=args.rolling_cap,
                rolling_hours=args.rolling_hours,
                profile_url=target.url,
                headless=not args.headed,
                post_limit=args.post_limit,
                max_scrolls=args.max_scrolls,
                asset_dir=args.asset_dir,
                manual_checkpoint_timeout_minutes=(
                    args.manual_checkpoint_timeout_minutes
                ),
            )
        )
        print(json.dumps({"processed": processed, "profile_url": target.url}))
    elif args.command == "run-batch":
        if args.manual_checkpoint_timeout_minutes and not args.headed:
            raise ValueError("manual checkpoint waiting requires --headed")
        if min(
            args.max_contacts,
            args.window_cap,
            args.window_hours,
            args.rolling_cap,
            args.rolling_hours,
        ) < 1:
            raise ValueError("batch size and limits must be at least 1")
        processed = asyncio.run(
            run_batch(
                store,
                user_data_dir=args.user_data_dir,
                max_contacts=args.max_contacts,
                window_cap=args.window_cap,
                window_hours=args.window_hours,
                rolling_cap=args.rolling_cap,
                rolling_hours=args.rolling_hours,
                profile_urls=args.profile_url,
                headless=not args.headed,
                post_limit=args.post_limit,
                max_scrolls=args.max_scrolls,
                asset_dir=args.asset_dir,
                manual_checkpoint_timeout_minutes=(
                    args.manual_checkpoint_timeout_minutes
                ),
            )
        )
        print(json.dumps({"processed": processed}))
    else:
        if args.manual_checkpoint_timeout_minutes and not args.headed:
            raise ValueError("manual checkpoint waiting requires --headed")
        if min(
            args.window_cap,
            args.window_hours,
            args.rolling_cap,
            args.rolling_hours,
        ) < 1:
            raise ValueError("window and rolling limits must be at least 1")
        processed = asyncio.run(
            run_once(
                store,
                user_data_dir=args.user_data_dir,
                window_cap=args.window_cap,
                window_hours=args.window_hours,
                rolling_cap=args.rolling_cap,
                rolling_hours=args.rolling_hours,
                headless=not args.headed,
                post_limit=args.post_limit,
                max_scrolls=args.max_scrolls,
                asset_dir=args.asset_dir,
                manual_checkpoint_timeout_minutes=(
                    args.manual_checkpoint_timeout_minutes
                ),
            )
        )
        print(json.dumps({"processed": processed}))


if __name__ == "__main__":
    main()
