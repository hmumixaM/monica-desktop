"""Refresh formal friends and create independent contacts for unclaimed accounts.

Existing identity/link decisions take precedence over the user's automatic
new-friend policy. Friend content, including WAL changes, identifies each batch.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import time
from collections import Counter
from contextlib import closing
from datetime import UTC, datetime
from hashlib import md5, sha256
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from .archive_media import link, prepare_archive_batch, register_prepared_file
from .content import canonical_json
from .imports import project_batch
from .platform_store import account_id, ensure_account, platform_engine
from .remote_media import retain_source_media
from .schemas import BatchRecord, ImportBatchRequest

CONTACT_IMPORT_VERSION = 2

SYSTEM_ACCOUNTS = {
    "brandsessionholder",
    "exmail_tool",
    "feedsapp",
    "filehelper",
    "floatbottle",
    "fmessage",
    "medianote",
    "mphelper",
    "newsapp",
    "notification_messages",
    "notifymessage",
    "opencustomerservicemsg",
    "qmessage",
    "qqmail",
    "tmessage",
    "voip",
    "weibo",
    "weixin",
    "weixinreminder",
    "weixinteam",
}

FRIEND_SOURCE_LABELS = {
    0: "未记录来源",
    1: "QQ 号搜索",
    3: "微信号搜索",
    4: "QQ 好友",
    6: "邮箱搜索",
    8: "群聊",
    9: "手机通讯录",
    10: "手机通讯录",
    12: "QQ 好友",
    13: "手机通讯录",
    14: "群聊",
    15: "手机号搜索",
    17: "名片分享",
    18: "附近的人",
    22: "摇一摇",
    25: "漂流瓶",
    29: "摇一摇",
    30: "扫一扫二维码",
    48: "雷达加朋友",
}


def _clean(value: Any) -> str:
    return " ".join(value.split()) if isinstance(value, str) else ""


def _category(username: str, local_type: int) -> str:
    if username.endswith("@chatroom"):
        return "chatroom"
    if username.startswith("gh_"):
        return "official_account"
    if username.endswith("@openim"):
        return "openim"
    if username in SYSTEM_ACCOUNTS or username.startswith("@"):
        return "system"
    return "stranger" if local_type == 3 else "friend"


def _read_varint(buffer: bytes, position: int) -> tuple[int, int]:
    value = shift = 0
    while True:
        if position >= len(buffer) or shift > 63:
            raise ValueError("invalid protobuf varint")
        byte = buffer[position]
        position += 1
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value, position
        shift += 7


def _parse_fields(buffer: bytes | None) -> dict[int, list[int | bytes]]:
    fields: dict[int, list[int | bytes]] = {}
    position = 0
    try:
        while buffer and position < len(buffer):
            key, position = _read_varint(buffer, position)
            field_number, wire_type = key >> 3, key & 0x07
            if not field_number:
                raise ValueError("invalid protobuf field")
            if wire_type == 0:
                value, position = _read_varint(buffer, position)
            elif wire_type in (1, 5):
                length = 8 if wire_type == 1 else 4
                value, position = buffer[position : position + length], position + length
            elif wire_type == 2:
                length, position = _read_varint(buffer, position)
                value, position = buffer[position : position + length], position + length
            else:
                raise ValueError("unsupported protobuf wire type")
            if position > len(buffer):
                raise ValueError("truncated protobuf field")
            fields.setdefault(field_number, []).append(value)
    except (IndexError, ValueError):
        return {}
    return fields


def _first_int(fields: dict[int, list[int | bytes]], number: int) -> int | None:
    return next((value for value in fields.get(number, []) if isinstance(value, int)), None)


def _first_text(fields: dict[int, list[int | bytes]], number: int) -> str:
    for value in fields.get(number, []):
        if isinstance(value, bytes):
            try:
                return _clean(value.decode())
            except UnicodeDecodeError:
                return ""
    return ""


def _profile_metadata(buffer: bytes | None, label_names: dict[int, str]) -> dict[str, Any]:
    fields = _parse_fields(buffer)
    source_code = _first_int(fields, 8)
    cover = ""
    for value in fields.get(27, []):
        if isinstance(value, bytes):
            cover = _first_text(_parse_fields(value), 2)
            if cover.startswith(("http://", "https://")):
                break
            cover = ""
    label_ids = {int(part) for part in _first_text(fields, 30).split(",") if part.strip().isdigit()}
    return {
        "signature": _first_text(fields, 4),
        "add_source_code": source_code,
        "add_source_label": (
            FRIEND_SOURCE_LABELS.get(source_code, f"未知来源（代码 {source_code}）")
            if source_code is not None
            else ""
        ),
        "moments_cover_url": cover,
        "labels": [label_names[label_id] for label_id in label_ids if label_id in label_names],
    }


def read_friends(database: Path) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Read formal friends only; strangers, rooms, official accounts, and system rows stay out."""
    with closing(sqlite3.connect(f"file:{database}?mode=ro", uri=True)) as connection:
        label_names = {
            int(label_id): _clean(name)
            for label_id, name in connection.execute(
                "SELECT label_id_,label_name_ FROM contact_label"
            )
            if _clean(name)
        }
        rows = connection.execute(
            """
            SELECT username,local_type,delete_flag,alias,remark,nick_name,
                   big_head_url,small_head_url,description,extra_buffer
            FROM contact
            """
        )
        friends = []
        excluded: Counter[str] = Counter()
        for (
            username,
            local_type,
            delete_flag,
            alias,
            remark,
            nickname,
            big_head_url,
            small_head_url,
            description,
            extra_buffer,
        ) in rows:
            username = _clean(username)
            category = _category(username, int(local_type or 0)) if username else "invalid"
            if delete_flag:
                excluded["deleted"] += 1
                continue
            if category != "friend":
                excluded[category] += 1
                continue
            friends.append(
                {
                    "username": username,
                    "alias": _clean(alias),
                    "remark": _clean(remark),
                    "nick_name": _clean(nickname),
                    "big_head_url": _clean(big_head_url),
                    "small_head_url": _clean(small_head_url),
                    "description": _clean(description),
                    **_profile_metadata(extra_buffer, label_names),
                }
            )
    friends.sort(key=lambda item: item["username"])
    return friends, dict(excluded)


