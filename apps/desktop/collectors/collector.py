#!/usr/bin/env python3
"""Desktop collector lifecycle. No Docker, SSH, developer installs or shared sessions."""
import argparse
import asyncio
import fcntl
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import Request, build_opener, HTTPRedirectHandler

from publishing import atomic_json
import publishing

SOURCE = Path(__file__).resolve().parent
sys.path.insert(0, str(SOURCE / "vendor"))
STOP = threading.Event()


class NeedsLogin(RuntimeError):
    pass


class CollectionPaused(RuntimeError):
    pass


def emit(**value):
    print(json.dumps(value, ensure_ascii=False), flush=True)


def read_json(path, default=None):
    return json.loads(path.read_text()) if path.exists() else (default if default is not None else {})


def status(root, **changes):
    path = root / "status.json"
    value = {**read_json(path), **changes, "updated_at": time.time()}
    atomic_json(path, value)
    return value


def wake_signature(root):
    try:
        return (root / "wake").stat().st_mtime_ns
    except FileNotFoundError:
        return None


def wait_for_schedule(root, deadline, observed):
    """Coalesce notifications received during collection and idle into one pass."""
    while not STOP.is_set() and time.time() < deadline:
        if wake_signature(root) != observed:
            return
        STOP.wait(min(1, max(0, deadline - time.time())))


