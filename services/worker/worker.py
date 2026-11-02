"""Small PostgreSQL-backed worker used for imports and aggregate refreshes."""

import multiprocessing
import os
import signal
import socket
import threading
import time
from contextlib import nullcontext
from datetime import UTC, datetime

from app import job_queue
from app.ai.schedule import enqueue_due as enqueue_due_ai
from app.jobs import heavy_io
from app.remote_media import (
    download_image,
    enqueue_due_media,
    materialize_image,
    media_state,
    needs_download,
)
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql+psycopg://crm:crm@localhost:5432/crm")
WORKER_ID = f"{socket.gethostname()}:{os.getpid()}"
engine = create_engine(DATABASE_URL, pool_pre_ping=True, pool_size=2, max_overflow=1, pool_timeout=5,
                       connect_args={"options": make_url(DATABASE_URL).query.get("options", "") + " -c lock_timeout=5000 -c statement_timeout=60000"})
job_queue.install_fence(engine)
RESOURCE = os.environ.get("WORKER_RESOURCE", "db")
STOP = threading.Event()
_ai_claims = 0


def claim_job():
    global _ai_claims
    if RESOURCE != 'ai':
        return job_queue.claim(engine, WORKER_ID, RESOURCE)
    # Two profile turns, one other intelligence turn, then one visual turn.
    # Old reconnect backfills must not starve profiles after visual work yields.
    turn = _ai_claims % 4
    job = job_queue.claim(engine, WORKER_ID, RESOURCE, prefer_ai_intelligence=turn != 3,
                          prefer_ai_profiles=(turn < 2) if turn != 3 else None)
    if job:
        _ai_claims += 1
    return job


def finish_job(job, error=None, terminal=False):
    with engine.begin() as db:
        job_queue.finish(db, job, error, terminal)