def contact_content(friend: dict[str, Any]) -> dict[str, Any]:
    fields = {
        key: value
        for key, value in (
            ("wechat.alias", friend["alias"]),
            ("wechat.remark", friend["remark"]),
            ("wechat.nickname", friend["nick_name"]),
            ("wechat.signature", friend["signature"]),
            ("wechat.add_source", friend["add_source_label"]),
            ("wechat.labels", friend["labels"]),
            ("wechat.description", friend["description"]),
            (
                "wechat.avatar_url",
                friend["big_head_url"] or friend["small_head_url"],
            ),
            ("wechat.moments_cover_url", friend["moments_cover_url"]),
        )
        if value not in ("", [], None)
    }
    return {
        "display_name": (
            friend["remark"] or friend["nick_name"] or friend["alias"] or friend["username"]
        ),
        "summary": friend["signature"] or friend["description"] or None,
        "username": friend["username"],
        "profile_url": friend["big_head_url"] or None,
        "fields": fields,
        "avatar_url": friend["big_head_url"] or friend["small_head_url"] or None,
        "moments_cover_url": friend["moments_cover_url"] or None,
    }


def cached_media(cache: Path, url: str, allowed_names: set[str] | None = None) -> Path | None:
    candidates = [url]
    if url.startswith("http://"):
        candidates.append(url.replace("http://", "https://", 1))
    for candidate in candidates:
        name = md5(f"{candidate}|".encode()).hexdigest() + ".bin"
        if allowed_names is not None and name not in allowed_names:
            continue
        path = cache / name
        if path.is_file() and path.stat().st_size:
            return path
    return None


