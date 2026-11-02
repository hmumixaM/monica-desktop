#!/usr/bin/env python3
"""Preload original Moments photos, videos, and Live Photo motion into Chatlog's cache.

The SNS database is read locally, while each media resource is requested through
Chatlog's media proxy. The proxy checks its own cache before contacting WeChat's
CDN. Returned decrypted bytes are atomically published to this importer's cache,
which may be separate from the proxy's filesystem.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import html
import json
import os
import random
import re
import sqlite3
import tempfile
import time
import xml.etree.ElementTree as ET
from collections import Counter
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime
from http.client import HTTPException
from pathlib import Path
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

MEDIA_RE = re.compile(r"<media>(.*?)</media>", re.DOTALL)
TYPE_RE = re.compile(r"<type>([^<]*)</type>")
CONTENT_TYPE_RE = re.compile(r"<ContentObject>.*?<type>([^<]*)</type>", re.DOTALL)
LIVE_PHOTO_RE = re.compile(r"<LivePhoto>(.*?)</LivePhoto>", re.DOTALL | re.IGNORECASE)
LIVE_MEDIA_RE = re.compile(r"<liveMedia>(.*?)</liveMedia>", re.DOTALL | re.IGNORECASE)
VIDEO_KEY_RE = re.compile(r'<enc\s+key="(\d+)"')
COMMENT_RE = re.compile(r"<comment_user_list>(.*?)</comment_user_list>", re.DOTALL)
COMMENT_IMAGE_RE = re.compile(r"<imageinfo>(.*?)</imageinfo>", re.DOTALL)
RESOURCE_RE = {
    name: re.compile(rf"<{name}([^>]*)>(.*?)</{name}>", re.DOTALL) for name in ("url", "thumb")
}
ATTR_RE = {name: re.compile(rf'{name}="([^"]*)"') for name in ("key", "token")}
IMAGE_CONTENT_TYPES = {"1", "7", "54"}
VIDEO_CONTENT_TYPES = {"6", "15"}
INCREMENTAL_IMPORT_KINDS = {
    "photo",
    "video",
    "live_photo",
    "avatar_big",
    "avatar_small",
    "moments_cover",
}


@dataclass(frozen=True)
class MediaResource:
    tid: int
    username: str
    kind: str
    url: str
    key: str
    tids: tuple[int, ...] = ()

    @property
    def cache_name(self) -> str:
        cache_key = "" if self.key == "0" else self.key
        digest = hashlib.md5(f"{self.url}|{cache_key}".encode())
        return (
            f"{digest.hexdigest()}.attachment.bin"
            if self.kind == "announcement_attachment"
            else f"{digest.hexdigest()}.bin"
        )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, required=True, help="decrypted sns.db")
    parser.add_argument(
        "--contact-db", type=Path, help="defaults to the sibling contact/contact.db"
    )
    parser.add_argument(
        "--supplemental-only",
        action="store_true",
        help="covers, brand icons, stranger avatars and notice attachments only",
    )
    parser.add_argument("--no-avatars", action="store_true", help="only prefetch Moments media")
    parser.add_argument("--base-url", default="http://nas:5030")
    parser.add_argument("--cache-dir", type=Path)
    parser.add_argument(
        "--content-types",
        default="1,6,7,15,54",
        help="outer Moments content types to include; defaults to photos and native videos",
    )
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--daily-cap", type=int, default=3000)
    parser.add_argument("--min-wait", type=float, default=0.5)
    parser.add_argument("--max-wait", type=float, default=2.0)
    parser.add_argument("--limit", type=int)
    parser.add_argument(
        "--moments-since-timestamp",
        type=int,
        help="only download Moments media created at or after this Unix timestamp",
    )
    parser.add_argument(
        "--import-queue",
        type=Path,
        help="append newly completed cache records for incremental Monica import",
    )
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def extract_resource(media_xml: str, name: str) -> tuple[str, str, str]:
    match = RESOURCE_RE[name].search(media_xml)
    if match is None:
        return "", "", ""
    attributes, raw_url = match.groups()
    values = {}
    for attr, pattern in ATTR_RE.items():
        attr_match = pattern.search(attributes)
        values[attr] = attr_match.group(1) if attr_match else ""
    return html.unescape(raw_url.strip()), values["key"], values["token"]


def media_url(raw_url: str, token: str, *, video: bool) -> str:
    # The source scheme is part of its fetch contract and cache identity.
    # Do not force HTTP to HTTPS or downgrade an HTTPS source.
    url = raw_url.strip()
    if not video:
        url = url.replace("/150?", "/0?")
        if url.endswith("/150"):
            url = f"{url[:-4]}/0"
    if not token or "token=" in url:
        return url
    if video and "?" in url:
        base, query = url.split("?", 1)
        return f"{base}?{urlencode({'token': token, 'idx': 1})}&{query}"
    separator = "&" if "?" in url else "?"
    return f"{url}{separator}{urlencode({'token': token, 'idx': 1})}"


def media_in_xml(
    tid: int,
    username: str,
    content: str,
    content_type: str,
) -> Iterable[MediaResource]:
    # Comments can contain pictures even when the post is text-only (type 2).
    for comments in COMMENT_RE.findall(content):
        for raw in re.findall(r"<user_comment>(.*?)</user_comment>", comments, re.DOTALL):
            if re.search(r"<b_deleted>\s*1\s*</b_deleted>", raw):
                continue
            for image_xml in COMMENT_IMAGE_RE.findall(raw):
                try:
                    image = ET.fromstring("<imageinfo>" + image_xml + "</imageinfo>")
                except ET.ParseError:
                    continue
                url = image.findtext("url") or image.findtext("thumb_url") or ""
                original = bool(image.findtext("url"))
                token = image.findtext("token" if original else "thumb_url_token") or ""
                key = image.findtext("key" if original else "thumb_key") or ""
                if url:
                    yield MediaResource(tid, username, "photo", media_url(url, token, video=False), key)

    video_key_match = VIDEO_KEY_RE.search(content)
    video_key = video_key_match.group(1) if video_key_match else ""

    for media_match in MEDIA_RE.finditer(content):
        media_xml = media_match.group(1)
        if content_type in IMAGE_CONTENT_TYPES:
            media_type = TYPE_RE.search(media_xml)
            if media_type is None or media_type.group(1).strip() != "2":
                continue
            raw_url, key, token = extract_resource(media_xml, "url")
            if not raw_url:
                raw_url, key, token = extract_resource(media_xml, "thumb")
            url = media_url(raw_url, token, video=False)
            if url:
                yield MediaResource(
                    tid=tid,
                    username=username,
                    kind="photo",
                    url=url,
                    key=key,
                )
        elif content_type in VIDEO_CONTENT_TYPES:
            raw_url, key, token = extract_resource(media_xml, "url")
            key = key if key not in ("", "0") else video_key
            url = media_url(raw_url, token, video=True)
            if url:
                yield MediaResource(
                    tid=tid,
                    username=username,
                    kind="video",
                    url=url,
                    key=key,
                )

        live_match = LIVE_PHOTO_RE.search(media_xml)
        if live_match is None:
            continue
        live_xml = live_match.group(1)
        live_media_match = LIVE_MEDIA_RE.search(live_xml)
        resource_xml = live_media_match.group(1) if live_media_match else live_xml
        raw_url, key, token = extract_resource(resource_xml, "url")
        live_key_match = VIDEO_KEY_RE.search(resource_xml)
        live_key = live_key_match.group(1) if live_key_match else video_key
        key = key if key not in ("", "0") else live_key
        url = media_url(raw_url, token, video=True)
        if url:
            yield MediaResource(
                tid=tid,
                username=username,
                kind="live_photo",
                url=url,
                key=key,
            )


def read_media(database: Path, content_types: set[str], since: int | None = None) -> list[MediaResource]:
    connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    unique: dict[str, MediaResource] = {}
    try:
        rows = connection.execute("SELECT tid, user_name, content FROM SnsTimeLine")
        for tid, username, content in rows:
            if since is not None:
                created = re.search(r"<createTime>(\d+)</createTime>", content or "")
                if not created or int(created.group(1)) < since:
                    continue
            content_type = CONTENT_TYPE_RE.search(content)
            if content_type is None:
                continue
            kind = content_type.group(1).strip()
            if kind not in content_types and not COMMENT_IMAGE_RE.search(content):
                continue
            for media in media_in_xml(
                tid,
                username,
                content,
                kind if kind in content_types else "",
            ):
                existing = unique.get(media.cache_name)
                if existing is None:
                    unique[media.cache_name] = replace(media, tids=(tid,))
                elif tid not in existing.tids:
                    unique[media.cache_name] = replace(existing, tids=(*existing.tids, tid))
    finally:
        connection.close()
    return list(unique.values())


def moments_tids_since(database: Path, timestamp: int) -> set[int]:
    tids = set()
    with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as connection:
        for tid, content in connection.execute("SELECT tid,content FROM SnsTimeLine"):
            match = re.search(r"<createTime>(\d+)</createTime>", content or "")
            if match and int(match.group(1)) >= timestamp:
                tids.add(tid)
    return tids


def append_import_queue(path: Path, media: MediaResource, size: int, *, cache_name: str | None = None, durable: bool = True) -> None:
    record = {
        "version": 1,
        "observed_at": datetime.now().astimezone().isoformat(),
        "cache_name": cache_name or media.cache_name,
        "tid": media.tid,
        "tids": list(media.tids or (media.tid,)),
        "username": media.username,
        "kind": media.kind,
        "byte_length": size,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    with metadata_lock(path), path.open("a", encoding="utf-8") as output:
        output.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
        output.flush()
        if durable:
            os.fsync(output.fileno())


@contextmanager
def metadata_lock(path: Path):
    """Protect a local publication only; never wrap fetch/network calls."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_name(path.name + ".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def write_json_atomic(path: Path, value) -> None:
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as output:
            temporary = Path(output.name)
            os.chmod(temporary, 0o600)
            json.dump(value, output, ensure_ascii=False, sort_keys=True)
            output.flush()
            os.fsync(output.fileno())
        temporary.replace(path)
    finally:
        if temporary:
            temporary.unlink(missing_ok=True)


