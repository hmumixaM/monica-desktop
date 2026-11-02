"""Serial, source-specific archive refreshes without a global migration backfill."""

import argparse
import base64
import fcntl
import json
import os
import re
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import text

from .archive_media import instagram_archive, moments_cache
from .db import SessionLocal, engine
from .import_messages import ingest
from .jobs import lower_priority
from .reconcile_sources import (
    changed_moments,
    instagram,
    instagram_profiles,
    moment_fingerprints,
    moments,
)
from .wechat_contacts import CONTACT_IMPORT_VERSION, sync_contact_media, sync_contacts


def signature(paths):
    return [(str(p), p.stat().st_size, p.stat().st_mtime_ns) for p in paths if p.is_file()]


def import_signature(kind: str, paths: list[Path], cache: Path) -> list[tuple[str, int, int]]:
    del kind, cache
    return signature(paths + [Path(str(path) + "-wal") for path in paths])


def prefetch_wechat(source: Path, cache: Path) -> dict[str, object]:
    configured = os.environ.get("WECHAT_PREFETCH_SCRIPT", "").strip()
    if not configured:
        return {"state": "disabled"}
    script = Path(configured)
    database = source / "sns/sns.db"
    contacts = source / "contact/contact.db"
    if not script.is_file() or not database.is_file() or not contacts.is_file():
        raise FileNotFoundError("WeChat prefetch script or database is missing")
    command = [
        sys.executable,
        str(script),
        "--db",
        str(database),
        "--contact-db",
        str(contacts),
        "--base-url",
        os.environ.get("CHATLOG_BASE_URL", "http://host.docker.internal:5030"),
        "--cache-dir",
        str(cache),
        "--workers",
        "1",
        "--daily-cap",
        os.environ.get("WECHAT_PREFETCH_LIMIT", "1000000"),
        "--min-wait",
        "0.5",
        "--max-wait",
        "2.0",
        "--import-queue",
        os.environ.get(
            "WECHAT_MEDIA_IMPORT_QUEUE",
            str(cache / "media-import-queue.jsonl"),
        ),
    ]
    moments_since = os.environ.get("WECHAT_MOMENTS_SINCE", "").strip()
    if moments_since:
        command.extend(["--moments-since-timestamp", moments_since])
    started = time.monotonic()
    # Network requests must not own the archive/import lock. Cache publication
    # is atomic in Chatlog; the producer protects only its short metadata writes.
    subprocess.run(command, check=True, timeout=1800)
    return {"state": "complete", "seconds": round(time.monotonic() - started, 2)}


def valid_media_tids(tids) -> bool:
    # SnsTimeLine.tid is a signed SQLite int64, while the XML post ID is
    # unsigned. These are distinct identifiers; accept the source's real key.
    return isinstance(tids, list) and all(
        tid is None or (not isinstance(tid, bool) and isinstance(tid, (int, str))
                        and re.fullmatch(r"-?[0-9]+", str(tid)) is not None
                        and -(2**63) <= int(tid) < 2**63)
        for tid in tids
    )