def sync_contact_media(
    db: Session,
    database: Path,
    cache: Path,
    media_root: Path,
    roles: set[str] | None = None,
    usernames: set[str] | None = None,
    cache_names: set[str] | None = None,
) -> dict[str, Any]:
    friends, _ = read_friends(database)
    if usernames is not None:
        friends = [friend for friend in friends if friend["username"] in usernames]
    accounts = {
        row.external_id: str(row.id)
        for row in db.execute(
            text("SELECT id,external_id FROM source_accounts WHERE provider='wechat'")
        )
    }
    current = {
        (str(row.account_id), row.role): str(row.media_id)
        for row in db.execute(
            text(
                """
                SELECT m.account_id,m.role,m.media_id FROM source_account_media m
                JOIN media_assets a ON a.id=m.media_id
                WHERE m.role IN ('wechat_avatar_big','wechat_avatar_small','wechat_avatar','cover')
                """
            )
        )
    }
    urls = {
        url
        for friend in friends
        for role, url in (
            ("wechat_avatar_big", friend["big_head_url"]),
            ("wechat_avatar_small", friend["small_head_url"]),
            ("cover", friend["moments_cover_url"]),
        )
        if url and (roles is None or role in roles)
    }
    known_media = {
        row.source_url: str(row.media_id)
        for row in db.execute(
            text(
                """
                SELECT r.source_url,r.media_id FROM remote_media_sources r
                JOIN media_assets a ON a.id=r.media_id
                WHERE r.source_url=ANY(:urls)
                """
            ),
            {"urls": list(urls)},
        )
    }
    db.commit()
    # Resolve caches and stage immutable bytes before the account/link writes.
    # NAS filesystem reads can exceed the database idle-transaction timeout.
    paths_by_url = {url: cached_media(cache, url, cache_names) for url in urls}
    paths_by_url = {url: path.resolve() for url, path in paths_by_url.items() if path is not None}
    prepared = prepare_archive_batch(db, paths_by_url.values(), media_root, "wechat")
    report: Counter[str] = Counter()
    matched_cache_names: set[str] = set()
    for friend in friends:
        account = accounts.get(friend["username"])
        if not account:
            report["unknown_accounts"] += 1
            continue
        resources = (
            ("wechat_avatar_big", friend["big_head_url"]),
            ("wechat_avatar_small", friend["small_head_url"]),
            ("cover", friend["moments_cover_url"]),
        )
        avatars: dict[str, tuple[str, Any]] = {}
        for role, url in resources:
            if roles is not None and role not in roles:
                continue
            if not url:
                continue
            report["references"] += 1
            path = paths_by_url.get(url)
            if cache_names is not None and path is None:
                report["missing_cache"] += 1
                continue
            if path is not None:
                matched_cache_names.add(path.name)
            existing = current.get((account, role))
            if existing is not None and known_media.get(url) == existing:
                report["already_linked"] += 1
                if role.startswith("wechat_avatar_"):
                    avatars[role] = (url, existing)
                continue
            if path is None:
                report["missing_cache"] += 1
                continue
            stamp, asset, snapshot = prepared[path]
            if asset is None:
                asset = register_prepared_file(db, path, stamp, snapshot, "wechat")
            if not asset:
                report["invalid_cache"] += 1
                continue
            link(
                db,
                asset,
                account,
                role,
                provider_url=url,
                entity_type="source_account",
                retain=False,
            )
            retain_source_media(
                db,
                {
                    "entity_type": "source_account",
                    "entity_id": account,
                    "role": role,
                    "download_key": f"contact-media:{role}:{url}",
                },
                asset,
            )
            current[(account, role)] = str(asset)
            known_media[url] = str(asset)
            report["linked"] += 1
            if role.startswith("wechat_avatar_"):
                avatars[role] = (url, asset)
            if report["linked"] % 50 == 0:
                db.commit()
            if report["linked"] % 500 == 0:
                print(
                    json.dumps(
                        {
                            "avatar_media_linked": report["linked"],
                            "already_linked": report["already_linked"],
                            "missing_cache": report["missing_cache"],
                        }
                    ),
                    flush=True,
                )
        selected = avatars.get("wechat_avatar_big")
        if selected is None and current.get((account, "wechat_avatar")) is None:
            selected = avatars.get("wechat_avatar_small")
        if selected:
            url, asset = selected
            if not db.execute(
                text("SELECT 1 FROM media_assets WHERE id=:id"),
                {"id": asset},
            ).scalar_one_or_none():
                report["stale_selected_media"] += 1
                continue
            if current.get((account, "wechat_avatar")) == str(asset):
                report["already_selected"] += 1
                continue
            link(
                db,
                asset,
                account,
                "wechat_avatar",
                provider_url=url,
                entity_type="source_account",
                retain=False,
            )
            retain_source_media(
                db,
                {
                    "entity_type": "source_account",
                    "entity_id": account,
                    "role": "wechat_avatar",
                    "download_key": f"contact-media:wechat_avatar:{url}",
                },
                asset,
            )
            current[(account, "wechat_avatar")] = str(asset)
            report["selected_avatars"] += 1
    db.commit()
    result: dict[str, Any] = dict(report)
    if cache_names is not None:
        result["unmatched_queue_files"] = sorted(cache_names - matched_cache_names)
    return result