AVATAR_HOSTS = {"wx.qlogo.cn", "mmhead.c2c.wechat.com", "mmhead.hk.wechat.com", "wework.qpic.cn"}


def read_avatars(database: Path, table: str = "contact") -> tuple[list[MediaResource], list[dict]]:
    if table not in ("contact", "stranger"):
        raise ValueError("Unexpected contact table")
    resources = {}
    references = []
    if not database.is_file():
        return [], []
    with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as connection:
        for username, big, small in connection.execute(
            f"SELECT username,big_head_url,small_head_url FROM {table}"
        ):
            if not username:
                continue
            for kind, raw_url in (("avatar_big", big), ("avatar_small", small)):
                url = (raw_url or "").strip()
                if not url:
                    continue
                try:
                    parsed = urlsplit(url)
                    if (
                        parsed.scheme not in ("http", "https")
                        or parsed.hostname not in AVATAR_HOSTS
                        or parsed.username
                        or parsed.password
                        or parsed.port not in (None, 80, 443)
                    ):
                        continue
                except ValueError:
                    continue
                item = MediaResource(tid=0, username=username, kind=kind, url=url, key="")
                resources.setdefault(item.cache_name, item)
                references.append(
                    {
                        "username": username,
                        "kind": kind,
                        "url": url,
                        "cache_name": item.cache_name,
                        "source_table": table,
                    }
                )
    return list(resources.values()), references