def execute_job(job):
    if job['job_type'] == 'source.project':
        from app.projection_feed import project_event
        with engine.begin() as connection:
            from sqlalchemy.orm import Session
            with Session(bind=connection) as db:
                project_event(db, job['payload'])
                db.flush()
                job_queue.finish(connection, job)
        return
    if job["job_type"].startswith("ai."):
        from app.ai.jobs import run_job

        # Model calls take seconds to minutes; they manage their own short transactions.
        print({"job": job["job_type"], "result": run_job(engine, job)}, flush=True)
        return
    downloaded = None
    if job["job_type"] == "media.download":
        with engine.connect() as lookup:
            download_required=needs_download(lookup,job['payload'])
            media_state(lookup,job['payload'],'downloading');lookup.commit()
        if download_required:
            downloaded = download_image(job['payload']['url'], os.environ.get('MEDIA_ROOT','/data/media'))
    permit = (heavy_io('media materialization')
              if job['job_type'] == 'media.download' and os.environ.get('APP_ENV') != 'desktop'
              else nullcontext())
    with permit, engine.begin() as db:
        if job["job_type"] == "media.download":
            # Cross-store materialization needs an early fence. CRM-only work
            # is fenced by finish below, allowing its independent heartbeats
            # to renew while a potentially slow result transaction runs.
            job_queue.assert_owner(db, job)
            materialize_image(db, job["payload"], os.environ.get("MEDIA_ROOT", "/data/media"), downloaded)
        elif job["job_type"] == "media.dimensions":
            from app.media_dimensions import record_dimensions

            record_dimensions(db, job["payload"]["media_id"])
        elif job["job_type"] == "metrics.refresh":
            payload = job["payload"] or {}
            watermark = payload.get("batch_id")
            # Global counts are sampled at most once per ten minutes. Each job
            # still refreshes only the profiles touched by its source batch.
            refresh_global = not db.execute(text("SELECT 1 FROM global_metric_snapshots WHERE computed_at>now()-interval '10 minutes' LIMIT 1")).scalar_one_or_none()
            metrics = {
                "global.profile_count": db.execute(
                    text("SELECT count(*) FROM profiles WHERE archived_at IS NULL")
                ).scalar_one(),
                "global.timeline_count": db.execute(
                    text("""
                      SELECT (SELECT count(*) FROM social_posts) +
                             (SELECT count(*) FROM social_stories) +
                             (SELECT count(*) FROM activities WHERE activity_type NOT IN ('wechat_direct_summary','wechat_group_summary'))
                    """)
                ).scalar_one(),
                f"provider.{payload.get('source', 'unknown')}.coverage": db.execute(
                    text("""
                      SELECT count(DISTINCT (stream,external_id)) FROM source_observations
                      WHERE source = :source AND state IN ('new', 'changed', 'unchanged')
                    """),
                    {"source": payload.get("source")},
                ).scalar_one(),
            } if refresh_global else {}
            for metric_key, value in metrics.items():
                db.execute(text("""
                  INSERT INTO global_metric_snapshots(metric_key, value_json, source_watermark, freshness_state)
                  VALUES (:metric_key, CAST(:value AS jsonb), :watermark, 'fresh')
                """), {"metric_key": metric_key, "value": str(value), "watermark": watermark})
            for profile_id in payload.get("affected_profiles", []):
                profile_timeline = db.execute(
                    text("""
                      SELECT (SELECT count(*) FROM social_posts WHERE profile_id = :profile_id) +
                             (SELECT count(*) FROM social_stories WHERE profile_id = :profile_id) +
                             (SELECT count(*) FROM activity_participants ap JOIN activities a ON a.id=ap.activity_id
                              WHERE ap.profile_id = :profile_id AND a.activity_type NOT IN ('wechat_direct_summary','wechat_group_summary'))
                    """),
                    {"profile_id": profile_id},
                ).scalar_one()
                identity_count = db.execute(
                    text("SELECT count(*) FROM identities WHERE profile_id = :profile_id AND status = 'active'"),
                    {"profile_id": profile_id},
                ).scalar_one()
                for metric_key, value in {
                    "timeline.count": profile_timeline,
                    "identity.count": identity_count,
                }.items():
                    db.execute(
                        text("""
                          INSERT INTO profile_metrics_current
                            (profile_id, metric_key, value_json, source_watermark, computed_at, freshness_state)
                          VALUES (:profile_id, :metric_key, CAST(:value AS jsonb), :watermark, now(), 'fresh')
                          ON CONFLICT (profile_id, metric_key) DO UPDATE SET
                            value_json = EXCLUDED.value_json,
                            source_watermark = EXCLUDED.source_watermark,
                            computed_at = EXCLUDED.computed_at,
                            freshness_state = EXCLUDED.freshness_state
                        """),
                        {
                            "profile_id": profile_id,
                            "metric_key": metric_key,
                            "value": str(value),
                            "watermark": watermark,
                        },
                    )
        else:
            raise ValueError('Unsupported task type: ' + job['job_type'])
        job_queue.finish(db, job)


def child_job(job, pause=None):
    # A separate process gives every handler a hard execution deadline. No live
    # SQLAlchemy connection is shared between parent and child.
    engine.dispose(close=False)
    signal.signal(signal.SIGTERM, signal.SIG_DFL)
    try:
        with job_queue.owned(job, pause):
            job_queue.checkpoint()
            for attempt in range(3):
                try:
                    execute_job(job)
                    break
                except Exception as error:
                    code = getattr(getattr(error, 'orig', None), 'sqlstate', None) or getattr(error, 'sqlstate', None)
                    if code not in ('40P01', '40001', '55P03') or attempt == 2 or job['job_type'].startswith('ai.'):
                        raise
                    # Retry a fresh WHOLE transaction, without synchronizing
                    # competing workers into the same retry window.
                    import random
                    print({'transaction_retry': code, 'attempt': attempt + 1}, flush=True)
                    job_queue.interruptible_wait(random.uniform(0.05, 0.2) * 2 ** attempt)
    except (job_queue.LeaseLost, job_queue.JobPaused):
        return
    except Exception as exc:  # noqa: BLE001 - record task failure before exiting
        from urllib.error import HTTPError
        terminal = isinstance(exc, HTTPError) and exc.code in (401, 403, 404, 410)
        try:
            with job_queue.owned(job), engine.begin() as db:
                if job['job_type'] == 'media.download':
                    media_state(db, job['payload'], 'expired' if terminal else 'failed', type(exc).__name__)
                job_queue.finish(db, job, str(exc)[:2000], terminal)
        except job_queue.LeaseLost:
            pass


