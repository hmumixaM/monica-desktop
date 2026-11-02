"""Bounded migration path for the large, already normalized Moments archive."""

import hashlib
import json

from sqlalchemy import text

from .content import canonical_json, get_or_create_content
from .imports import _write_raw_manifest
from .interactions import project_interactions


def project_moments(db, request):
    existing = db.execute(text("SELECT * FROM import_batches WHERE idempotency_key = :key"),
                          {"key": request.idempotency_key}).mappings().one_or_none()
    if existing and existing["status"] == "complete":
        return
    from .source_projection import enabled
    if enabled(db,'wechat'):
        # Uses the shared source-first path after the independent database has
        # passed migration verification. Historic migration stays bounded.
        from .imports import project_batch
        return project_batch(db,request)
    manifest, path = _write_raw_manifest(request)
    manifest_id, _ = get_or_create_content(db, value=manifest, content_kind="raw_json",
                                         storage_uri=path, store_payload=False)
    batch_id = db.execute(text("""
        INSERT INTO import_batches(source, stream, schema_version, batch_id, idempotency_key,
            cursor_before, cursor_after, raw_manifest_object_id, status)
        VALUES ('wechat', 'moments', :schema, :batch, :key, :before, :after, :manifest, 'projecting')
        ON CONFLICT(idempotency_key) DO UPDATE SET status = 'projecting' RETURNING id
    """), {"schema": request.schema_version, "batch": request.batch_id,
           "key": request.idempotency_key, "before": request.cursor_before,
           "after": request.cursor_after, "manifest": manifest_id}).scalar_one()
    rows = []
    for record in request.records:
        payload = canonical_json(record.content)
        rows.append({"external_id": record.external_id, "profile_id": str(record.profile_id) if record.profile_id else None,
                     "occurred_at": record.occurred_at.isoformat() if record.occurred_at else None,
                     "payload": json.loads(payload), "digest": hashlib.sha256(payload).hexdigest(),
                     "byte_length": len(payload)})
    db.execute(text("""
        CREATE TEMP TABLE moments_input ON COMMIT DROP AS
        SELECT r.*, p.current_content_hash AS old_hash,
            CASE WHEN o.content_hash IS NULL THEN 'new'
                 WHEN o.content_hash = r.digest THEN 'unchanged' ELSE 'changed' END AS state
        FROM jsonb_to_recordset(CAST(:rows AS jsonb)) AS r(external_id text, profile_id uuid,
            occurred_at timestamptz, payload jsonb, digest char(64), byte_length bigint)
        LEFT JOIN social_posts p ON p.provider = 'wechat' AND p.external_id = r.external_id
        LEFT JOIN LATERAL (
            SELECT content_hash FROM source_observations
            WHERE source = 'wechat' AND stream = 'moments' AND external_id = r.external_id
            ORDER BY observed_at DESC LIMIT 1
        ) o ON true
    """), {"rows": json.dumps(rows)})
    db.execute(text("""
        INSERT INTO content_objects(sha256, content_kind, byte_length, payload_json)
        SELECT DISTINCT ON (digest) digest, 'post_body', byte_length, payload FROM moments_input
        ON CONFLICT(content_kind, sha256) DO NOTHING
    """))
    db.execute(text("""
        INSERT INTO source_observations(source, stream, external_id, batch_id, source_cursor,
            content_hash, observed_at, state)
        SELECT 'wechat', 'moments', external_id, :batch, :cursor, digest, :observed, state
        FROM moments_input ON CONFLICT(batch_id, source, stream, external_id) DO NOTHING
    """), {"batch": batch_id, "cursor": request.cursor_after, "observed": request.observed_at})
    db.execute(text("""
        INSERT INTO social_posts(profile_id, provider, external_id, content_object_id,
            occurred_at, current_content_hash)
        SELECT i.profile_id, 'wechat', i.external_id, c.id, i.occurred_at, i.digest
        FROM moments_input i JOIN content_objects c ON c.content_kind='post_body' AND c.sha256=i.digest
        ON CONFLICT(provider, external_id) DO UPDATE SET
            profile_id=COALESCE(EXCLUDED.profile_id, social_posts.profile_id),
            content_object_id=EXCLUDED.content_object_id,
            occurred_at=COALESCE(EXCLUDED.occurred_at, social_posts.occurred_at),
            current_content_hash=EXCLUDED.current_content_hash, last_seen_at=now()
    """))
    db.execute(text("""
        INSERT INTO social_post_revisions(post_id, content_object_id, content_hash, observed_at, source_record_id)
        SELECT p.id, p.content_object_id, i.digest, :observed, i.external_id
        FROM moments_input i JOIN social_posts p ON p.provider='wechat' AND p.external_id=i.external_id
        WHERE i.old_hash IS DISTINCT FROM i.digest

    """), {"observed": request.observed_at})
    from .moments_xml import enqueue_finder_covers
    for row in db.execute(text("SELECT p.id,i.payload FROM moments_input i JOIN social_posts p ON p.provider='wechat' AND p.external_id=i.external_id")).mappings():
        enqueue_finder_covers(db, row['id'], row['payload'].get('media') or [])
    project_interactions(db,request.records,'wechat')
    counts = {r.state: r.count for r in db.execute(text("SELECT state, count(*) FROM moments_input GROUP BY state"))}
    if counts.get("new") or counts.get("changed"):
        affected = [str(r.profile_id) for r in db.execute(text("""
            SELECT DISTINCT profile_id FROM moments_input
            WHERE state <> 'unchanged' AND profile_id IS NOT NULL
        """))]
        db.execute(text("INSERT INTO jobs(job_type, payload) VALUES ('metrics.refresh', CAST(:p AS jsonb))"),
                   {"p": json.dumps({"source": "wechat", "batch_id": request.batch_id, "affected_profiles": affected})})
    db.execute(text("""
        UPDATE import_batches SET status='complete', completed_at=now(),
            inserted_count=:new, changed_count=:changed, unchanged_count=:unchanged WHERE id=:id
    """), {"id": batch_id, "new": counts.get("new", 0), "changed": counts.get("changed", 0),
           "unchanged": counts.get("unchanged", 0)})
    db.execute(text("""
        INSERT INTO source_cursors(source, stream, cursor_value, batch_id)
        VALUES ('wechat', 'moments', :cursor, :batch)
        ON CONFLICT(source, stream) DO UPDATE SET cursor_value=EXCLUDED.cursor_value,
            batch_id=EXCLUDED.batch_id, updated_at=now()
    """), {"cursor": request.cursor_after, "batch": batch_id})