def create_friend_profiles(db: Session, friends: list[dict[str, Any]], *, deadline=None) -> int:
    """User-authorized policy for verified friends, never matching people by name.

    Any existing identity or link decision is left to the user, including an
    explicit unlink. Only previously unclaimed, formal friend accounts qualify.
    """
    from .account_links import set_link
    from .jobs import ShutdownRequested
    from .ownership import lock_accounts, related_accounts
    from .repositories import create_profile
    from .runtime_status import STOP

    eligible = """SELECT a.id,a.external_id FROM source_accounts a
        WHERE a.provider='wechat' AND a.object_kind='person' AND a.external_id=ANY(:ids)
          AND NOT EXISTS(SELECT 1 FROM source_account_links l WHERE l.account_id=a.id)
          AND NOT EXISTS(SELECT 1 FROM source_account_link_revisions r WHERE r.account_id=a.id)
          AND NOT EXISTS(SELECT 1 FROM identities i WHERE i.source_account_id=a.id
                         OR (i.provider='wechat' AND i.external_id=a.external_id))
          AND NOT EXISTS(SELECT 1 FROM own_message_accounts o WHERE o.account_id=a.id)"""
    by_id = {friend['username']: friend for friend in friends}
    candidates = db.execute(text(eligible), {'ids': list(by_id)}).mappings().all()
    db.commit()
    created = 0
    for account in sorted(candidates, key=lambda row: str(row['id'])):
        if STOP.is_set():
            raise ShutdownRequested('Stopped before creating the next friend profile')
        if deadline is not None and time.monotonic() >= deadline:
            break
        # Same barrier/ownership lock order as manual linking; recheck after
        # acquiring it so a concurrent manual decision always takes precedence.
        barrier = 'pg_advisory_xact_lock_shared' if os.getenv('MONICA_SCOPED_OWNERSHIP_LOCKS') == '1' else 'pg_advisory_xact_lock'
        db.execute(text(f'SELECT {barrier}(11803295)'))
        lock_accounts(db, [account['id']], related_accounts(db, account['id']))
        current = db.execute(text(eligible), {'ids': [account['external_id']]}).mappings().one_or_none()
        if current:
            content = contact_content(by_id[account['external_id']])
            profile = create_profile(db, 'person', content['display_name'], content.get('summary'))
            set_link(db, account['id'], profile['id'], evidence={
                'kind': 'wechat-formal-friend', 'policy': 'auto-create-new-person',
                'source': 'contact/contact.db',
            })
            created += 1
        db.commit()
    return created


