"""Link existing local social images by explicit archive IDs and Chatlog cache keys."""

import argparse
import hashlib
import json
import mimetypes
import os
import shutil
import signal
import sqlite3
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import quote_plus

from sqlalchemy import text

from .ai.image_ocr import enqueue_media
from .ai.queue import enqueue_story_media
from .db import SessionLocal
from .media_store import place_file, record_asset
from .moments_posts import comment_images, moments_interactions
from .moments_xml import parse_moments_xml


def copy_media_snapshot(source, target):
    """Use a private CoW copy on NAS Btrfs; fall back to a normal atomic copy.

    Never hard-link a mutable collector cache into the content-addressed store.
    FICLONE changes remain private when either file is edited or removed.
    """
    fd, temporary = tempfile.mkstemp(prefix=".media-", dir=target.parent)
    try:
        with os.fdopen(fd, "wb") as output, source.open("rb") as input_file:
            cloned = False
            if sys.platform == "linux":
                import fcntl

                try:
                    fcntl.ioctl(output.fileno(), 0x40049409, input_file.fileno())  # Linux FICLONE
                    cloned = True
                except OSError:
                    output.seek(0)
                    output.truncate()
            if not cloned:
                shutil.copyfileobj(input_file, output, 1024 * 1024)
        os.replace(temporary, target)
    finally:
        Path(temporary).unlink(missing_ok=True)


def register_file(db, path, media_root, provider):
    path = Path(path).resolve()
    stamp = path.stat()
    media_root = Path(media_root).resolve()
    cached = (
        db.execute(
            text("""SELECT f.media_id,a.object_path FROM archive_media_files f
        JOIN media_assets a ON a.id=f.media_id WHERE f.provider=:provider AND f.source_path=:path
          AND f.byte_length=:bytes AND f.modified_ns=:modified"""),
            {
                "provider": provider,
                "path": str(path),
                "bytes": stamp.st_size,
                "modified": stamp.st_mtime_ns,
            },
        )
        .mappings()
        .one_or_none()
    )
    if cached and Path(cached["object_path"]).is_file():
        return cached["media_id"]
    return register_prepared_file(db, path, stamp, prepare_file(path, media_root, stamp), provider)


def prepare_file(path, media_root, stamp):
    """Snapshot and hash collector bytes before opening a write transaction."""
    media_root.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".archive-", dir=media_root)
    os.close(fd)
    snapshot = Path(temporary)
    try:
        # Hash the private snapshot, not a file the collector can overwrite
        # between hashing and copying. CoW avoids another physical NAS copy.
        copy_media_snapshot(path, snapshot)
        current = path.stat()
        if (stamp.st_size, stamp.st_mtime_ns) != (current.st_size, current.st_mtime_ns):
            raise OSError("Collector media changed during snapshot; retry the archive scan")
        digest = hashlib.sha256()
        with snapshot.open("rb") as stream:
            head = stream.read(32)
            digest.update(head)
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
        mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        if head.startswith(b"\xff\xd8\xff"):
            mime = "image/jpeg"
        elif head.startswith(b"\x89PNG"):
            mime = "image/png"
        elif head.startswith(b"GIF8"):
            mime = "image/gif"
        elif head[8:12] == b"WEBP":
            mime = "image/webp"
        elif head[4:8] == b"ftyp":
            mime = "video/mp4"
        if not mime.startswith(("image/", "video/")):
            return None
        sha = digest.hexdigest()
        target = place_file(snapshot, sha, media_root)
    finally:
        snapshot.unlink(missing_ok=True)
    return {"sha256": sha, "path": target, "byte_length": stamp.st_size,
            "media_type": mime, "source_record_id": str(path)}