def write_avatar_manifest(cache: Path, references: list[dict]) -> None:
    # Keep both source roles even when they resolve to the same cached file.
    destination = cache / "avatar-manifest.json"
    value = {
        "version": 1,
        "observed_at": datetime.now().astimezone().isoformat(),
        "avatars": references,
    }
    write_json_atomic(destination, value)


COVER_HOSTS = {"shmmsns.qpic.cn", "szmmsns.qpic.cn", "mmsns.qpic.cn", "mmsns.c2c.wechat.com"}
BRAND_HOSTS = {"mmbiz.qpic.cn", "mmecoa.qpic.cn"}


def protobuf_bytes(data):
    """Read length-delimited fields without interpreting unrelated profile data."""

    def number(offset):
        result = 0
        for n in range(10):
            byte = data[offset]
            offset += 1
            result |= (byte & 127) << (7 * n)
            if byte < 128:
                return result, offset
        raise ValueError("Invalid varint")

    offset = 0
    fields = []
    while offset < len(data):
        tag, offset = number(offset)
        field, wire = tag >> 3, tag & 7
        if not field:
            raise ValueError("Invalid field")
        if wire == 0:
            _, offset = number(offset)
        elif wire in (1, 5):
            offset += 8 if wire == 1 else 4
        elif wire == 2:
            length, offset = number(offset)
            value = data[offset : offset + length]
            offset += length
            if len(value) != length:
                raise ValueError("Truncated field")
            fields.append((field, value))
        else:
            raise ValueError("Unsupported wire type")
        if offset > len(data):
            raise ValueError("Truncated field")
    return fields