def sync_contacts(
    db: Session,
    database: Path,
    *,
    batch_size: int = 500,
    work_budget_seconds: float = 120,
) -> dict[str, Any]:
    from .runtime_status import STOP, progress

    deadline = time.monotonic() + work_budget_seconds
    friends, excluded = read_friends(database)
    # A WAL snapshot can change while its main file keeps the old size/mtime.
    # Identify the logical rows we actually read, never the physical DB file.
    observed_at = datetime.now(UTC)
    before = {
        row.external_id
        for row in db.execute(
            text("SELECT external_id FROM source_accounts WHERE provider='wechat'")
        )
    }
    totals: Counter[str] = Counter()
    for offset in range(0, len(friends), batch_size):
        if STOP.is_set() or time.monotonic() >= deadline:
            return {**totals, 'stopped_at_checkpoint': True, 'formal_friends': len(friends)}
        chunk = friends[offset : offset + batch_size]
        payloads = [{"external_id": friend["username"], "content": contact_content(friend)} for friend in chunk]
        digest = sha256(canonical_json(payloads)).hexdigest()
        key = f"chatlog-contacts-v{CONTACT_IMPORT_VERSION}-{digest}"
        completed_key = key
        from .config import get_settings
        if get_settings().async_projection_enabled and batch_size <= 25:
            from .projection_feed import batch_key
            completed_key = batch_key(key)
        # Completed content batches are durable checkpoints. Avoid repeating
        # thousands of source-account writes on every resumed archive cycle.
        if db.execute(text("SELECT 1 FROM import_batches WHERE idempotency_key=:key AND status='complete'"), {'key': completed_key}).first():
            db.commit()
            totals['profiles_created'] += create_friend_profiles(db, chunk, deadline=deadline)
            progress(stage='contacts', processed=offset+len(chunk), total=len(friends))
            continue
        with platform_engine("wechat").begin() as source:
            for friend in chunk:
                content = contact_content(friend)
                ensure_account(
                    source,
                    "wechat",
                    friend["username"],
                    display_name=content["display_name"],
                    username=friend["alias"] or friend["username"],
                    profile_url=friend["big_head_url"] or None,
                )
        account_rows = [
            {
                "id": account_id("wechat", friend["username"]),
                "external": friend["username"],
                "name": contact_content(friend)["display_name"],
                "username": friend["alias"] or friend["username"],
                "url": friend["big_head_url"] or None,
            }
            for friend in chunk
        ]
        from .ownership import lock_accounts, lock_profiles
        db.execute(text('SELECT pg_advisory_xact_lock_shared(11803295)'))
        lock_accounts(db, [row['id'] for row in account_rows])
        db.execute(
            text(
                """
                INSERT INTO source_accounts(id,provider,external_id,object_kind,display_name,username,profile_url,migrated_at)
                VALUES(:id,'wechat',:external,'person',:name,:username,:url,now())
                ON CONFLICT(id) DO UPDATE SET display_name=EXCLUDED.display_name,username=EXCLUDED.username,
                    profile_url=COALESCE(EXCLUDED.profile_url,source_accounts.profile_url),migrated_at=now()
                """
            ),
            account_rows,
        )
        # Explicit account ownership works both before and after activation of
        # source-first ingestion. Existing exact identities are retained; names
        # are never used to resolve a person.
        profiles = dict(db.execute(text("""SELECT a.external_id,
            COALESCE(l.profile_id, CASE WHEN NOT EXISTS(
                SELECT 1 FROM source_account_link_revisions r WHERE r.account_id=a.id)
                THEN i.profile_id END) AS profile_id
            FROM source_accounts a LEFT JOIN source_account_links l ON l.account_id=a.id
            LEFT JOIN identities i ON i.provider='wechat' AND i.external_id=a.external_id
                AND i.status NOT IN ('invalid','merged')
            WHERE a.provider='wechat' AND a.external_id=ANY(:ids)"""),
            {'ids': [friend['username'] for friend in chunk]}).all())
        lock_profiles(db, profiles.values())
        request = ImportBatchRequest(
            source="wechat",
            stream="contacts",
            batch_id=key,
            idempotency_key=key,
            cursor_after=digest,
            observed_at=observed_at,
            records=[
                BatchRecord(
                    external_id=friend["username"],
                    content=contact_content(friend),
                    profile_id=profiles.get(friend['username']),
                    source_account_id=account_id('wechat', friend['username']),
                    source_is_authoritative=True,
                    source_updated_at=observed_at,
                )
                for friend in chunk
            ],
        )
        result = project_batch(db, request)
        totals.update(
            {
                "inserted": int(result.get("inserted_count") or 0),
                "changed": int(result.get("changed_count") or 0),
                "unchanged": int(result.get("unchanged_count") or 0),
            }
        )
        db.execute(
            text(
                """
                UPDATE identities SET display_name=:name,username=:username,
                    profile_url=COALESCE(:url,profile_url),last_seen_at=now()
                WHERE source_account_id=:id AND status NOT IN ('invalid','merged')
                """
            ),
            account_rows,
        )
        db.commit()
        totals['profiles_created'] += create_friend_profiles(db, chunk, deadline=deadline)
        progress(stage='contacts', processed=offset+len(chunk), total=len(friends))
    if time.monotonic() >= deadline:
        return {**totals, 'stopped_at_checkpoint': True, 'formal_friends': len(friends)}
    current_ids = {friend["username"] for friend in friends}
    linked = db.execute(
        text(
            """
            SELECT count(*) FROM source_account_links l
            JOIN source_accounts a ON a.id=l.account_id
            WHERE a.provider='wechat' AND a.external_id=ANY(:ids)
            """
        ),
        {"ids": list(current_ids)},
    ).scalar_one()
    return {
        "source_rows": len(friends) + sum(excluded.values()),
        "formal_friends": len(friends),
        "new_source_accounts": len(current_ids - before),
        "profiles_created": totals["profiles_created"],
        "linked_accounts_refreshed": linked,
        "inserted": totals["inserted"],
        "changed": totals["changed"],
        "unchanged": totals["unchanged"],
        "excluded": excluded,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--cache", type=Path, required=True)
    parser.add_argument("--media-root", type=Path, required=True)
    parser.add_argument("--roles", nargs="+")
    arguments = parser.parse_args()
    from .db import SessionLocal

    with SessionLocal() as db:
        report = sync_contact_media(
            db,
            arguments.db,
            arguments.cache,
            arguments.media_root,
            roles=set(arguments.roles) if arguments.roles else None,
        )
    print(json.dumps(report, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