def register_prepared_file(db, path, stamp, prepared, provider):
    if prepared is None:
        return None
    media = record_asset(db, **prepared,
                         source_type=provider)
    db.execute(
        text("""INSERT INTO archive_media_files(provider,source_path,byte_length,modified_ns,media_id)
        VALUES(:provider,:path,:bytes,:modified,:media) ON CONFLICT(provider,source_path) DO UPDATE SET
        byte_length=EXCLUDED.byte_length,modified_ns=EXCLUDED.modified_ns,media_id=EXCLUDED.media_id
        WHERE (archive_media_files.byte_length,archive_media_files.modified_ns,archive_media_files.media_id)
          IS DISTINCT FROM (EXCLUDED.byte_length,EXCLUDED.modified_ns,EXCLUDED.media_id)"""),
        {
            "provider": provider,
            "path": str(path),
            "bytes": stamp.st_size,
            "modified": stamp.st_mtime_ns,
            "media": media,
        },
    )
    return media


def prepare_archive_batch(db, paths, media_root, provider):
    """Release the lookup transaction before slow source/storage filesystem I/O.

    The caller commits each preceding batch before entering this function.
    Cached bytes keep their fast path; new bytes are all staged before writes.
    """
    paths = list(dict.fromkeys(Path(path).resolve() for path in paths))
    cached = {row["source_path"]: row for row in db.execute(text("""
        SELECT f.source_path,f.byte_length,f.modified_ns,f.media_id,a.object_path
        FROM archive_media_files f JOIN media_assets a ON a.id=f.media_id
        WHERE f.provider=:provider AND f.source_path=ANY(:paths)
        """), {"provider": provider, "paths": [str(path) for path in paths]}).mappings()}
    db.commit()
    prepared = {}
    for path in paths:
        stamp = path.stat()
        previous = cached.get(str(path))
        if (previous and previous["byte_length"] == stamp.st_size
                and previous["modified_ns"] == stamp.st_mtime_ns
                and Path(previous["object_path"]).is_file()):
            prepared[path] = (stamp, previous["media_id"], None)
        else:
            prepared[path] = (stamp, None, prepare_file(path, media_root, stamp))
    # New shared assets are registered in one stable hash order. Cached assets
    # need no write locks; pointer changes are guarded separately per slot.
    ordered = sorted(prepared, key=lambda path: ((prepared[path][2] or {}).get('sha256', ''), str(path)))
    for path in ordered:
        stamp, asset, snapshot = prepared[path]
        if asset is None and snapshot is not None:
            asset = register_prepared_file(db, path, stamp, snapshot, provider)
            prepared[path] = (stamp, asset, None)
    return prepared


def fix_url(url, token, video=False):
    url = url.strip()
    if not video:
        url = url.replace("/150?", "/0?")
        if url.endswith("/150"):
            url = url[:-4] + "/0"
    if not token or "token=" in url:
        return url
    if video:
        parts = url.split("?", 1)
        return (
            parts[0]
            + "?token="
            + quote_plus(token)
            + "&idx=1"
            + ("&" + parts[1] if len(parts) > 1 else "")
        )
    return url + ("&" if "?" in url else "?") + "token=" + quote_plus(token) + "&idx=1"


