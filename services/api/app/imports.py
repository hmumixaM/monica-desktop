"""Authenticated, idempotent import batch projection."""

import hashlib
import json
from datetime import UTC, datetime
from typing import Any

from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.orm import Session

from .config import get_settings
from .content import canonical_json, get_or_create_content
from .import_common import _content_value, _parse_datetime, _record_entity
from .media_store import store_bytes
from .project_messages import _project_message_record
from .project_profiles import (
    _ensure_profile_for_record,
    _entity_id,
    _project_structured_fact,
    _project_structured_profile,
    _structured_values,
)
from .project_social import _project_social_record
from .remote_media import enqueue_media, enqueue_post_media
from .repositories import add_observation, append_field_revision
from .schemas import ImportBatchRequest


def digest_record(record: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_json(record)).hexdigest()


def _write_raw_manifest(request: ImportBatchRequest) -> tuple[dict[str, Any], str]:
    manifest = request.model_dump(mode="json")
    root = get_settings().raw_root
    for record in manifest["records"]:
        payload = canonical_json(record.pop("content"))
        content_digest = hashlib.sha256(payload).hexdigest()
        relative = f"objects/sha256/{content_digest[:2]}/{content_digest}.json"
        content_path = root / relative
        content_path.parent.mkdir(parents=True, exist_ok=True)
        if not content_path.exists():
            content_path.write_bytes(payload)
        record["content_ref"] = relative
    manifest["manifest_version"] = 2
    digest = digest_record(manifest)
    safe_stream = "".join(
        char if char.isalnum() or char in "-_" else "_" for char in request.stream
    )
    target = get_settings().raw_root / request.source / safe_stream / "sha256" / f"{digest}.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.exists():
        temporary = target.with_suffix(".tmp")
        temporary.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
        temporary.replace(target)
    return manifest, str(target)


def hydrate_manifest(manifest: dict) -> dict:
    """Read content references, retaining compatibility with inline archives."""
    result = {**manifest, "records": []}
    for record in manifest.get("records", []):
        item = dict(record)
        reference = item.pop("content_ref", None)
        if reference:
            root = get_settings().raw_root.resolve()
            path = (root / reference).resolve()
            if not path.is_relative_to(root / "objects"):
                raise ValueError("Invalid content reference")
            item["content"] = json.loads(path.read_text())
        result["records"].append(item)
    return result


def project_batch(db: Session, request: ImportBatchRequest) -> dict[str, Any]:
    """Commit source facts once per import batch, before the CRM caller commits.

    Every job/school formerly opened and fsynced its own source transaction,
    including unchanged facts. Keep ordered observations and replay IDs while
    closing all source transactions before returning a successful projection.
    Standalone repairs retain their normal immediate source commit behavior.
    """
    from contextlib import ExitStack

    if (get_settings().async_projection_enabled and request.source in ('wechat', 'instagram', 'linkedin')
            and not db.info.get('input_projecting')):
        from .projection_feed import stage_batch
        return stage_batch(db, request)

    if 'structured_source_batch' in db.info:
        raise RuntimeError('Import batches cannot be nested in one session')
    with ExitStack() as stack:
        db.info['structured_source_batch']=(stack,{})
        try:
            return _project_batch(db,request)
        finally:
            db.info.pop('structured_source_batch',None)