@contextmanager
def media_queue_writer(queue: Path):
    queue.parent.mkdir(parents=True, exist_ok=True)
    with queue.with_name(queue.name + ".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def recover_signed_media_queue(snapshots: Path, queue: Path) -> dict[str, object]:
    """Replay only records rejected by the old unsigned-ID validator.

    Append+fsync precedes the recovery marker. A crash in between may replay
    twice; normal media linking is idempotent and the original quarantine stays
    available for audit. Never rewind the consumer's committed byte offset.
    """
    recovered = 0
    quarantine = snapshots / "media-queue-quarantine"
    pending = []

    def flush():
        nonlocal recovered
        if not pending:
            return
        queue.parent.mkdir(parents=True, exist_ok=True)
        with media_queue_writer(queue), queue.open("ab") as output:
            for _, raw in pending:
                output.write(raw.rstrip(b"\n") + b"\n")
            output.flush()
            os.fsync(output.fileno())
        for path, _ in pending:
            path.rename(path.with_suffix(".replayed"))
        directory = os.open(quarantine, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        recovered += len(pending)
        pending.clear()

    for path in sorted(quarantine.glob("*.json")):
        if path.stat().st_size > 2 * 1024 * 1024:
            continue
        try:
            saved = json.loads(path.read_text())
            if saved.get("reason") != "Invalid message IDs":
                continue
            raw = base64.b64decode(saved["raw_base64"], validate=True)
            record = json.loads(raw)
            tids = record.get("tids", [record.get("tid")])
            if (record.get("version") != 1 or not record.get("cache_name")
                    or record.get("kind") not in {"photo", "video", "live_photo"}
                    or not valid_media_tids(tids) or not any(tid is not None and int(tid) < 0 for tid in tids)):
                continue
        except (ValueError, TypeError, KeyError, AttributeError):
            continue
        pending.append((path, raw))
        if len(pending) >= 25:
            flush()
    flush()
    return {"recovered_records": recovered}


def read_media_import_queue(
    queue_path: Path,
    state_path: Path,
) -> tuple[list[dict[str, object]], int]:
    if not queue_path.is_file():
        return [], 0
    state = json.loads(state_path.read_text()) if state_path.is_file() else {}
    offset = int(state.get("offset", 0))
    if queue_path.stat().st_size < offset:
        raise ValueError("WeChat media import queue was truncated before its checkpoint")
    records: dict[str, dict[str, object]] = {}
    with queue_path.open("rb") as queue:
        queue.seek(offset)
        for _ in range(500):
            start = queue.tell()
            line = queue.readline(1024 * 1024 + 1)
            if not line:
                break
            if len(line) > 1024 * 1024:
                quarantine = state_path.parent / "media-queue-quarantine"
                quarantine.mkdir(parents=True, exist_ok=True)
                target = quarantine / f"{start}.bin"
                temporary = target.with_suffix(".tmp")
                with temporary.open("wb") as output:
                    output.write(line)
                    while not line.endswith(b"\n"):
                        line = queue.readline(256 * 1024)
                        if not line:
                            break
                        output.write(line)
                    output.flush()
                    os.fsync(output.fileno())
                if not line.endswith(b"\n"):
                    temporary.unlink()
                    queue.seek(start)
                    break
                temporary.replace(target)
                metadata = quarantine / f"{start}.json"
                metadata.write_text(json.dumps({"offset": start, "reason": "Line exceeds 1 MiB", "raw_file": target.name}))
                with metadata.open("rb") as saved:
                    os.fsync(saved.fileno())
                directory = os.open(quarantine, os.O_RDONLY)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
                continue
            if not line.endswith(b"\n"):
                queue.seek(start)
                break
            try:
                record = json.loads(line)
                if not isinstance(record, dict) or record.get("version") != 1 or not record.get("cache_name"):
                    raise ValueError("Unexpected media queue record")
                tids = record.get("tids", [record.get("tid")])
                if not valid_media_tids(tids):
                    raise ValueError("Invalid message IDs")
                if record.get("kind") not in {"photo", "video", "live_photo", "avatar_big", "avatar_small", "moments_cover"}:
                    raise ValueError("Unsupported media kind")
            except (ValueError, TypeError) as exc:
                quarantine = state_path.parent / "media-queue-quarantine"
                quarantine.mkdir(parents=True, exist_ok=True)
                target = quarantine / f"{start}.json"
                temporary = target.with_suffix(".tmp")
                with temporary.open("w") as output:
                    json.dump({"offset": start, "reason": str(exc),
                               "raw_base64": base64.b64encode(line).decode()}, output)
                    output.flush()
                    os.fsync(output.fileno())
                temporary.replace(target)
                directory = os.open(quarantine, os.O_RDONLY)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
                continue
            cache_name = str(record["cache_name"])
            previous = records.get(cache_name)
            if previous is not None:
                tids = {
                    int(tid)
                    for source in (previous, record)
                    for tid in source.get("tids", [source.get("tid")])
                    if tid
                }
                previous.update(record)
                previous["tids"] = sorted(tids)
            else:
                records[cache_name] = record
        next_offset = queue.tell()
    return list(records.values()), next_offset


def write_media_import_checkpoint(path: Path, offset: int) -> None:
    temporary = path.with_suffix(".tmp")
    with temporary.open("w") as output:
        json.dump({"version": 1, "offset": offset}, output)
        output.flush()
        os.fsync(output.fileno())
    temporary.replace(path)


def consume_media_import_queue(
    snapshots: Path,
    cache: Path,
    media: Path,
) -> dict[str, object]:
    if os.environ.get('MONICA_ARCHIVE_SEPARATED') == '1':
        from .media_dependencies import consume
        return consume(snapshots, cache, media, SessionLocal)
    queue_path = Path(
        os.environ.get(
            "WECHAT_MEDIA_IMPORT_QUEUE",
            str(cache / "media-import-queue.jsonl"),
        )
    )
    state_path = snapshots / "media-import-queue-state.json"
    records, next_offset = read_media_import_queue(queue_path, state_path)
    if not records:
        write_media_import_checkpoint(state_path, next_offset)
        return {"records": 0, "state": "pending" if queue_path.exists() and queue_path.stat().st_size > next_offset else "unchanged", "offset": next_offset}

    moment_records = [
        record for record in records if record.get("kind") in {"photo", "video", "live_photo"}
    ]
    contact_records = [
        record
        for record in records
        if record.get("kind") in {"avatar_big", "avatar_small", "moments_cover"}
    ]
    if len(moment_records) + len(contact_records) != len(records):
        raise ValueError("Unsupported media kind entered the incremental import queue")

    report: dict[str, object] = {"records": len(records)}
    unmatched: list[str] = []
    with SessionLocal() as db:
        if moment_records:
            moment_snapshot = snapshots / "moments/sns.db"
            if not moment_snapshot.is_file():
                raise FileNotFoundError("Moments snapshot is missing for media queue import")
            moment_names = {str(record["cache_name"]) for record in moment_records}
            moment_tids = {
                int(tid)
                for record in moment_records
                for tid in record.get("tids", [record.get("tid")])
                if tid
            }
            result = moments_cache(
                db,
                moment_snapshot,
                cache,
                media,
                tids=moment_tids,
                cache_names=moment_names,
            )
            report["moments"] = result
            unmatched.extend(result["unmatched_queue_files"])
        if contact_records:
            contact_snapshot = snapshots / "contacts/contact.db"
            if not contact_snapshot.is_file():
                raise FileNotFoundError("Contact snapshot is missing for media queue import")
            role_by_kind = {
                "avatar_big": "wechat_avatar_big",
                "avatar_small": "wechat_avatar_small",
                "moments_cover": "cover",
            }
            contact_names = {str(record["cache_name"]) for record in contact_records}
            result = sync_contact_media(
                db,
                contact_snapshot,
                cache,
                media,
                roles={role_by_kind[str(record["kind"])] for record in contact_records},
                usernames={str(record["username"]) for record in contact_records},
                cache_names=contact_names,
            )
            report["contacts"] = result
            unmatched.extend(result["unmatched_queue_files"])
    # A file that matches no source record will not match on replay either.
    # Set it aside for review instead of failing the batch, which previously
    # kept the checkpoint from advancing and replayed the whole queue forever.
    report["unmatched"] = write_unmatched_media(snapshots, records, unmatched)
    write_media_import_checkpoint(state_path, next_offset)
    report["state"] = "complete"
    report["offset"] = next_offset
    report["lag_bytes"] = max(0, queue_path.stat().st_size - next_offset)
    if report["lag_bytes"]:
        report["state"] = "pending"
    return report


def write_unmatched_media(snapshots: Path, records: list[dict[str, object]], unmatched: list[str]) -> int:
    if not unmatched:
        return 0
    missing = set(unmatched)
    stamp = time.time()
    with (snapshots / "media-import-unmatched.jsonl").open("a") as review:
        for record in records:
            if record["cache_name"] in missing:
                review.write(json.dumps({**record, "reason": "no matching source record",
                                         "recorded_at": stamp}, ensure_ascii=False) + "\n")
    return len(missing)


def changed_files(paths: list[Path], previous, destination: Path, *, force=False) -> list[Path]:
    """Source files whose own or WAL signature moved, or whose snapshot is missing."""
    before = {entry[0]: list(entry[1:]) for entry in previous or []}
    now = {entry[0]: list(entry[1:]) for entry in signature(paths + [Path(str(p) + "-wal") for p in paths])}
    return [
        path for path in paths
        if path.is_file() and (
            force or before.get(str(path)) != now.get(str(path))
            or before.get(str(path) + "-wal") != now.get(str(path) + "-wal")
            or not (destination / path.name).is_file()
        )
    ]


def snapshot_sqlite(paths: list[Path], destination: Path) -> None:
    from .sqlite_snapshot import snapshot
    for path in paths:
        snapshot(path, destination)


def reconcile_moments(db, snapshot: Path, fingerprints_path: Path) -> dict:
    from .jobs import ShutdownRequested
    from .runtime_status import STOP

    current = moment_fingerprints(db, snapshot)
    previous = json.loads(fingerprints_path.read_text()) if fingerprints_path.is_file() else None
    tids = None if previous is None else changed_moments(previous, current)
    report = moments(db, snapshot, apply=True, tids=tids)
    # The live SNS source can advance while prefetch is downloading. Its queue
    # may then be consumed against the previous snapshot and set aside. Cached
    # files will not be downloaded/published again, so close that race when the
    # new posts are projected, before acknowledging their fingerprints. Look up
    # only these posts' exact cache keys; never rescan the entire media cache.
    media_tids = {int(tid) for tid in (current if tids is None else tids)}
    if media_tids and os.environ.get('MONICA_ARCHIVE_SEPARATED') != '1':
        report["cached_media"] = moments_cache(
            db, snapshot,
            Path(os.environ.get("SNS_CACHE_ROOT", "/data/sns-cache")),
            Path(os.environ.get("MEDIA_ROOT", "/data/media")),
            tids=media_tids, stop_requested=STOP.is_set,
        )
        if report["cached_media"].get("stopped_at_checkpoint"):
            raise ShutdownRequested("Stopped while linking cached Moments media")
    temporary = fingerprints_path.with_suffix(".tmp")
    temporary.write_text(json.dumps(current))
    temporary.replace(fingerprints_path)
    return report


def refresh_projections(db) -> dict:
    from . import location_index, metric_index
    from .contact_stats import refresh as refresh_contact_stats

    return {
        "dashboard_metadata": metric_index.rebuild(db),
        "location_metadata": location_index.rebuild(db),
        "contact_stats": refresh_contact_stats(db),
    }


def write_state(state_file: Path, state: dict) -> None:
    state_file.parent.mkdir(parents=True, exist_ok=True)
    if state_file.is_file() and json.loads(state_file.read_text()) == json.loads(json.dumps(state)):
        return
    temporary = state_file.with_suffix(".tmp")
    temporary.write_text(json.dumps(state))
    temporary.replace(state_file)


def cycle(*, kinds=None, phase_names=None, state_name='state.json'):
    from .runtime_status import STOP, phase, record
    source = Path(os.environ.get("CHATLOG_DB_ROOT", "/data/chatlog"))
    staging = Path(os.environ.get("STAGING_ROOT", "/data/staging"))
    media = Path(os.environ.get("MEDIA_ROOT", "/data/media"))
    cache = Path(os.environ.get("SNS_CACHE_ROOT", "/data/sns-cache"))
    instagram_root = staging / "instagram-current"
    snapshots = staging / "continuous-snapshots"
    snapshots.mkdir(parents=True, exist_ok=True)
    state_file = snapshots / state_name
    state = json.loads(state_file.read_text()) if state_file.exists() else {}
    next_state = dict(state)
    sources = [
        ("contacts", [source / "contact/contact.db"]),
        ("moments", [source / "sns/sns.db"]),
        ("messages", sorted((source / "message").glob("message_[0-9]*.db"))),
    ]
    failures = []
    for kind, paths in sources:
        if kinds is not None and kind not in kinds:
            continue
        if STOP.is_set():
            break
        destination = snapshots / kind
        # Back up only files that changed: a WeChat update usually touches one
        # of nine message databases, not all 3.5 GB of sources.
        # Repair old completed-but-skipped contact batches once, even when the
        # source files have not changed since the bad deduplication checkpoint.
        repair_contacts = kind == "contacts" and state.get("contacts_import_version") != CONTACT_IMPORT_VERSION
        link_version = None
        if kind == 'moments' and os.environ.get('MONICA_ARCHIVE_SEPARATED') == '1':
            with engine.connect() as lookup:
                # Sequence-backed cache versions are visible before commit.
                # A committed identity digest prevents acknowledging a future
                # version while still observing the old account mapping.
                link_version = lookup.execute(text("""SELECT coalesce(md5(string_agg(
                    i.external_id||':'||i.profile_id::text,',' ORDER BY i.external_id)),'')
                    FROM identities i JOIN profiles p ON p.id=i.profile_id WHERE i.provider='wechat'
                    AND i.status NOT IN ('invalid','merged') AND p.archived_at IS NULL AND p.profile_type='person'""")).scalar_one()
        changed = changed_files(paths, next_state.get(kind), destination,
            force=repair_contacts or (link_version is not None and next_state.get('moments_links_version') != link_version))
        if not changed:
            record("archives", kind, "unchanged")
            continue
        try:
            with phase("archives", kind) as details:
                stamp = import_signature(kind, paths, cache)
                snapshot_sqlite(changed, destination)
                with SessionLocal() as db:
                    if kind == "contacts":
                        report = (sync_contacts(db, destination / "contact.db", batch_size=25, work_budget_seconds=15)
                                  if os.environ.get('MONICA_ARCHIVE_SEPARATED') == '1'
                                  else sync_contacts(db, destination / "contact.db"))
                    elif kind == "moments":
                        report = reconcile_moments(db, destination / "sns.db", snapshots / "moments-fingerprints.json")
                    else:
                        with engine.connect() as lease:
                            if not lease.execute(
                                text("SELECT pg_try_advisory_lock(11803292)")
                            ).scalar_one():
                                failures.append(kind)
                                details.update(state="busy")
                                continue
                            lease.commit()
                            try:
                                report = ingest(db, destination, files={path.name for path in changed}, stop_requested=STOP.is_set)
                            finally:
                                lease.execute(text("SELECT pg_advisory_unlock(11803292)"))
                                lease.commit()
                print(json.dumps({"source": kind, "changed_files": [p.name for p in changed], **report}), flush=True)
                if report.get("stopped_at_checkpoint"):
                    details.update(report, state="pending")
                    failures.append(kind)
                    continue
                if report.get("pending_files"):
                    pending = {str(path) for path in paths if path.name in report["pending_files"]}
                    stamp = [entry for entry in stamp if entry[0] not in pending
                             and entry[0].removesuffix("-wal") not in pending]
                details.update(report)
                details["state"] = "pending" if report.get("pending_files") else "success"
                if report.get("pending_files"):
                    failures.append(kind + "_pending")
                next_state[kind] = stamp
                if link_version is not None:
                    next_state['moments_links_version'] = link_version
                if kind == "contacts":
                    next_state["contacts_import_version"] = CONTACT_IMPORT_VERSION
                    # Resolve authors before the Moments phase in this pass,
                    # including unchanged SNS files after contact repairs.
                    next_state.pop("moments", None)
                write_state(state_file, next_state)
        except Exception as exc:  # noqa: BLE001 - a source failure must not block other sources/media
            failures.append(kind)
            print(json.dumps({"source": kind, "state": "failed",
                              "error": type(exc).__name__, "detail": str(exc)[:500]}), flush=True)
    write_state(state_file, next_state)

    def import_instagram():
        published_root = (instagram_root / "CURRENT").resolve() if (instagram_root / "CURRENT").is_symlink() else instagram_root
        stamp = signature([published_root / ".crm-ready"])
        if not stamp or state.get("instagram") == [list(x) for x in stamp]:
            return {"state": "unchanged"}
        with SessionLocal() as db:
            # Register exact account IDs before posts on a fresh desktop so
            # profiles without posts are visible and every post has an owner.
            from .config import get_settings
            desktop_profiles = (instagram_profiles(db, published_root / "profiles", apply=True)
                                if get_settings().app_env == "desktop" else None)
            report = instagram(db, published_root / "profiles", apply=True)
            report["profiles"] = desktop_profiles if desktop_profiles is not None else instagram_profiles(db, published_root / "profiles", apply=True)
            if os.environ.get('MONICA_ARCHIVE_SEPARATED') != '1':
                report["media"] = instagram_archive(db, published_root / "profiles", media)
        next_state["instagram"] = stamp
        write_state(state_file, next_state)
        return report

    def projections():
        with SessionLocal() as db:
            return refresh_projections(db)

    queue_path = Path(os.environ.get("WECHAT_MEDIA_IMPORT_QUEUE", str(cache / "media-import-queue.jsonl")))
    from .cache_warmup import warm
    phases = {
        "wechat_media_recovery": lambda: recover_signed_media_queue(snapshots, queue_path),
        "wechat_media_ready": lambda: consume_media_import_queue(snapshots, cache, media),
        "instagram": import_instagram,
        "projections": projections,
        "cache_warmup": lambda: {"seconds": warm(SessionLocal)},
    }
    for name, action in phases.items():
        if phase_names is not None and name not in phase_names:
            continue
        if STOP.is_set():
            break
        try:
            with phase("archives", name) as details:
                details.update(action())
                if details.get("state") in ("busy", "pending"):
                    failures.append(name)
                print(json.dumps({"source": name, **details}), flush=True)
        except Exception as exc:  # noqa: BLE001 - stages recover independently
            failures.append(name)
            print(json.dumps({"source": name, "state": "failed", "error": type(exc).__name__,
                              "detail": str(exc)[:500]}), flush=True)
    return failures


def download_wechat_media():
    """Run without an archive lease or a long-lived database connection."""
    from .runtime_status import phase

    source = Path(os.environ.get("CHATLOG_DB_ROOT", "/data/chatlog"))
    cache = Path(os.environ.get("SNS_CACHE_ROOT", "/data/sns-cache"))
    snapshots = Path(os.environ.get("STAGING_ROOT", "/data/staging")) / "continuous-snapshots"
    snapshots.mkdir(parents=True, exist_ok=True)
    # Keep download scheduling separate from the importer's locked state file.
    state_path = snapshots / "wechat-prefetch-state.json"
    old_state = snapshots / "state.json"
    previous = json.loads(state_path.read_text()) if state_path.is_file() else (
        json.loads(old_state.read_text()).get("wechat_prefetch", {}) if old_state.is_file() else {})
    stamp = import_signature("wechat_media", [source / "sns/sns.db", source / "contact/contact.db",
                           Path(os.environ.get("WECHAT_PREFETCH_SCRIPT", ""))], cache)
    with phase("archives", "wechat_prefetch") as details:
        if (previous.get("signature") == [list(item) for item in stamp]
                and time.time() - previous.get("at", 0) < 3600):
            details.update(state="unchanged")
            return
        report = prefetch_wechat(source, cache)
        details.update(report)
        if report.get("state") == "complete":
            # Unique temporary names allow overlapping producers to publish
            # complete scheduling observations without sharing a download lock.
            import tempfile
            with tempfile.NamedTemporaryFile(mode="w", dir=snapshots, delete=False) as output:
                temporary = Path(output.name)
                try:
                    json.dump({"signature": stamp, "at": time.time()}, output)
                    output.flush()
                    os.fsync(output.fileno())
                    temporary.replace(state_path)
                finally:
                    temporary.unlink(missing_ok=True)


def run_locked_archive(action, lock_key=11803360):
    """Fence source/link checkpoints, and release the connection before I/O."""
    with engine.connect() as lease:
        locked = lease.execute(text("SELECT pg_try_advisory_lock(:key)"), {'key': lock_key}).scalar_one()
        lease.commit()
        if not locked:
            return ["busy"]
        try:
            return action()
        finally:
            lease.execute(text("SELECT pg_advisory_unlock(:key)"), {'key': lock_key})
            lease.commit()


def link_downloaded_media():
    from .runtime_status import phase

    snapshots = Path(os.environ.get("STAGING_ROOT", "/data/staging")) / "continuous-snapshots"
    cache = Path(os.environ.get("SNS_CACHE_ROOT", "/data/sns-cache"))
    media = Path(os.environ.get("MEDIA_ROOT", "/data/media"))
    with phase("archives", "wechat_media_incremental") as details:
        details.update(consume_media_import_queue(snapshots, cache, media))
        return ["wechat_media_incremental"] if details.get("state") == "pending" else []


def iteration():
    """A failed or busy importer must not prevent independent downloads."""
    from .runtime_status import STOP

    failures = []
    for name, action in (
        ("cycle", lambda: run_locked_archive(cycle)),
        ("wechat_prefetch", download_wechat_media),
        ("wechat_media_incremental", lambda: run_locked_archive(link_downloaded_media)),
    ):
        if STOP.is_set():
            break
        try:
            failures.extend(action() or [])
        except Exception as exc:  # noqa: BLE001 - source failures are isolated
            print(json.dumps({"source": name, "error": type(exc).__name__,
                              "detail": str(exc)[:500]}), flush=True)
            failures.append(name)
    return failures


LANES = ('contacts', 'moments', 'messages', 'media', 'download', 'instagram', 'instagram-media', 'maintenance')


def lane_iteration(lane):
    if lane == 'download':
        download_wechat_media()
        return []
    def action():
        if lane in ('contacts', 'moments', 'messages'):
            return cycle(kinds={lane}, phase_names=set(), state_name='state-' + lane + '.json')
        if lane == 'media':
            return cycle(kinds=set(), phase_names={'wechat_media_recovery', 'wechat_media_ready'}, state_name='state-media.json')
        if lane == 'instagram':
            return cycle(kinds=set(), phase_names={'instagram'}, state_name='state-instagram.json')
        if lane == 'instagram-media':
            from .config import get_settings
            root = Path(os.environ['STAGING_ROOT']) / 'instagram-current'
            published = (root / 'CURRENT').resolve() if (root / 'CURRENT').is_symlink() else root
            if not (published / 'profiles').is_dir():
                return []
            checkpoint = Path(os.environ['STAGING_ROOT']) / 'continuous-snapshots/instagram-media-state.json'
            with SessionLocal() as db:
                if get_settings().async_projection_enabled and db.execute(text("SELECT 1 FROM projection_inbox WHERE provider='instagram' AND projected_at IS NULL LIMIT 1")).first():
                    return ['projection_pending']
                projected = db.execute(text("SELECT coalesce(max(id),0) FROM projection_inbox WHERE provider='instagram' AND projected_at IS NOT NULL")).scalar_one() if get_settings().async_projection_enabled else 0
                stamp = {'ready': signature([published / '.crm-ready']), 'projected': projected}
                if checkpoint.is_file() and json.loads(checkpoint.read_text()) == json.loads(json.dumps(stamp)):
                    return []
                instagram_archive(db, published / 'profiles', get_settings().media_root)
            write_state(checkpoint, stamp)
            return []
        return cycle(kinds=set(), phase_names={'projections', 'cache_warmup'}, state_name='state-maintenance.json')
    return run_locked_archive(action, 11803420 + LANES.index(lane))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--lane', choices=(*LANES, 'all'), default='all')
    args = parser.parse_args()
    if args.lane != 'all':
        os.environ['MONICA_ARCHIVE_SEPARATED'] = '1'
    interval = max(int(os.environ.get("ARCHIVE_SYNC_SECONDS", "300")), 60)
    lower_priority()
    from .runtime_status import STOP, IdleWait, install_shutdown
    install_shutdown()
    idle = IdleWait()
    while not STOP.is_set():
        try:
            failures = iteration() if args.lane == 'all' else lane_iteration(args.lane)
        except Exception as exc:  # noqa: BLE001 - retain the last good watermark and retry later
            print(json.dumps({"archive_sync_error": type(exc).__name__, "detail": str(exc)[:500]}), flush=True)
            failures = ["cycle"]
        retry_soon = any(name != "messages_pending" for name in failures)
        delay = 3 if args.lane == 'media' else 15 if args.lane in ('moments', 'instagram-media') else min(interval, 60) if retry_soon else interval
        idle.wait(delay)


if __name__ == "__main__":
    main()