def link(db, media_id, entity_id, role, provider_url=None, entity_type="social_post", retain=True):
    if not media_id:
        return
    previous = (db.execute(text('SELECT media_id FROM remote_media_sources WHERE source_url=:url'),
                           {'url': provider_url}).scalar_one_or_none() if provider_url else None)
    stale = (db.execute(text('''SELECT 1 FROM media_links l JOIN media_source_revisions r ON r.media_id=l.media_id
        WHERE r.source_url=:url AND l.media_id<>:media AND l.entity_type=:type AND l.entity_id=:entity
        AND l.role=:role LIMIT 1'''), {'url': provider_url, 'media': media_id, 'type': entity_type,
                                    'entity': entity_id, 'role': role}).first()
             if provider_url and previous == media_id else None)
    if provider_url and (previous != media_id or stale):
        from .media_store import lock_media_slot
        lock_media_slot(db, entity_type, entity_id, role, provider_url)
        db.execute(
            text("""INSERT INTO remote_media_sources(source_url,media_id) VALUES (:url,:media)
            ON CONFLICT(source_url) DO UPDATE SET media_id=EXCLUDED.media_id
            WHERE remote_media_sources.media_id IS DISTINCT FROM EXCLUDED.media_id"""),
            {"url": provider_url, "media": media_id},
        )
        # Upgrading a cached thumbnail to its original changes this one slot.
        # Prior bytes and the URL's true A→B→A history remain archived.
        db.execute(
            text("""DELETE FROM media_links l USING media_source_revisions r
            WHERE r.source_url=:url AND l.media_id=r.media_id AND l.media_id<>:media
              AND l.entity_type=:type AND l.entity_id=:entity AND l.role=:role"""),
            {
                "url": provider_url,
                "media": media_id,
                "type": entity_type,
                "entity": entity_id,
                "role": role,
            },
        )
    added = db.execute(
        text("""INSERT INTO media_links(media_id,entity_type,entity_id,role)
        VALUES (:media,:type,:entity,:role) ON CONFLICT DO NOTHING"""),
        {"media": media_id, "type": entity_type, "entity": entity_id, "role": role},
    ).rowcount
    if added:
        enqueue_media(db, media_id, entity_type)
    if added and entity_type == "social_story":
        enqueue_story_media(db, entity_id)
    if retain:
        from .remote_media import retain_source_media

        retain_source_media(
            db, {"entity_type": entity_type, "entity_id": entity_id, "role": role}, media_id
        )
    return added


