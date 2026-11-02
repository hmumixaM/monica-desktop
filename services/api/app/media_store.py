"""The one content-addressed media store: bytes on disk by SHA-256, one asset row each."""

import hashlib
import os
import tempfile
from pathlib import Path

from sqlalchemy import text

from .content import media_object_path


class WriteContended(RuntimeError):
    sqlstate = '55P03'


def lock_media_slot(db, entity_type, entity_id, role, url=None):
    """Only mutable slot/URL changes need exclusion; never wait in a cycle."""
    keys = ['media-slot:' + str(entity_type) + ':' + str(entity_id) + ':' + str(role)]
    if url:
        keys.append('media-url:' + url)
    for key in sorted(keys):
        if not db.execute(text('SELECT pg_try_advisory_xact_lock(hashtextextended(:key,0))'), {'key': key}).scalar_one():
            raise WriteContended('Media slot is being updated; retry this transaction')


def record_asset(db, *, sha256: str, path, byte_length: int, media_type: str, source_type=None,
                 source_record_id=None, width=None, height=None):
    """Register a stored file; replays of an unchanged file write nothing.

    Unchanged rows are not rewritten, so idempotent imports do not advance the
    media data version and invalidate cached aggregates.
    """
    existing = db.execute(text('SELECT id,object_path FROM media_assets WHERE sha256=:sha'), {'sha': sha256}).one_or_none()
    if existing and existing.object_path == str(path):
        return existing.id
    params = {"sha": sha256, "size": byte_length, "path": str(path), "mime": media_type,
              "source": source_type, "record": source_record_id, "width": width, "height": height}
    content_id = db.execute(text("""INSERT INTO content_objects(sha256, content_kind, byte_length, storage_uri)
        VALUES (:sha, 'media', :size, :path)
        ON CONFLICT (content_kind, sha256) DO UPDATE SET storage_uri = EXCLUDED.storage_uri
        WHERE content_objects.storage_uri IS DISTINCT FROM EXCLUDED.storage_uri
        RETURNING id"""), params).scalar_one_or_none() or db.execute(
        text("SELECT id FROM content_objects WHERE content_kind = 'media' AND sha256 = :sha"), params).scalar_one()
    return db.execute(text("""INSERT INTO media_assets(content_object_id, sha256, media_type, byte_length, object_path,
          source_type, source_record_id, width, height)
        VALUES (:content, :sha, :mime, :size, :path, :source, :record, :width, :height)
        ON CONFLICT (sha256) DO UPDATE SET object_path = EXCLUDED.object_path
        WHERE media_assets.object_path IS DISTINCT FROM EXCLUDED.object_path
        RETURNING id"""), {**params, "content": content_id}).scalar_one_or_none() or db.execute(
        text("SELECT id FROM media_assets WHERE sha256 = :sha"), params).scalar_one()


def place_file(temporary: Path, sha256: str, media_root: Path) -> Path:
    """Move a fully written temporary file to its content address (or discard a duplicate)."""
    target = media_object_path(media_root, sha256)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        temporary.unlink(missing_ok=True)
    else:
        os.replace(temporary, target)
    return target


def store_bytes(db, payload: bytes, media_root: Path, *, media_type: str, source_type=None, source_record_id=None):
    sha = hashlib.sha256(payload).hexdigest()
    media_root.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=".store-", dir=media_root)
    with os.fdopen(descriptor, "wb") as output:
        output.write(payload)
    target = place_file(Path(name), sha, media_root)
    return record_asset(db, sha256=sha, path=target, byte_length=len(payload), media_type=media_type,
                        source_type=source_type, source_record_id=source_record_id)