def read_supplemental(database):
    resources, references = read_avatars(database, "stranger")
    unique = {item.cache_name: item for item in resources}

    def add(username, kind, url, allowed, **metadata):
        url = (url or "").strip()
        try:
            parsed = urlsplit(url)
            if (
                parsed.scheme not in ("http", "https")
                or parsed.hostname not in allowed
                or parsed.username
                or parsed.password
                or parsed.port not in (None, 80, 443)
            ):
                return
        except ValueError:
            return
        item = MediaResource(0, username, kind, url, "")
        unique.setdefault(item.cache_name, item)
        references.append(
            {
                "username": username,
                "kind": kind,
                "url": url,
                "cache_name": item.cache_name,
                **metadata,
            }
        )

    if not database.is_file():
        return [], []
    with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as db:
        for table in ("contact", "stranger"):
            for username, extra in db.execute(
                f"SELECT username,extra_buffer FROM {table} WHERE extra_buffer IS NOT NULL"
            ):
                try:
                    for field, value in protobuf_bytes(extra):
                        if field == 27:
                            for nested, raw in protobuf_bytes(value):
                                if nested == 2:
                                    add(
                                        username,
                                        "moments_cover",
                                        raw.decode("utf-8"),
                                        COVER_HOSTS,
                                        source_table=table,
                                        source_field="extra_buffer.27.2",
                                    )
                except (ValueError, IndexError, UnicodeError):
                    continue
        for user, url in db.execute(
            "SELECT username,brand_icon_url FROM biz_info WHERE brand_icon_url IS NOT NULL"
        ):
            add(user, "brand_icon", url, BRAND_HOSTS, source_table="biz_info")
        for user, xml in db.execute(
            "SELECT username_,xml_announcement_ FROM chat_room_info_detail WHERE xml_announcement_ IS NOT NULL"
        ):
            try:
                root = ET.fromstring(xml)
            except ET.ParseError:
                continue
            for item in root.iter("dataitem"):
                add(
                    user,
                    "announcement_attachment",
                    item.findtext("cdn_dataurl"),
                    {"wxapp.tc.qq.com"},
                    source_table="chat_room_info_detail",
                    data_id=item.get("dataid"),
                    data_format=item.findtext("datafmt"),
                    original_md5=item.findtext("fullmd5"),
                    original_size=item.findtext("fullsize"),
                    encrypted=bool(item.findtext("cdn_datakey")),
                    storage="raw_download_requires_validation",
                )
    return list(unique.values()), references


class AttachmentRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        parsed = urlsplit(newurl)
        if (
            parsed.scheme not in ("http", "https")
            or parsed.hostname != "wxapp.tc.qq.com"
            or parsed.username
            or parsed.password
            or parsed.port not in (None, 80, 443)
        ):
            raise ValueError("Unexpected attachment redirect")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch_attachment(media, cache):
    # Download original bytes only. Decryption keys stay in the contact DB.
    if cache is None:
        raise ValueError("Attachment download requires --cache-dir")
    destination = cache / media.cache_name
    if destination.is_file() and destination.stat().st_size:
        return destination.stat().st_size
    request = Request(media.url, headers={"User-Agent": "MicroMessenger Client"})
    temporary = None
    try:
        with build_opener(ProxyHandler({}), AttachmentRedirect()).open(
            request, timeout=90
        ) as response:
            if response.status != 200 or response.headers.get_content_type() in (
                "text/html",
                "application/json",
            ):
                raise ValueError("Attachment response is not a complete file")
            total = 0
            with tempfile.NamedTemporaryFile(dir=cache, delete=False) as output:
                temporary = Path(output.name)
                while chunk := response.read(1024 * 1024):
                    total += len(chunk)
                    if total > 50 * 1024 * 1024:
                        raise ValueError("Attachment exceeds 50 MiB")
                    output.write(chunk)
        if not total:
            raise ValueError("Empty attachment")
        temporary.replace(destination)
        return total
    finally:
        if temporary:
            temporary.unlink(missing_ok=True)


def fetch(
    media: MediaResource, base_url: str, min_wait: float, max_wait: float, cache_dir=None
) -> int:
    time.sleep(random.uniform(min_wait, max_wait))
    if media.kind == "announcement_attachment":
        return fetch_attachment(media, cache_dir)
    query = urlencode({"url": media.url, "key": media.key})
    request_url = f"{base_url.rstrip('/')}/api/v1/sns/media/proxy?{query}"
    opener = build_opener(ProxyHandler({}))
    total = 0
    temporary = None
    try:
        with opener.open(request_url, timeout=900 if media.kind in {"video", "live_photo"} else 90) as response:
            if cache_dir is None:
                while chunk := response.read(1024 * 1024):
                    total += len(chunk)
                return total
            if response.status != 200 or response.headers.get_content_type() in (
                "text/html", "application/json"
            ):
                raise ValueError("Media proxy response is not a complete media file")
            with tempfile.NamedTemporaryFile(dir=cache_dir, delete=False) as output:
                temporary = Path(output.name)
                while chunk := response.read(1024 * 1024):
                    total += len(chunk)
                    output.write(chunk)
                output.flush()
                os.fsync(output.fileno())
            if not total:
                raise ValueError("Empty media proxy response")
            expected = response.headers.get("Content-Length")
            if expected is not None and int(expected) != total:
                raise ValueError("Incomplete media proxy response")
            temporary.replace(cache_dir / media.cache_name)
        return total
    finally:
        if temporary:
            temporary.unlink(missing_ok=True)


FAILURE_BACKOFF_SECONDS = 60
FAILURE_BACKOFF_CAP_SECONDS = 3600


def failure_ledger_path(args: argparse.Namespace) -> Path | None:
    return None if args.cache_dir is None else args.cache_dir / "prefetch-failures.json"


def read_failures(path: Path | None) -> dict[str, dict]:
    if path is None or not path.is_file():
        return {}
    failures = json.loads(path.read_text())
    # Earlier versions postponed transient failures for up to 30 days. Bring
    # that legacy ledger back into the current bounded retry schedule.
    now = time.time()
    for entry in failures.values():
        if entry.get("next_attempt", 0) > now + FAILURE_BACKOFF_CAP_SECONDS:
            entry["next_attempt"] = now
    return failures


def record_failure(failures: dict[str, dict], media: MediaResource, error: Exception, now: float) -> None:
    """Back off per resource: a proxy 500 for an expired CDN object repeats every run."""
    count = failures.get(media.cache_name, {}).get("failures", 0) + 1
    delay = min(FAILURE_BACKOFF_SECONDS * 2 ** min(count - 1, 6), FAILURE_BACKOFF_CAP_SECONDS)
    failures[media.cache_name] = {"failures": count, "next_attempt": now + delay,
                                  "kind": media.kind, "last_error": str(error)[:200]}


