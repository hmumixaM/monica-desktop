"""Import a collector export when its contents change, independently of WeChat."""

import argparse
import hashlib
import json
import time
from pathlib import Path

from sqlalchemy import text

from .config import get_settings
from .db import SessionLocal
from .jobs import lower_priority
from .linkedin_import import load_linkedin, materialize_remote_media

FAILURE_RETRY_SECONDS = 60


def file_signature(path: Path):
    current = path.parent / "CURRENT"
    if current.is_symlink():
        path = current.resolve() / path.name
    stat = path.stat()
    return stat.st_size, stat.st_mtime_ns


def sync(path: Path, *, media_only=False):
    current = path.parent / "CURRENT"
    if current.is_symlink():
        path = current.resolve() / path.name
    if not path.is_file():
        return "unchanged"
    with path.open() as source:
        snapshot = json.load(source)
    hasher = hashlib.sha256()
    for chunk in json.JSONEncoder(sort_keys=True, ensure_ascii=False).iterencode(
        {"version": 2, "profiles": snapshot.get("profiles", []), "assets": snapshot.get("local_assets", [])}
    ):
        hasher.update(chunk.encode())
    digest = hasher.hexdigest()
    with SessionLocal() as db, db.get_bind().engine.connect() as lease:
        # One importer per file, including manual invocations alongside the watcher.
        if not lease.execute(text("SELECT pg_try_advisory_lock(11803291)")).scalar_one():
            return "busy"
        lease.commit()
        try:
            if media_only:
                from .archive_linkedin import archive
                from .runtime_status import STOP
                result = archive(db, snapshot, path.parent, get_settings().media_root, stop_requested=STOP.is_set)
                print(json.dumps({"local_media": result}), flush=True)
                if result['missing_files'] or snapshot.get('local_asset_missing_files'):
                    raise ValueError('LinkedIn local assets incomplete')
                return "success"
            previous = db.execute(text("""
                SELECT cursor_value FROM source_cursors
                WHERE source = 'linkedin' AND stream = 'collector_export'
            """)).scalar_one_or_none()
            if previous == digest:
                return "unchanged"
            # End the read transaction before a possibly long wait for the
            # lock; the advisory lock is session-level and stays held.
            db.commit()
            if get_settings().async_projection_enabled:
                if db.execute(text("SELECT 1 FROM projection_inbox WHERE provider='linkedin' AND projected_at IS NULL LIMIT 1")).first():
                    return 'pending'
                from .platform_store import platform_engine
                with platform_engine('linkedin').connect() as source:
                    accepted = source.execute(text('SELECT count(*) FROM ingest_batches')).scalar_one()
                captured = db.execute(text("SELECT count(*) FROM projection_inbox WHERE provider='linkedin'")).scalar_one()
                if accepted > captured:
                    return 'pending'
            return import_export(db, path, snapshot, digest) or "success"
        finally:
            db.rollback()
            if not lease.execute(text("SELECT pg_advisory_unlock(11803291)")).scalar_one():
                raise RuntimeError("LinkedIn advisory lock ownership lost")
            lease.commit()


def import_export(db, path, snapshot, digest):
    mapping = {str(row.external_id).removeprefix("contact:"): str(row.profile_id)
               for row in db.execute(text("""
        SELECT external_id, profile_id FROM identities WHERE provider = 'monica'
            AND external_id LIKE 'contact:%' AND profile_id IS NOT NULL
    """))}
    # The collector may replace its file during import. The committed
    # digest must describe exactly the in-memory export being consumed.
    changed = load_linkedin(db, path, mapping, export_snapshot=snapshot, incremental=True)
    if get_settings().async_projection_enabled:
        # Source batches are durable, but their CRM rows may not exist yet.
        # Do not acknowledge the export or lose its local-media join work.
        db.commit()
        pending = db.execute(text("SELECT 1 FROM projection_inbox WHERE provider='linkedin' AND projected_at IS NULL LIMIT 1")).first()
        from .platform_store import platform_engine
        with platform_engine('linkedin').connect() as source:
            accepted = source.execute(text('SELECT count(*) FROM ingest_batches')).scalar_one()
        captured = db.execute(text("SELECT count(*) FROM projection_inbox WHERE provider='linkedin'")).scalar_one()
        if pending or captured < accepted:
            return 'pending'
    from .archive_linkedin import archive
    from .runtime_status import STOP
    media_result = archive(db, snapshot, path.parent, get_settings().media_root, stop_requested=STOP.is_set)
    if media_result['missing_files'] or snapshot.get('local_asset_missing_files'):
        raise ValueError('LinkedIn local assets incomplete; cursor not advanced')
    queued = materialize_remote_media(db, get_settings().media_root, account_external_ids=changed)
    db.execute(text("""
        INSERT INTO source_cursors(source, stream, cursor_value, batch_id)
        SELECT 'linkedin', 'collector_export', :digest, id FROM import_batches
        WHERE source = 'linkedin' AND status = 'complete'
        ORDER BY completed_at DESC LIMIT 1
        ON CONFLICT(source, stream) DO UPDATE SET
            cursor_value = EXCLUDED.cursor_value, updated_at = now()
    """), {"digest": digest})
    db.commit()
    print(json.dumps({"linkedin_export": digest, "media_queued": queued, "local_media": media_result}), flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--watch", action="store_true")
    parser.add_argument("--media-only", action="store_true", help="Archive already imported account/post media without advancing metadata cursors")
    args = parser.parse_args()
    if args.media_only and args.watch:
        parser.error("--media-only is a one-time archive operation")
    lower_priority()
    from .runtime_status import STOP, IdleWait, install_shutdown, phase
    install_shutdown()
    idle = IdleWait()
    # Parsing and hashing a large unchanged export every minute keeps the NAS
    # disks busy; a completed pass is skipped until the file itself changes.
    checked = None
    failed = None
    failures = 0
    while not STOP.is_set():
        try:
            signature = file_signature(args.source) if args.source.is_file() or (args.source.parent / "CURRENT").is_symlink() else None
            # A failed export (e.g. missing collector assets) is retried when the
            # file changes or after a long delay, not re-imported every minute.
            retry_failed = failed is None or failed[0] != signature or time.monotonic() - failed[1] > min(3600, FAILURE_RETRY_SECONDS * 2 ** min(failures, 6))
            if signature is not None and signature != checked and retry_failed:
                with phase('linkedin', 'collector_export') as details:
                    outcome = sync(args.source, media_only=args.media_only)
                    details['state'] = outcome
                if outcome in ("success", "unchanged"):
                    checked, failed, failures = signature, None, 0
        except Exception as exc:
            if not args.watch:
                raise
            failed = (signature, time.monotonic())
            failures += 1
            print(json.dumps({"linkedin_sync_error": type(exc).__name__, "detail": str(exc)[:300]}), flush=True)
        if not args.watch:
            break
        idle.wait(60)


if __name__ == "__main__":
    main()
