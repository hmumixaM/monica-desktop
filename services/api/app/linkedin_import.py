"""Shared import services; transaction ownership stays with the caller."""
from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from .bulk_moments import project_moments
from .imports import project_batch
from .jobs import heavy_io
from .remote_media import enqueue_media
from .schemas import ImportBatchRequest
from .source_time import iso


def now() -> datetime:
    return datetime.now(UTC)


def json_load(path: Path) -> Any:
    with path.open(encoding="utf-8") as stream:
        return json.load(stream)


def find_file(root: Path, *names: str) -> Path | None:
    for name in names:
        direct = root / name
        if direct.is_file():
            return direct
    for name in names:
        matches = sorted(root.rglob(name))
        if matches:
            return matches[0]
    return None


def chunks(values: list[Any], size: int = 500) -> Iterable[list[Any]]:
    for start in range(0, len(values), size):
        yield values[start : start + size]


def record_digest(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


def run_batch(
    db: Session,
    *,
    source: str,
    stream: str,
    records: list[dict[str, Any]],
    batch_name: str,
    chunk_size: int = 500,
) -> None:
    """Run a normalized API batch in bounded transactions."""
    stamp = now()
    observation = __import__("uuid").uuid4().hex if source == "linkedin" and stream in {"profiles", "posts"} else ""
    if not records:
        # An empty batch still documents a checked source stream and its cursor.
        records = []
    total = max(1, (len(records) + chunk_size - 1) // chunk_size)
    for index, part in enumerate(chunks(records, chunk_size) or [[]]):
        batch_id = f"{batch_name}-{index + 1:04d}-of-{total:04d}-{record_digest(part)[:16]}"
        if observation:
            batch_id += "-" + observation
        request = ImportBatchRequest(
            source=source,
            stream=stream,
            schema_version=1,
            batch_id=batch_id,
            idempotency_key=f"{source}:{stream}:{batch_id}",
            cursor_before=str(index) if index else None,
            cursor_after=str(index + 1),
            observed_at=stamp,
            records=part,
        )
        from .runtime_status import STOP
        if STOP.is_set():
            raise RuntimeError('Importer stopped at committed batch checkpoint')
        db.commit()  # Never retain a transaction while waiting for disk permission.
        with heavy_io(f"import {source}/{stream} batch"):
            if STOP.is_set():
                raise RuntimeError('Importer stopped before starting next batch')
            if source == "wechat" and stream == "moments":
                project_moments(db, request)
            else:
                project_batch(db, request)
            db.commit()
        from .runtime_status import progress
        progress(stage=stream, committed_batch=index+1, total_batches=total)
        if stream == "moments":
            print(f"Moments {batch_id}: {len(part)} records committed", flush=True)
        else:
            # Report only durable batches. These are observed source records,
            # not a percentage of a provider's potentially incomplete archive.
            print(json.dumps({
                "source": source, "stream": stream, "batch": index + 1,
                "batches_in_file": total, "records_checked": len(part),
                "batch_committed": True,
            }), flush=True)


def profile_map(db: Session, provider: str) -> dict[str, str]:
    rows = db.execute(
        text(
            """SELECT i.external_id,i.profile_id FROM identities i JOIN profiles p ON p.id=i.profile_id
            WHERE i.provider=:provider AND i.status NOT IN ('invalid','merged') AND p.archived_at IS NULL
            AND p.profile_type=CASE WHEN :provider='wechat_group' THEN 'group' ELSE 'person' END"""
        ),
        {"provider": provider},
    ).mappings()
    return {str(row["external_id"]): str(row["profile_id"]) for row in rows}


def upsert_identity(
    db: Session,
    *,
    provider: str,
    external_id: str,
    profile_id: str | None,
    username: str | None = None,
    profile_url: str | None = None,
    display_name: str | None = None,
    status: str = "active",
) -> None:
    if not external_id:
        return
    from .source_projection import enabled
    actual_provider='wechat' if provider=='wechat_group' else provider
    if enabled(db,actual_provider):
        from .platform_migrate import account_lookup, resolve_account
        _,aliases,profiles=account_lookup(db,actual_provider)
        aid=resolve_account(actual_provider,external_id,'',profile_id,aliases,profiles)
        if not aid:return
        profile_id=db.execute(text('SELECT profile_id FROM source_account_links WHERE account_id=:id'),{'id':aid}).scalar_one_or_none()
        db.execute(text('''INSERT INTO identities(profile_id,provider,external_id,username,profile_url,display_name,status,source_account_id)
            VALUES(:profile,:provider,:external,:username,:url,:name,:status,:account)
            ON CONFLICT(provider,external_id) DO UPDATE SET profile_id=EXCLUDED.profile_id,source_account_id=EXCLUDED.source_account_id,
              username=COALESCE(EXCLUDED.username,identities.username),profile_url=COALESCE(EXCLUDED.profile_url,identities.profile_url),
              display_name=COALESCE(EXCLUDED.display_name,identities.display_name),last_seen_at=now()'''),
            {'profile':profile_id,'provider':provider,'external':external_id,'username':username,'url':profile_url,'name':display_name,'status':status,'account':aid})
        return
    db.execute(
        text("""
          INSERT INTO identities(profile_id, provider, external_id, username, profile_url, display_name, status)
          VALUES (:profile_id, :provider, :external_id, :username, :profile_url, :display_name, :status)
          ON CONFLICT(provider, external_id) DO UPDATE SET
            profile_id = COALESCE(identities.profile_id, EXCLUDED.profile_id),
            username = COALESCE(EXCLUDED.username, identities.username),
            profile_url = COALESCE(EXCLUDED.profile_url, identities.profile_url),
            display_name = COALESCE(EXCLUDED.display_name, identities.display_name),
            status = CASE WHEN EXCLUDED.profile_id IS NULL THEN identities.status ELSE EXCLUDED.status END,
            last_seen_at = now()
        """),
        {
            "profile_id": profile_id,
            "provider": provider,
            "external_id": external_id,
            "username": username,
            "profile_url": profile_url,
            "display_name": display_name,
            "status": status,
        },
    )


def linkedin_profile_content(item: dict[str, Any]) -> dict[str, Any]:
    profile = item.get("profile") or {}
    experiences = profile.get("experiences") or []
    educations = profile.get("educations") or []
    location = profile.get("location")
    fields: dict[str, Any] = {}
    for key, value in (
        ("linkedin.about", profile.get("about")),
        ("linkedin.background_url", profile.get("background_url")),
        ("linkedin.open_to_work", profile.get("open_to_work")),
        ("linkedin.headline", profile.get("headline")),
        ("linkedin.profile_url", item.get("profile_url")),
        ("linkedin.mutuals", item.get("mutuals")),
        ("linkedin.partial", item.get("partial")),
        (
            "linkedin.avatar_url",
            profile.get("avatar_url")
            or profile.get("profile_image_url")
            or profile.get("image_url")
            or profile.get("photo_url"),
        ),
    ):
        if value not in (None, "", []):
            fields[key] = value
    return {
        "display_name": profile.get("name")
        or item.get("provider_user_id")
        or item.get("profile_url")
        or "LinkedIn profile",
        "summary": profile.get("about"),
        "profile_url": item.get("profile_url"),
        "fields": fields,
        "address": location,
        "employment": experiences,
        "education": educations,
        "avatar_url": profile.get("avatar_url")
        or profile.get("profile_image_url")
        or profile.get("image_url")
        or profile.get("photo_url"),
    }


def _linkedin_media(post: dict[str, Any]) -> list[dict[str, Any]]:
    """Normalize LinkedIn's image URL list into the common timeline shape."""
    rows: list[dict[str, Any]] = []
    urls = post.get("image_urls") or post.get("images") or []
    if isinstance(urls, str):
        urls = [urls]
    if not isinstance(urls, list):
        urls = []
    for index, url in enumerate(urls):
        if isinstance(url, dict):
            url = url.get("url") or url.get("source_url") or url.get("image_url")
        if not isinstance(url, str) or not url.strip():
            continue
        rows.append(
            {
                "role": f"media-{index + 1}",
                "media_type": "image",
                "source_url": url.strip(),
                "thumbnail_url": url.strip(),
            }
        )
    video = post.get("video_url")
    if isinstance(video, str) and video.strip():
        poster = rows[0]['source_url'] if rows else None
        rows = [{'role': 'video-1', 'media_type': 'video', 'source_url': video.strip(), 'thumbnail_url': poster}] + rows[1:]
    return rows


def load_linkedin(db: Session, root: Path, monica_map: dict[str, str], *, export_snapshot=None, incremental=False) -> list[str]:
    path = root if root.is_file() else find_file(root, "linkedin_enrichment.incoming.json")
    data = export_snapshot if export_snapshot is not None else (json_load(path) if path else {})
    pending = data.get('profiles', [])
    account_digests = {}
    if incremental:
        previous = dict(db.execute(text("SELECT stream,cursor_value FROM source_cursors WHERE source='linkedin' AND stream LIKE 'collector_account:%'")).all())
        selected = []
        for item in pending:
            provider_id = str(item.get('provider_user_id') or item.get('profile_url') or '')
            if not provider_id:
                continue
            digest = record_digest(item)
            if provider_id in account_digests and account_digests[provider_id] != digest:
                raise ValueError('Conflicting LinkedIn account entries in the same export')
            account_digests[provider_id] = digest
            if previous.get('collector_account:' + record_digest(provider_id)) != digest:
                selected.append(item)
        pending = selected
        if not pending:
            return []
    records = []
    for item in pending:
        provider_id = str(item.get("provider_user_id") or item.get("profile_url") or "")
        if not provider_id:
            continue
        records.append(
            {
                "external_id": provider_id,
                "profile_id": monica_map.get(str(item.get("contact_id"))),
                "content": linkedin_profile_content(item),
            }
        )
    run_batch(db, source="linkedin", stream="profiles", records=records,
              batch_name="profiles-media-v3-" + record_digest(records)[:20])
    db.execute(text("""
        UPDATE social_posts p SET external_id = co.payload_json->>'provider_post_id'
        FROM content_objects co WHERE co.id = p.content_object_id AND p.provider = 'linkedin'
            AND NULLIF(co.payload_json->>'provider_post_id', '') IS NOT NULL
            AND p.external_id IS DISTINCT FROM co.payload_json->>'provider_post_id'
            AND NOT EXISTS (SELECT 1 FROM social_posts other WHERE other.provider = 'linkedin'
                AND other.external_id = co.payload_json->>'provider_post_id' AND other.id <> p.id)
    """))
    linkedin_map = profile_map(db, "linkedin")
    for item in pending:
        provider_id = str(item.get("provider_user_id") or item.get("profile_url") or "")
        profile_id = monica_map.get(str(item.get("contact_id"))) or linkedin_map.get(provider_id)
        from .source_projection import enabled
        if not profile_id and not enabled(db,'linkedin'):
            continue
        upsert_identity(
            db,
            provider="linkedin",
            external_id=provider_id,
            profile_id=profile_id,
            profile_url=item.get("profile_url"),
            display_name=(item.get("profile") or {}).get("name"),
        )
        snapshots = sorted(
            item.get("snapshots") or [], key=lambda row: str(row.get("captured_at") or "")
        )
        snapshot_records = []
        for index, snapshot in enumerate(snapshots):
            snapshot_id = f"{provider_id}:{snapshot.get('hash') or index}"
            snapshot_records.append(
                {
                    "external_id": snapshot_id,
                    "profile_id": profile_id,
                    "content": {
                        "fields": {
                            "linkedin.snapshot": snapshot.get("payload") or snapshot,
                            "linkedin.snapshot_hash": snapshot.get("hash"),
                            "linkedin.snapshot_status": {
                                "partial": snapshot.get("partial"),
                                "kind": snapshot.get("kind"),
                            },
                        }
                    },
                    "source_updated_at": iso(snapshot.get("captured_at")),
                }
            )
        if snapshot_records:
            run_batch(
                db,
                source="linkedin",
                stream="snapshots",
                records=snapshot_records,
                batch_name=f"snapshots-{provider_id[:32]}-{record_digest(snapshot_records)[:16]}",
                chunk_size=250,
            )
        posts = []
        for post in item.get("posts") or []:
            posts.append({**post, "profile_id": profile_id, "provider": "linkedin"})
        if posts:
            normalized = []
            for post in posts:
                media = _linkedin_media(post)
                normalized.append(
                    {
                        "external_id": str(
                            post.get("provider_post_id") or post.get("urn") or post.get("external_id") or post.get("id") or post.get("source_key") or record_digest(post)
                        ),
                        "profile_id": profile_id,
                        "source_account_external_id": provider_id,
                        "provider": "linkedin",
                        "entity_type": "post",
                        "occurred_at": iso(post.get("occurred_at") or post.get("posted_at") or post.get("created_at")),
                        "content": {
                            **post,
                            "media": media,
                            "like_count": post.get("reaction_count"),
                            "comment_count": post.get("comment_count"),
                        },
                        "media": media,
                    }
                )
            run_batch(
                db,
                source="linkedin",
                stream="posts",
                records=normalized,
                batch_name=f"posts-media-v3-{provider_id[:32]}-{record_digest(normalized)[:16]}",
            )
        if incremental:
            # The account checkpoint follows every committed profile, fact,
            # snapshot and post. A failed account remains eligible for retry.
            db.execute(text("""INSERT INTO source_cursors(source,stream,cursor_value,batch_id)
                SELECT 'linkedin',:stream,:digest,id FROM import_batches
                WHERE source='linkedin' AND status='complete' ORDER BY completed_at DESC LIMIT 1
                ON CONFLICT(source,stream) DO UPDATE SET cursor_value=EXCLUDED.cursor_value,
                    batch_id=EXCLUDED.batch_id,updated_at=now()"""),
                {'stream':'collector_account:' + record_digest(provider_id),'digest':account_digests[provider_id]})
            db.commit()
    db.execute(
        text("""
      INSERT INTO global_metric_snapshots(metric_key, value_json, source_watermark, freshness_state)
      VALUES ('coverage.linkedin', CAST(:value AS jsonb), :watermark, 'fresh')
    """),
        {
            "value": json.dumps(
                {
                    "profiles": len(data.get("profiles", [])),
                    "posts": sum(len(x.get("posts") or []) for x in data.get("profiles", [])),
                    "status": "partial"
                    if any(x.get("partial") for x in data.get("profiles", []))
                    else "fresh",
                }
            ),
            "watermark": str(data.get("generated_at") or now().isoformat()),
        },
    )
    db.commit()

    return [str(item.get('provider_user_id') or item.get('profile_url') or '') for item in pending]


def materialize_remote_media(db: Session, media_root: Path, *, providers=None, account_external_ids=None) -> int:
    """Backfill queue entries; worker downloads without blocking requests/imports."""
    from .remote_media import enqueue_post_media
    queued = 0
    for row in db.execute(text("""
        SELECT p.id, co.payload_json FROM social_posts p
        JOIN content_objects co ON co.id = p.content_object_id WHERE p.provider = ANY(:providers)
        AND (CAST(:accounts AS text[]) IS NULL OR p.source_account_id IN
             (SELECT id FROM source_accounts WHERE external_id=ANY(CAST(:accounts AS text[]))))
    """),{'providers':providers or ['linkedin'], 'accounts': account_external_ids}).mappings():
        payload = row["payload_json"] or {}
        for index, media in enumerate(payload.get("media") or _linkedin_media(payload)):
            queued += enqueue_post_media(db, media=media, post_id=row["id"], index=index)
    for row in db.execute(text("""
        SELECT c.profile_id, co.payload_json AS url FROM profile_field_current c
        JOIN profile_field_revisions r ON r.id = c.current_revision_id
        JOIN content_objects co ON co.id = r.content_object_id
        WHERE c.field_key = 'linkedin.avatar_url'
        AND (CAST(:accounts AS text[]) IS NULL OR c.profile_id IN (SELECT l.profile_id
          FROM source_account_links l JOIN source_accounts a ON a.id=l.account_id
          WHERE a.external_id=ANY(CAST(:accounts AS text[]))))
    """), {'accounts': account_external_ids}).mappings():
        queued += enqueue_media(db, url=row["url"], entity_type="profile",
                                entity_id=row["profile_id"], role="linkedin_avatar")
    db.commit()
    return queued