def write_failures(path: Path | None, failures: dict[str, dict], updates: dict | None = None) -> None:
    if path is None:
        return
    with metadata_lock(path):
        if updates is not None:
            current = read_failures(path)
            for name, value in updates.items():
                if value is None:
                    current.pop(name, None)
                else:
                    current[name] = value
            failures.clear()
            failures.update(current)
        write_json_atomic(path, failures)


def cached_path(media: MediaResource, cache: Path) -> Path | None:
    candidates = [cache / media.cache_name]
    if media.url.startswith("http://"):
        candidates.append(cache / replace(media, url=media.url.replace("http://", "https://", 1)).cache_name)
    return next((path for path in candidates if path.is_file() and path.stat().st_size > 0), None)


def publish_cached_moments(resources, cache: Path | None, queue: Path | None, limit=500) -> int:
    """Recover cache/queue crash gaps, including a reused image on a new post.

    The existing queue is the durable publication journal. Only Moments are
    repaired here: contact roles are published by their normal downloader.
    Replay is safe even if the consumer has already committed the same link.
    """
    if cache is None or queue is None:
        return 0
    published = set()
    if queue.is_file():
        with queue.open() as journal:
            for line in journal:
                try:
                    record = json.loads(line)
                    for tid in record.get("tids", [record.get("tid")]):
                        published.add((record["cache_name"], record["kind"], int(tid)))
                except (ValueError, KeyError, TypeError, AttributeError):
                    continue
    count = 0
    try:
        for media in sorted(resources, key=lambda item: item.tid, reverse=True):
            if media.kind not in {"photo", "video", "live_photo"}:
                continue
            path = cached_path(media, cache)
            if path is None:
                continue
            tids = media.tids or (media.tid,)
            missing = tuple(tid for tid in tids if (path.name, media.kind, tid) not in published)
            if not missing:
                continue
            append_import_queue(queue, replace(media, tid=missing[0], tids=missing), path.stat().st_size, cache_name=path.name, durable=False)
            published.update((path.name, media.kind, tid) for tid in missing)
            count += 1
            if count >= limit:
                break
    finally:
        # The archive consumer runs after this producer returns. Persist the
        # entire repair batch before any byte-offset checkpoint can acknowledge
        # it, including when a later cache lookup fails partway through repair.
        if count:
            with queue.open("rb") as journal:
                os.fsync(journal.fileno())
    return count


def media_for_today(
    resources: list[MediaResource],
    args: argparse.Namespace,
    failures: dict[str, dict] | None = None,
    now: float | None = None,
) -> list[MediaResource]:
    if failures:
        moment = time.time() if now is None else now
        resources = [media for media in resources
                     if failures.get(media.cache_name, {}).get("next_attempt", 0) <= moment]
    if args.cache_dir is not None:
        cached_names = {path.name for path in args.cache_dir.iterdir()}

        def already_cached(media):
            if media.cache_name in cached_names:
                return True
            # Historical prefetch forcibly upgraded source HTTP URLs. Reuse
            # those successful files without repeating their network request.
            if media.url.startswith("http://") and media.kind != "announcement_attachment":
                old_url = media.url.replace("http://", "https://", 1)
                old_name = hashlib.md5(f"{old_url}|{media.key}".encode()).hexdigest() + ".bin"
                return old_name in cached_names
            return False

        resources = [media for media in resources if not already_cached(media)]
    resources.sort(key=lambda media: (media.tid == 0, -media.tid, media.cache_name))
    fresh = [media for media in resources if not (failures or {}).get(media.cache_name)]
    retries = [media for media in resources if (failures or {}).get(media.cache_name)]
    resources = fresh + retries
    if args.limit is not None:
        return resources[: args.limit]
    if not resources:
        return []
    today = datetime.now().astimezone().date()
    if retries:
        start = today.toordinal() * args.daily_cap % len(retries)
        retries = retries[start:] + retries[:start]
    return (fresh + retries)[: args.daily_cap]