def moments_cache(
    db,
    sqlite_path,
    cache,
    media_root,
    limit=0,
    audit=False,
    stop_requested=None,
    tids=None,
    cache_names=None,
):
    from .remote_media import retain_post_media_batch

    posts = dict(
        db.execute(text("SELECT external_id,id FROM social_posts WHERE provider='wechat'")).all()
    )
    comments = dict(db.execute(text("""SELECT external_id,id FROM social_interactions
        WHERE provider='wechat' AND interaction_type IN ('comment','reply')""")).all())
    db.commit()
    incremental = cache_names is not None
    # Incremental runs receive exact newly downloaded names and never enumerate
    # the full cache. Manual full registration retains the directory inventory.
    available = (
        set(cache_names or ())
        if incremental
        else (
            {p.name for p in cache.iterdir() if p.name.endswith(".bin")}
            if cache.is_dir()
            else set()
        )
    )
    matched_queue = set()
    result = {
        "source_posts": 0,
        "source_media": 0,
        "target_media": 0,
        "excluded_media": 0,
        "cache_directory_available": cache.is_dir(),
        "cache_files": len(available),
        "cache_matches": 0,
        "linked_media": 0,
        "added_links": 0,
        "unavailable_media": 0,
        "excluded_unavailable_media": 0,
        "unsupported_files": 0,
        "unmapped_posts": 0,
        "invalid_xml": 0,
        "recovered_xml": 0,
        "escaped_character_references": 0,
        "incremental": incremental,
        "not_queued_media": 0,
    }
    pending = []

    def commit_media():
        if not pending:
            return
        batch = [(path.resolve(), post, role, original, entity_type, entity)
                 for path, post, role, original, entity_type, entity in pending]
        prepared = prepare_archive_batch(db, [item[0] for item in batch], media_root, "wechat")
        attachments = []
        for path, post, role, original, entity_type, entity in batch:
            stamp, asset, snapshot = prepared[path]
            if asset is None:
                asset = register_prepared_file(db, path, stamp, snapshot, "wechat")
            if asset:
                result["added_links"] += link(db, asset, entity, role, original, entity_type=entity_type, retain=False)
                retained_role = role if entity_type == "social_post" else f"comment-{entity}-{role}"
                attachments.append({"post": str(post), "media": str(asset), "role": retained_role})
                result["linked_media"] += 1
            else:
                result["unsupported_files"] += 1
        if attachments:
            retain_post_media_batch(db, attachments)
        db.commit()
        pending.clear()

    with sqlite3.connect(f"file:{sqlite_path}?mode=ro", uri=True) as source:
        if tids is None:
            rows = source.execute("SELECT tid,content FROM SnsTimeLine")
        else:
            rows = []
            ordered = sorted(set(tids))
            for offset in range(0, len(ordered), 500):
                chunk = ordered[offset : offset + 500]
                placeholders = ",".join("?" for _ in chunk)
                rows.extend(
                    source.execute(
                        f"SELECT tid,content FROM SnsTimeLine WHERE tid IN ({placeholders})",
                        chunk,
                    )
                )
        for index, (tid, xml) in enumerate(rows):
            if stop_requested and stop_requested():
                commit_media()
                db.commit()
                return {**result, "stopped_at_checkpoint": True}
            if limit and index >= limit:
                break
            result["source_posts"] += 1
            try:
                root, repairs = parse_moments_xml(xml)
                result["recovered_xml"] += bool(repairs)
                result["escaped_character_references"] += sum(repairs.values())
            except ET.ParseError:
                result["invalid_xml"] += 1
                continue
            obj = root.find("TimelineObject")
            if obj is None:
                result["invalid_xml"] += 1
                continue
            post = posts.get(obj.findtext("id") or str(tid))
            if not post:
                result["unmapped_posts"] += 1
                continue
            content_type = obj.findtext("ContentObject/type")
            slots = []
            for position, media in enumerate(obj.findall("ContentObject/mediaList/media")):
                slots.append((f"media-{position + 1}", media, content_type in ("6", "15"), "social_post", post))
                live = media.find("LivePhoto")
                if live is not None:
                    nested = live.find("liveMedia")
                    slots.append(
                        (f"live-media-{position + 1}", nested if nested is not None else live, True, "social_post", post)
                    )
            comment_nodes = root.findall("LocalExtraInfo/comment_user_list/user_comment")
            # Resolve by source comment identity, including replies and old rows
            # imported before the image parser existed.
            comment_interactions = [item for item in moments_interactions(root, obj.findtext("id") or str(tid))
                                    if item["source"] == "comment_user_list"]
            for node, interaction in zip(comment_nodes, comment_interactions, strict=True):
                entity = comments.get(interaction["external_id"])
                if entity and not interaction["is_deleted"]:
                    slots.extend((f"media-{n + 1}", image, False, "social_interaction", entity)
                                 for n, image in enumerate(comment_images(node)))
            for role, media, is_video, entity_type, entity in slots:
                result["source_media"] += 1
                target_media = entity_type == "social_interaction" or content_type in ("1", "6", "7", "15", "54")
                result["target_media" if target_media else "excluded_media"] += 1
                url = media.find("url")
                thumb = media.find("thumb")
                attrs = {
                    **(thumb.attrib if thumb is not None else {}),
                    **(url.attrib if url is not None else {}),
                }
                key = attrs.get("key", "")
                token = attrs.get("token", "")
                if not key or key == "0":
                    enc = media.find(".//enc")
                    if enc is None:
                        enc = root.find(".//enc")
                    key = enc.get("key", "") if enc is not None else key
                found = None
                original = media.findtext("url") or media.findtext("thumb")
                for raw, video in [
                    (media.findtext("url"), is_video),
                    (media.findtext("thumb"), False),
                ]:
                    if not raw:
                        continue
                    candidates = [fix_url(raw, token, video), raw]
                    if raw.startswith("http://"):
                        legacy = raw.replace("http://", "https://", 1)
                        candidates.extend([fix_url(legacy, token, video), legacy])
                    for candidate in dict.fromkeys(candidates):
                        cache_keys = ("", key) if key == "0" else (key,)
                        for cache_key in cache_keys:
                            path = cache / (
                                hashlib.md5((candidate + "|" + cache_key).encode()).hexdigest()
                                + ".bin"
                            )
                            if path.name in available:
                                found = path
                                matched_queue.add(path.name)
                                break
                        if found:
                            break
                    if found:
                        break
                if found:
                    result["cache_matches"] += 1
                    if not audit:
                        pending.append((found, post, role, original, entity_type, entity))
                        # Prepare bytes outside transactions, then commit a bounded
                        # batch of assets, links and source-retention pointers.
                        if len(pending) >= 25:
                            commit_media()
                elif incremental:
                    result["not_queued_media"] += 1
                elif target_media:
                    result["unavailable_media"] += 1
                else:
                    result["excluded_unavailable_media"] += 1
            if index % 400 == 0:
                commit_media()
                db.commit()
                print(json.dumps({"posts_checked": index, **result}), flush=True)
    commit_media()
    db.commit()
    result["unmatched_queue_files"] = sorted(available - matched_queue)
    return result