def worker_heartbeat(job=None, stopped=False):
    with engine.begin() as db:
        db.execute(text("""INSERT INTO worker_heartbeats(worker_id,resource_class,job_id,draining,stopped_at)
            VALUES(:worker,:resource,:job,:draining,CASE WHEN :stopped THEN now() END) ON CONFLICT(worker_id) DO UPDATE
            SET heartbeat_at=now(),job_id=excluded.job_id,draining=excluded.draining,stopped_at=excluded.stopped_at"""),
                   {'worker': WORKER_ID, 'resource': RESOURCE, 'job': job['id'] if job else None,
                    'draining': STOP.is_set(), 'stopped': stopped})


def run(poll_seconds: float = 2.0):
    signal.signal(signal.SIGTERM, lambda *_: STOP.set())
    signal.signal(signal.SIGINT, lambda *_: STOP.set())
    last_heartbeat = 0.0
    while not STOP.is_set():
        process = None
        try:
            if time.monotonic() - last_heartbeat >= 30:
                worker_heartbeat()
                last_heartbeat = time.monotonic()
            job = claim_job()
            if not job:
                STOP.wait(poll_seconds)
                continue
            context = multiprocessing.get_context('spawn')
            pause = context.Event()
            process = context.Process(target=child_job, args=(job, pause))
            process.start()
            started = time.monotonic()
            # A person's full history may need several cached model batches.
            default_budget = '3600' if job['job_type'] in ('ai.profile', 'ai.person_week') else '900' if RESOURCE == 'ai' else '300'
            budget = int(os.environ.get('JOB_TIMEOUT_SECONDS', default_budget))
            paused_at = None
            last_pulse = started
            while process.is_alive():
                if STOP.is_set() and paused_at is None:
                    pause.set()
                    paused_at = time.monotonic()
                process.join(1)
                if not process.is_alive():
                    break
                now = time.monotonic()
                lost = False
                if now - last_pulse >= 30:
                    worker_heartbeat(job)
                    lost = not job_queue.heartbeat(engine, job)
                    last_pulse = now
                # Allow the current model call/transaction to reach a checkpoint,
                # with a bound even for a stuck external service. Child death is
                # confirmed before its token can be released.
                if now - started > budget or lost or (paused_at is not None and now - paused_at >= 240):
                    process.terminate()
                    process.join(5)
                    if process.is_alive():
                        process.kill()
                        process.join()
                    break
            # A clean child has already acknowledged. Crashes/deadlines are retried.
            try:
                if paused_at is not None:
                    with engine.begin() as db:
                        job_queue.relinquish(db, job)
                else:
                    finish_job(job, 'Worker child exited before acknowledgement')
            except job_queue.LeaseLost:
                pass
        except Exception as exc:  # noqa: BLE001 - recover from temporary DB loss
            print({'worker_error': type(exc).__name__, 'detail': str(exc)[:500]}, flush=True)
            STOP.wait(5)
        finally:
            if process is not None and process.is_alive():
                process.terminate()
                process.join(5)
                if process.is_alive():
                    process.kill()
                    process.join()
    worker_heartbeat(stopped=True)


def schedule(poll_seconds=30):
    signal.signal(signal.SIGTERM, lambda *_: STOP.set())
    while not STOP.is_set():
        from app.data_versions import compact
        for name, action in (
            ('media', enqueue_due_media), ('ai', lambda db: enqueue_due_ai(db, datetime.now(UTC))),
            ('cache_versions', compact),
        ):
            try:
                with engine.begin() as db:
                    action(db)
            except Exception as exc:  # noqa: BLE001 - independent scheduling lanes
                print({'scheduler_error': name, 'detail': str(exc)[:500]}, flush=True)
        worker_heartbeat()
        STOP.wait(poll_seconds)
    worker_heartbeat(stopped=True)


if __name__ == "__main__":
    from app.jobs import lower_priority
    lower_priority()
    (schedule if RESOURCE == 'scheduler' else run)()