def main() -> None:
    args = parse_args()
    if args.workers < 1:
        raise ValueError("--workers must be at least 1")
    if not 0 <= args.min_wait <= args.max_wait:
        raise ValueError("wait bounds must satisfy 0 <= min <= max")

    content_types = {item.strip() for item in args.content_types.split(",") if item.strip()}
    all_media = [] if args.supplemental_only else read_media(args.db, content_types, args.moments_since_timestamp)
    avatar_references = []
    if not args.no_avatars and not args.supplemental_only:
        contact_db = args.contact_db or args.db.parent.parent / "contact" / "contact.db"
        avatars, avatar_references = read_avatars(contact_db)
        all_media.extend(avatars)
        if args.cache_dir is not None and not args.dry_run:
            write_avatar_manifest(args.cache_dir, avatar_references)
    contact_db = args.contact_db or args.db.parent.parent / "contact" / "contact.db"
    supplemental, references = read_supplemental(contact_db)
    all_media.extend(supplemental)
    all_media = list({item.cache_name: item for item in all_media}.values())
    if args.cache_dir is not None and not args.dry_run:
        path = args.cache_dir / "contact-media-manifest.json"
        write_json_atomic(path,
                {
                    "version": 1,
                    "observed_at": datetime.now().astimezone().isoformat(),
                    "resources": references,
                })
    ledger = failure_ledger_path(args)
    failures = read_failures(ledger)
    # A proxy/cache hit can precede queue publication (or come from another
    # reader). Repair that gap independently of the download selection/backoff.
    repaired = publish_cached_moments(all_media, args.cache_dir, args.import_queue) if not args.dry_run else 0
    if repaired:
        print(json.dumps({"requeued_cached_moments": repaired}), flush=True)
    media = media_for_today(all_media, args, failures)
    totals = Counter(item.kind for item in all_media)
    print(
        f"found {len(all_media)} unique original media "
        f"(photos={totals['photo']} videos={totals['video']} "
        f"live_photos={totals['live_photo']} avatar_big={totals['avatar_big']} avatar_small={totals['avatar_small']}); "
        f"{len(media)} uncached resources selected today",
        flush=True,
    )
    print("available kinds: " + json.dumps(dict(totals), sort_keys=True), flush=True)
    selected = Counter(item.kind for item in media)
    print("selected kinds: " + json.dumps(dict(selected), sort_keys=True), flush=True)
    if args.dry_run:
        return

    completed = failed = downloaded_bytes = 0
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        batch_size = args.workers * 4
        for batch_start in range(0, len(media), batch_size):
            batch = media[batch_start : batch_start + batch_size]
            updates = {}
            futures = {
                pool.submit(
                    fetch,
                    item,
                    args.base_url,
                    args.min_wait,
                    args.max_wait,
                    args.cache_dir,
                ): item
                for item in batch
            }
            for future in as_completed(futures):
                item = futures[future]
                try:
                    size = future.result()
                except (HTTPException, OSError, ValueError) as error:
                    failed += 1
                    record_failure(failures, item, error, time.time())
                    updates[item.cache_name] = failures[item.cache_name]
                    print(
                        f"FAIL kind={item.kind} tid={item.tid} user={item.username} "
                        f"cache={item.cache_name}: {error}",
                        flush=True,
                    )
                    continue
                completed += 1
                downloaded_bytes += size
                failures.pop(item.cache_name, None)
                updates[item.cache_name] = None
                if args.import_queue is not None and item.kind in INCREMENTAL_IMPORT_KINDS:
                    destination = args.cache_dir / item.cache_name
                    if destination.is_file() and destination.stat().st_size:
                        append_import_queue(args.import_queue, item, size)
                    else:
                        raise OSError(f"successful response did not create cache {item.cache_name}")
                if item.kind.startswith("avatar_") and completed <= 5:
                    print(f"cached {item.kind} cache={item.cache_name} bytes={size}", flush=True)
                if completed % 100 == 0 or completed == len(media):
                    print(
                        f"progress {completed}/{len(media)} failed={failed} "
                        f"served={downloaded_bytes / 1024 / 1024:.1f} MiB",
                        flush=True,
                    )
            write_failures(ledger, failures, updates)

    print(
        f"finished: {completed} served, {failed} failed, "
        f"{downloaded_bytes / 1024 / 1024:.1f} MiB served",
        flush=True,
    )


if __name__ == "__main__":
    main()