@contextmanager
def exclusive(root):
    with (root / ".collector.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield


def run_child(root, args, *, env=None):
    """Keep provider diagnostics private; give every descendant a shutdown boundary."""
    with (root / "logs/collector.log").open("ab") as log:
        process = subprocess.Popen([sys.executable, *map(str, args)], cwd=root,
                                   env=env, stdout=log, stderr=log, start_new_session=True)
        while process.poll() is None:
            if STOP.wait(0.2):
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait()
                raise InterruptedError("Collector stopped")
        if process.returncode:
            raise RuntimeError("Collector phase failed; private log has diagnostics")


def instagram_blocked(root):
    state = root / "state"
    if (state / "COOLDOWN_REQUIRED").exists():
        return "access_checkpoint"
    pause = state / "COLLECTION_PAUSE_UNTIL"
    if pause.exists() and float(pause.read_text().split("\t", 1)[0]) > time.time():
        return "scheduled_pause"
    return None


async def instagram_login(root, username):
    """The user handles passwords, 2FA and checkpoints in the official headed page."""
    import instaloader
    from playwright.async_api import async_playwright

    if not re.fullmatch(r"[A-Za-z0-9_.]{1,30}", username or ""):
        raise ValueError("An Instagram username is required")
    async with async_playwright() as playwright:
        context = await playwright.chromium.launch_persistent_context(root / "browser", headless=False)
        try:
            page = context.pages[0] if context.pages else await context.new_page()
            await page.goto("https://www.instagram.com/accounts/login/", wait_until="domcontentloaded")
            deadline = time.monotonic() + 1800
            while time.monotonic() < deadline and not STOP.is_set():
                if page.is_closed():
                    raise NeedsLogin("Login window was closed")
                path = urlsplit(page.url).path
                cookies = {cookie["name"]: cookie["value"] for cookie in await context.cookies("https://www.instagram.com/")}
                if (cookies.get("sessionid") and cookies.get("ds_user_id")
                        and not any(word in path for word in ("login", "challenge", "checkpoint"))):
                    loader = instaloader.Instaloader(max_connection_attempts=1, request_timeout=30, iphone_support=False)
                    loader.context.load_session(username, cookies)
                    verified = await asyncio.to_thread(loader.test_login)
                    if verified != username:
                        raise NeedsLogin("The browser account differs from the configured Instagram username")
                    destination = root / "sessions" / ("session-" + username)
                    loader.save_session_to_file(str(destination))
                    destination.chmod(0o600)
                    atomic_json(root / "auth.json", {"username": username, "authenticated_at": time.time()})
                    return
                await asyncio.sleep(2)
            raise NeedsLogin("Login did not complete")
        finally:
            await context.close()


def refresh_completed_profiles(root):
    """Revisit completed histories every two weeks without resetting interrupted cursors."""
    queue_path = root / "state/friend-history-queue.json"
    queue = read_json(queue_path)
    if not queue:
        return
    for username in list(queue.get("completed", [])):
        if not re.fullmatch(r"[A-Za-z0-9_.]{1,30}", username):
            raise ValueError("Invalid archived Instagram username")
        marker = root / "state" / ("profile-complete-" + username + ".json")
        completed = read_json(marker)
        if completed and time.time() - completed.get("completed_at", time.time()) >= 14 * 86400:
            marker.replace(marker.with_suffix(".previous.json"))
            queue["completed"].remove(username)
            if username not in queue["pending"]:
                queue["pending"].append(username)
            queue["attempts"][username] = 0
    atomic_json(queue_path, queue)


def instagram_cycle(root, data_root, *, discover_only=False):
    username = read_json(root / "auth.json").get("username")
    if not username or not (root / "sessions" / ("session-" + username)).is_file():
        raise NeedsLogin("Instagram login is required")
    if instagram_blocked(root):
        raise CollectionPaused(instagram_blocked(root))
    env = {**os.environ, "MONICA_INSTAGRAM_ROOT": str(root), "DESKTOP_INSTAGRAM_MEDIA": "1"}
    # Preserve all original conservative per-request, per-post and daily limits.
    env.setdefault("MAX_FRIENDS_PER_RUN", "4")
    env.setdefault("MIN_STORY_FREE_GIB", "2")
    vendor = SOURCE / "vendor/instagram"
    if not (root / "state/FOLLOWEE_REFRESH_COOLDOWN_REQUIRED").exists():
        status(root, state="collecting", phase="followees")
        run_child(root, [vendor / "followee_refresh.py", "--login", username], env=env)
    elif not (root / "data/network/followees.jsonl").is_file():
        raise CollectionPaused("followee_discovery_checkpoint")
    if discover_only:
        return {"discovered": sum(1 for _ in (root / "data/network/followees.jsonl").open())}
    for script, phase in (("archive_stories.py", "stories"), ("archive_highlights.py", "highlights"), ("archive_followees.py", "posts")):
        if STOP.is_set():
            raise InterruptedError("Collector stopped")
        if instagram_blocked(root):
            raise CollectionPaused(instagram_blocked(root))
        pause = root / "state/POSTS_PAUSE_UNTIL"
        if phase == "posts" and pause.exists() and float(pause.read_text().split("\t", 1)[0]) > time.time():
            continue
        if phase == "posts":
            refresh_completed_profiles(root)
        status(root, state="collecting", phase=phase)
        try:
            run_child(root, [vendor / script, "--login", username], env=env)
        finally:
            # Publish completed artifacts even on interruption, scanning
            # only the tree this phase writes, not all historic media.
            publishing.instagram(root, data_root, kinds=("profiles" if phase == "posts" else phase,))
    return {"published": True}


def linkedin_store(root):
    from linkedin_enrichment.storage import Store
    store = Store(root / "archive.sqlite3")
    store.initialize()
    return store


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        return None


def manual_linkedin_targets(store):
    base = os.environ.get("MONICA_API_URL")
    token = os.environ.get("LINKEDIN_COLLECTION_TOKEN")
    if not base or not token:
        return
    parsed = urlsplit(base)
    if parsed.scheme != "http" or parsed.hostname != "127.0.0.1" or parsed.username or parsed.password:
        raise ValueError("Desktop target feed must be a loopback API")
    from linkedin_enrichment.crm_targets import MAX_FEED_BYTES, apply_targets
    request = Request(base.rstrip('/') + "/api/v1/linkedin/collection-targets", headers={"X-LinkedIn-Targets-Token": token})
    with build_opener(NoRedirect()).open(request, timeout=30) as response:
        raw = response.read(MAX_FEED_BYTES + 1)
    if len(raw) > MAX_FEED_BYTES:
        raise ValueError("Collector target feed is too large")
    apply_targets(store, json.loads(raw))


def linkedin_cycle(root, data_root, *, discover_only=False):
    if not (root / "auth.json").is_file():
        raise NeedsLogin("LinkedIn login is required")
    store = linkedin_store(root)
    circuit = store.status().get("circuit") or {}
    if circuit.get("paused_until") and datetime.fromisoformat(circuit["paused_until"]) > datetime.now(timezone.utc):
        raise CollectionPaused("linkedin_circuit_breaker")
    state = root / "state/schedule.json"
    schedule = read_json(state)
    base = ["--db", str(root / "archive.sqlite3"), "--user-data-dir", str(root / "browser"), "--asset-dir", str(root / "assets")]
    runner = [SOURCE / "run_linkedin.py", *base]
    if discover_only or time.time() - schedule.get("connections_at", 0) >= 86400:
        status(root, state="collecting", phase="connections")
        run_child(root, [*runner, "scan-connections"])
        schedule["connections_at"] = time.time()
        atomic_json(state, schedule)
    manual_linkedin_targets(store)
    if discover_only:
        return {"discovered": store.status()["active_targets"]}
    status(root, state="collecting", phase="profiles_posts")
    try:
        run_child(root, [*runner, "run-batch", "--max-contacts", "3", "--window-cap", "3", "--rolling-cap", "36"])
    finally:
        publishing.linkedin(root, data_root, store)
    return {"published": True, "active_targets": store.status()["active_targets"]}


def run_action(args, root):
    if args.action == "login":
        status(root, state="needs_login", phase="browser_login")
        if args.provider == "instagram":
            asyncio.run(instagram_login(root, args.username))
        else:
            run_child(root, [SOURCE / "run_linkedin.py", "--db", root / "archive.sqlite3", "--user-data-dir", root / "browser", "login"])
            atomic_json(root / "auth.json", {"authenticated_at": time.time()})
        return {"authenticated": True}
    cycle = instagram_cycle if args.provider == "instagram" else linkedin_cycle
    return cycle(root, args.data_root, discover_only=args.action == "discover")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider", choices=["instagram", "linkedin"], required=True)
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--username")
    parser.add_argument("action", choices=["login", "discover", "once", "watch", "status"])
    args = parser.parse_args()
    os.umask(0o077)
    args.data_root = args.data_root.expanduser().resolve()
    root = args.data_root / "collectors" / args.provider
    for name in ("state", "logs", "sessions", "assets", "data/network"):
        (root / name).mkdir(parents=True, exist_ok=True)
    if args.action == "status":
        emit(provider=args.provider, **read_json(root / "status.json", {"state": "needs_login"}))
        return
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: STOP.set())
    interval = 3 * 3600 if args.provider == "instagram" else 2 * 3600
    while not STOP.is_set():
        observed = wake_signature(root)
        try:
            with exclusive(root):
                result = run_action(args, root)
                completed = time.time()
                value = status(root, state="ready", phase="idle", last_success_at=completed,
                               last_success=datetime.fromtimestamp(completed, timezone.utc).isoformat(), error=None, **result)
                emit(provider=args.provider, **value)
        except BlockingIOError:
            emit(provider=args.provider, state="busy")
            if args.action != "watch":
                raise SystemExit(75)
        except InterruptedError:
            break
        except Exception as error:
            state = "needs_login" if isinstance(error, NeedsLogin) else "paused" if isinstance(error, CollectionPaused) else "failed"
            if args.provider == "instagram" and instagram_blocked(root):
                state = "paused"
            value = status(root, state=state, error=type(error).__name__, phase="idle")
            emit(provider=args.provider, **value)
            if args.action != "watch":
                raise SystemExit(1) from None
        if args.action != "watch":
            return
        # Wall clock due time makes sleep/wake catch up once, never replay every
        # missed historical cycle. Provider budgets and cooldowns still apply.
        deadline = time.time() + interval
        status(root, next_run_at=deadline)
        wait_for_schedule(root, deadline, observed)
    status(root, state="stopped", phase="idle")


if __name__ == "__main__":
    main()
