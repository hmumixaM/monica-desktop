"""Per-person model jobs, spaced so imports and downloads queued meanwhile still get their turn."""

import json

from sqlalchemy import text

from ..job_queue import commit_result


def enqueue_spaced(db, job_type: str, payloads: list[dict], spacing_seconds: int,
                   key: str = "profile_id", priority: int = 0) -> int:
    """Queue one job per payload, `spacing_seconds` apart, after any of the same type still waiting.

    Starting after the existing tail keeps a second pass from interleaving with the first and halving the
    spacing. A subject (`payload[key]`) that already has one of these jobs queued or running is skipped.
    """
    db.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:kind, 0))"), {"kind": job_type})
    queued = db.execute(text("""WITH tail AS (
          SELECT greatest(now(), max(available_at) + make_interval(secs => :spacing)) AS start FROM jobs
          WHERE job_type = :job_type AND status IN ('queued', 'running')),
        fresh AS (
          SELECT t.payload, row_number() OVER (ORDER BY t.n) AS n
          FROM jsonb_array_elements(CAST(:payloads AS jsonb)) WITH ORDINALITY AS t(payload, n)
          WHERE NOT EXISTS (SELECT 1 FROM jobs j WHERE j.job_type = :job_type AND j.status IN ('queued', 'running')
                            AND j.payload->>:key = t.payload->>:key))
        INSERT INTO jobs(job_type, payload, available_at, priority)
        SELECT :job_type, fresh.payload, tail.start + make_interval(secs => :spacing * (fresh.n - 1)), :priority
        FROM fresh, tail ON CONFLICT DO NOTHING"""), {"job_type": job_type, "payloads": json.dumps(payloads), "spacing": spacing_seconds,
                                 "key": key, "priority": priority}).rowcount
    commit_result(db)
    return queued


def enqueue_story_media(db, story_id) -> int:
    """Add an asynchronous read in the media import transaction; the model runs only in the worker."""
    from ..config import get_settings

    settings = get_settings()
    if not settings.ai_enabled:
        return 0
    eligible = db.execute(text("""SELECT id FROM social_stories
        WHERE id = CAST(:id AS uuid) AND occurred_at >= CAST(:since AS date)::timestamp AT TIME ZONE :zone"""),
                          {"id": story_id, "since": settings.ai_post_media_since, "zone": settings.crm_timezone}).first()
    if not eligible:
        return 0
    # Changes arriving while a model call is running must survive its ack.
    db.execute(text("""UPDATE jobs SET rerun_requested=true WHERE job_type='ai.story_media_one'
        AND status='running' AND payload->>'story_id'=:id"""), {'id': str(story_id)})
    # available_at alone cannot jump an already-due historical backlog: claims
    # sort by priority first. Keep imports ahead of backfill in both dimensions.
    # A short grace period lets the same import attach a video's poster before it is read.
    return db.execute(text("""WITH tail AS (
        SELECT greatest(now() + interval '12 seconds', max(available_at) + interval '12 seconds') AS start
        FROM jobs WHERE job_type = 'ai.story_media_one' AND status IN ('queued', 'running')
          AND payload->>'ingested' = 'true'),
      promoted AS (
        UPDATE jobs SET available_at = tail.start, priority = greatest(priority, 10),
          payload = payload || jsonb_build_object('ingested', true)
        FROM tail WHERE job_type = 'ai.story_media_one' AND status = 'queued'
          AND payload->>'story_id' = :id AND coalesce(payload->>'ingested', 'false') <> 'true'
        RETURNING id),
      inserted AS (
        INSERT INTO jobs(job_type, payload, available_at, priority)
        SELECT 'ai.story_media_one', jsonb_build_object('story_id', CAST(:id AS text), 'ingested', true), tail.start, 10 FROM tail
        WHERE NOT EXISTS (SELECT 1 FROM jobs WHERE job_type = 'ai.story_media_one'
          AND status IN ('queued', 'running') AND payload->>'story_id' = :id)
        ON CONFLICT DO NOTHING RETURNING id)
      SELECT (SELECT count(*) FROM promoted) + (SELECT count(*) FROM inserted)"""),
                      {"id": str(story_id)}).scalar_one()


def enqueue_visual_result(db, media_id) -> None:
    """Publish a completed combined reading asynchronously; no second model call.

    A result arriving during assembly must survive the active job's ack. Keep
    these jobs ahead of the historical scan, in the OCR completion transaction.
    """
    links = db.execute(text("""SELECT DISTINCT entity_type,entity_id FROM media_links
        WHERE media_id=CAST(:id AS uuid) AND entity_type IN ('social_post','social_story')
          AND role ~ '^(media-[0-9]+(-poster)?|thumbnail-(media-)?[0-9]+)$'"""), {'id': media_id}).all()
    for kind, entity_id in links:
        key = 'story_id' if kind == 'social_story' else 'post_id'
        job_type = 'ai.story_media_one' if kind == 'social_story' else 'ai.post_media_one'
        params = {'id': str(entity_id), 'key': key, 'kind': job_type}
        db.execute(text("""UPDATE jobs SET rerun_requested=true WHERE job_type=:kind
            AND status='running' AND payload->>:key=:id"""), params)
        db.execute(text("""UPDATE jobs SET priority=greatest(priority,10),available_at=least(available_at,now())
            WHERE job_type=:kind AND status='queued' AND payload->>:key=:id"""), params)
        db.execute(text("""INSERT INTO jobs(job_type,payload,priority)
            SELECT :kind,jsonb_build_object(CAST(:key AS text),CAST(:id AS text)),10
            WHERE NOT EXISTS (SELECT 1 FROM jobs WHERE job_type=:kind AND status IN ('queued','running')
              AND payload->>:key=:id) ON CONFLICT DO NOTHING"""), params)