def _project_batch(db: Session, request: ImportBatchRequest) -> dict[str, Any]:
    db.info.pop('source_lookup',None)
    existing = (
        db.execute(
            text("SELECT * FROM import_batches WHERE idempotency_key = :key"),
            {"key": request.idempotency_key},
        )
        .mappings()
        .one_or_none()
    )
    if existing and existing["status"] == "complete":
        db.execute(
            text("""
              INSERT INTO source_cursors(source, stream, cursor_value, batch_id)
              VALUES (:source, :stream, :cursor_value, :batch_id)
              ON CONFLICT (source, stream) DO UPDATE SET
                cursor_value = EXCLUDED.cursor_value,
                batch_id = EXCLUDED.batch_id,
                updated_at = now()
            """),
            {
                "source": existing["source"],
                "stream": existing["stream"],
                "cursor_value": existing["cursor_after"],
                "batch_id": existing["id"],
            },
        )
        return dict(existing)
    if existing:
        batch_db_id = existing["id"]
    else:
        manifest, manifest_path = _write_raw_manifest(request)
        raw_manifest_id, _ = get_or_create_content(
            db,
            value=manifest,
            content_kind="raw_json",
            storage_uri=manifest_path,
            store_payload=False,
        )
        batch_db_id = db.execute(
            text("""
              INSERT INTO import_batches
                (source, stream, schema_version, batch_id, idempotency_key,
                 cursor_before, cursor_after, raw_manifest_object_id, status)
              VALUES (:source, :stream, :schema_version, :batch_id, :key,
                      :before, :after, :raw_manifest_id, 'projecting')
              RETURNING id
            """),
            {
                "source": request.source,
                "stream": request.stream,
                "schema_version": request.schema_version,
                "batch_id": request.batch_id,
                "key": request.idempotency_key,
                "before": request.cursor_before,
                "after": request.cursor_after,
                "raw_manifest_id": raw_manifest_id,
            },
        ).scalar_one()

    inserted = changed = unchanged = 0
    affected_profiles: set[str] = set()
    from .source_projection import finish_record, prepare_records
    for record in prepare_records(db,request):
        content = record.content
        resolved_profile_id = _ensure_profile_for_record(db, record, request, content)
        if resolved_profile_id and resolved_profile_id != record.profile_id:
            record = record.model_copy(update={"profile_id": resolved_profile_id})
        if record.profile_id:
            affected_profiles.add(str(record.profile_id))
        digest = digest_record(content)
        previous = db.execute(
            text("""
                SELECT content_hash FROM source_observations
                WHERE source = :source AND stream = :stream AND external_id = :external_id
                ORDER BY observed_at DESC LIMIT 1
            """),
            {"source": request.source, "stream": request.stream, "external_id": record.external_id},
        ).scalar_one_or_none()
        state = "new" if previous is None else ("unchanged" if previous == digest else "changed")
        if state == "new":
            inserted += 1
        elif state == "changed":
            changed += 1
        else:
            unchanged += 1
        add_observation(
            db,
            batch_db_id=batch_db_id,
            source=request.source,
            stream=request.stream,
            external_id=record.external_id,
            source_cursor=request.cursor_after,
            digest=digest,
            state=state,
            observed_at=request.observed_at,
            source_updated_at=record.source_updated_at,
        )
        _project_social_record(db, record, request, content)
        _project_message_record(db, record, request, content)
        _project_structured_profile(db, record, request, content)
        # Generic profile streams can project one field or a field map without
        # coupling the importer to provider-specific schemas.
        field_values: dict[str, Any] = {}
        if isinstance(content, dict) and content.get("field_key"):
            field_values[str(content["field_key"])] = content.get("value")
        elif isinstance(content, dict) and isinstance(content.get("fields"), dict):
            field_values = {str(key): value for key, value in content["fields"].items()}
        for field_key, field_value in field_values.items():
            if not record.profile_id and not getattr(record,'source_account_id',None):
                continue
            is_manual_current = (
                db.execute(
                    text("""
                    SELECT current_source_type FROM profile_field_current
                    WHERE profile_id = :profile_id AND field_key = :field_key
                """),
                    {"profile_id": record.profile_id, "field_key": field_key},
                ).scalar_one_or_none()
                == "manual"
            )
            append_field_revision(
                db,
                profile_id=record.profile_id,
                field_key=field_key,
                value=field_value,
                source_type=request.source,
                operation="import",
                source_record_id=record.external_id,
                observed_at=request.observed_at,
                is_conflict=is_manual_current,
                source_account_id=getattr(record,'source_account_id',None),
            )

        finish_record(db,record,request)

        if request.source in ("linkedin", "instagram") and isinstance(content, dict):
            fields = content.get("fields") or {}
            avatar = fields.get(f"{request.source}.avatar_url") or fields.get('instagram.profile_pic_url') or content.get("avatar_url")
            source_account=getattr(record,'source_account_id',None)
            if avatar and (record.profile_id or source_account):
                enqueue_media(db, url=avatar, entity_type="source_account" if source_account else "profile",
                              entity_id=source_account or record.profile_id, role=f"{request.source}_avatar")
            if _record_entity(record, request, content) == "post":
                post_id = db.execute(text("""
                    SELECT id FROM social_posts WHERE provider = :source AND external_id = :id
                """), {"id": record.external_id,'source':request.source}).scalar_one_or_none()
                media_rows = record.media or content.get("media") or [
                    {"source_url": url} for url in content.get("image_urls", [])
                ]
                if post_id:
                    for index, media in enumerate(media_rows):
                        if isinstance(media, dict):
                            enqueue_post_media(db, media=media, post_id=post_id, index=index)

        if request.source == 'wechat' and isinstance(content, dict):
            from .moments_xml import enqueue_finder_covers
            finder_rows = content.get('media') or []
            if any(item.get('source_kind') == 'wechat_channels' for item in finder_rows):
                post_id = db.execute(text("SELECT id FROM social_posts WHERE provider='wechat' AND external_id=:id"), {'id':record.external_id}).scalar_one_or_none()
                if post_id:
                    enqueue_finder_covers(db, post_id, finder_rows)

        for media in record.media:
            media_bytes = media.get("bytes")
            if not isinstance(media_bytes, str):
                # Importers normally upload media separately. A manifest-only record
                # is kept in raw JSON and does not create a fake asset.
                continue
            try:
                payload = bytes.fromhex(media_bytes)
            except ValueError as exc:
                raise HTTPException(status_code=422, detail="media.bytes must be hex") from exc
            # Inline bytes are stored like every other asset: on disk by hash.
            store_bytes(db, payload, get_settings().media_root,
                        media_type=media.get("media_type", "application/octet-stream"),
                        source_type=request.source, source_record_id=record.external_id)

        entity_type = _record_entity(record, request, content)
        if entity_type in {"post", "story"} and record.media:
            provider = record.provider or request.source
            table = "social_posts" if entity_type == "post" else "social_stories"
            entity_name = "social_post" if entity_type == "post" else "social_story"
            entity_id = db.execute(
                text(
                    f"SELECT id FROM {table} WHERE provider = :provider AND external_id = :external_id"
                ),
                {"provider": provider, "external_id": record.external_id},
            ).scalar_one_or_none()
            if entity_id:
                for index, media in enumerate(record.media):
                    media_id = media.get("media_id")
                    if not media_id:
                        media_hash = media.get("sha256")
                        if not media_hash and isinstance(media.get("bytes"), str):
                            try:
                                media_hash = hashlib.sha256(
                                    bytes.fromhex(media["bytes"])
                                ).hexdigest()
                            except ValueError:
                                media_hash = None
                        if media_hash:
                            media_id = db.execute(
                                text("SELECT id FROM media_assets WHERE sha256 = :sha256"),
                                {"sha256": media_hash},
                            ).scalar_one_or_none()
                    if media_id:
                        db.execute(
                            text("""
                              INSERT INTO media_links(media_id, entity_type, entity_id, role)
                              VALUES (:media_id, :entity_type, :entity_id, :role)
                              ON CONFLICT DO NOTHING
                            """),
                            {
                                "media_id": media_id,
                                "entity_type": entity_name,
                                "entity_id": entity_id,
                                "role": media.get("role", f"media-{index + 1}"),
                            },
                        )

    completed_at = datetime.now(UTC)
    if inserted or changed:
        db.execute(
            text("""
              INSERT INTO jobs(job_type, payload)
              VALUES ('metrics.refresh', CAST(:payload AS jsonb))
            """),
            {
                "payload": json.dumps(
                    {
                        "batch_id": request.batch_id,
                        "source": request.source,
                        "affected_profiles": sorted(affected_profiles),
                        "metric_keys": [
                            "global.profile_count",
                            "global.timeline_count",
                            f"provider.{request.source}.coverage",
                        ],
                    }
                )
            },
        )
    result = (
        db.execute(
            text("""
            UPDATE import_batches SET status = 'complete', completed_at = :completed_at,
              inserted_count = :inserted, changed_count = :changed, unchanged_count = :unchanged
            WHERE id = :batch_id
            RETURNING *
        """),
            {
                "completed_at": completed_at,
                "inserted": inserted,
                "changed": changed,
                "unchanged": unchanged,
                "batch_id": batch_db_id,
            },
        )
        .mappings()
        .one()
    )
    db.execute(
        text("""
          INSERT INTO source_cursors(source, stream, cursor_value, batch_id)
          VALUES (:source, :stream, :cursor_value, :batch_id)
          ON CONFLICT (source, stream) DO UPDATE SET
            cursor_value = EXCLUDED.cursor_value,
            batch_id = EXCLUDED.batch_id,
            updated_at = now()
        """),
        {
            "source": request.source,
            "stream": request.stream,
            "cursor_value": request.cursor_after,
            "batch_id": batch_db_id,
        },
    )
    return dict(result)


def list_batches(db: Session, limit: int = 50):
    return list(
        db.execute(
            text("SELECT * FROM import_batches ORDER BY received_at DESC LIMIT :limit"),
            {"limit": limit},
        ).mappings()
    )

__all__ = ['_content_value', '_ensure_profile_for_record', '_entity_id', '_parse_datetime', '_project_message_record', '_project_social_record', '_project_structured_fact', '_project_structured_profile', '_record_entity', '_structured_values']