def instagram_file_slots(metadata, node, files=None):
    """Map archive filenames to explicit carousel positions, including sparse downloads."""
    import re

    children = [
        edge.get("node", {}) for edge in node.get("edge_sidecar_to_children", {}).get("edges", [])
    ] or [node]
    for path in sorted(files if files is not None else metadata.parent.glob(metadata.stem + "*")):
        if path.suffix.lower() not in (".jpg", ".jpeg", ".png", ".webp", ".mp4"):
            continue
        suffix = path.stem[len(metadata.stem) :]
        number = re.fullmatch(r"_(\d+)", suffix)
        position = int(number.group(1)) if number else 1 if not suffix and len(children) == 1 else 0
        if not 1 <= position <= len(children):
            yield path, None, None
            continue
        child = children[position - 1]
        video = path.suffix.lower() == ".mp4"
        url = child.get("video_url") if video else child.get("display_url")
        role = f"{'thumbnail' if child.get('is_video') and not video else 'media'}-{position}"
        yield path, role, url


ARCHIVE_COMMIT_POSTS = 50
ARCHIVE_COMMIT_MEDIA = 200


def instagram_archive(db, root, media_root, stop_requested=None):
    from .remote_media import retain_post_media_batch

    posts = dict(
        db.execute(text("SELECT external_id,id FROM social_posts WHERE provider='instagram'")).all()
    )
    from .source_projection import enabled

    source_enabled = enabled(db, "instagram")
    aliases = {}
    if source_enabled:
        from .platform_migrate import account_lookup

        _, aliases, _ = account_lookup(db, "instagram")
    identities = dict(
        db.execute(
            text(
                "SELECT external_id,profile_id FROM identities WHERE provider='instagram' AND status NOT IN ('invalid','merged')"
            )
        ).all()
    )
    result = {
        "posts_with_files": 0,
        "linked_media": 0,
        "added_links": 0,
        "avatars": 0,
        "unmapped_files": 0,
        "unsupported_files": 0,
    }
    batch_posts = batch_media = 0
    for directory in root.iterdir():
        if stop_requested and stop_requested():
            db.commit()
            return {**result, "stopped_at_checkpoint": True}
        if not directory.is_dir():
            continue
        account = aliases.get("username:" + directory.name) or aliases.get(directory.name)
        profile = (
            None
            if source_enabled
            else identities.get("username:" + directory.name) or identities.get(directory.name)
        )
        # A media pass should not reread thousands of JSON-only posts or
        # rescan the same directory once per post. Inventory each directory once.
        entries = list(directory.iterdir())
        metadata = {p.stem: p for p in entries if p.suffix == ".json" and "_UTC_" in p.name}
        by_metadata = {}
        for file in entries:
            if (
                file.suffix.lower() not in (".jpg", ".jpeg", ".png", ".webp", ".mp4")
                or "profile_pic" in file.name
            ):
                continue
            candidates = {file.stem} if file.stem in metadata else set()
            stem, _, number = file.stem.rpartition("_")
            if number.isdigit() and stem in metadata:
                candidates.add(stem)
            if len(candidates) != 1:
                result["unmapped_files"] += 1
                continue
            by_metadata.setdefault(candidates.pop(), []).append(file)
        for stem, available in sorted(by_metadata.items()):
            if stop_requested and stop_requested():
                db.commit()
                return {**result, "stopped_at_checkpoint": True}
            path = metadata[stem]
            try:
                payload = json.loads(path.read_text())
                node = payload.get("node", payload)
            except (ValueError, OSError):
                continue
            shortcode = str(node.get("shortcode") or "")
            post = posts.get(str(node.get("id"))) or posts.get(shortcode)
            if not post:
                continue
            files = list(instagram_file_slots(path, node, available))
            result["posts_with_files"] += bool(files)
            pending = []
            for file, role, url in files:
                if role is None:
                    result["unmapped_files"] += 1
                    continue
                asset = register_file(db, file, media_root, "instagram")
                if not asset:
                    result["unsupported_files"] += 1
                    continue
                result["added_links"] += link(db, asset, post, role, url, retain=False) or 0
                pending.append({"post": str(post), "media": str(asset), "role": role})
                result["linked_media"] += 1
                # Existing URLs now resolve to real local bytes. The normal
                # worker can acknowledge its queued job without another fetch.
                if url:
                    db.execute(
                        text("""UPDATE media_manifest SET state='ready',media_id=:media,
                        error=NULL,checked_at=now() WHERE source_url=:url AND entity_type='social_post'
                        AND entity_id=:post AND role=:role AND (state,media_id) IS DISTINCT FROM ('ready',CAST(:media AS uuid))"""),
                        {"media": asset, "url": url, "post": post, "role": role},
                    )
            # A whole post is the smallest checkpoint unit. Durable commits are
            # batched across posts: one WAL fsync per post saturated the NAS
            # disks during large archive backfills, and replays are idempotent.
            if pending:
                retain_post_media_batch(db, pending)
                batch_media += len(pending)
            batch_posts += 1
            if batch_posts >= ARCHIVE_COMMIT_POSTS or batch_media >= ARCHIVE_COMMIT_MEDIA:
                db.commit()
                batch_posts = batch_media = 0
        if account or profile:
            for file in directory.glob("*profile_pic*"):
                if file.suffix.lower() not in (".jpg", ".jpeg", ".png"):
                    continue
                asset = register_file(db, file, media_root, "instagram")
                link(
                    db,
                    asset,
                    account or profile,
                    "instagram_avatar",
                    entity_type="source_account" if account else "profile",
                )
                if asset:
                    if profile:
                        db.execute(
                            text(
                                "UPDATE profiles SET avatar_media_id=COALESCE(avatar_media_id,:asset) WHERE id=:id"
                            ),
                            {"id": profile, "asset": asset},
                        )
                    result["avatars"] += 1
        db.commit()
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sns-db", type=Path)
    parser.add_argument("--cache", type=Path)
    parser.add_argument("--instagram-root", type=Path)
    parser.add_argument("--media-root", type=Path, default=Path("/data/media"))
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--audit", action="store_true")
    args = parser.parse_args()
    if args.audit and args.instagram_root:
        parser.error("--audit currently supports only the Chatlog cache")
    stopping = False

    def stop_at_checkpoint(_signal, _frame):
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, stop_at_checkpoint)
    signal.signal(signal.SIGINT, stop_at_checkpoint)
    with SessionLocal() as db:
        if args.sns_db:
            report = moments_cache(
                db,
                args.sns_db,
                args.cache,
                args.media_root,
                args.limit,
                args.audit,
                lambda: stopping,
            )
            print(json.dumps(report), flush=True)
            if report.get("stopped_at_checkpoint"):
                raise SystemExit(75)
        if args.instagram_root:
            report = instagram_archive(db, args.instagram_root, args.media_root, lambda: stopping)
            print(json.dumps(report), flush=True)
            if report.get("stopped_at_checkpoint"):
                raise SystemExit(75)


if __name__ == "__main__":
    from .jobs import heavy_io, lower_priority

    lower_priority()
    with heavy_io("archive_media"):
        main()
