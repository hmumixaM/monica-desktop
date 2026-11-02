"""Model service status for the settings page."""

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.orm import Session

from ..auth import current_user
from ..config import get_settings
from ..db import get_db
from . import digest, image_ocr, person_weekly, profiles, reconnect
from .schedule import SCHEDULES

router = APIRouter()
router.include_router(reconnect.router)
router.include_router(digest.router)
router.include_router(profiles.router)
router.include_router(person_weekly.router)


def reachable(base_url: str) -> bool:
    try:
        return httpx.get(base_url.removesuffix("/v1") + "/health/liveliness", timeout=3).is_success
    except httpx.HTTPError:
        return False


@router.get("/api/v1/ai/status")
def ai_status(db: Session = Depends(get_db), _user=Depends(current_user)):
    settings = get_settings()
    if not settings.ai_enabled:
        return {"enabled": False}
    runs = db.execute(text("""SELECT DISTINCT ON (r.job_type) r.job_type, r.period, j.status, j.completed_at, j.error
        FROM scheduled_runs r LEFT JOIN jobs j ON j.id = r.job_id
        ORDER BY r.job_type, r.enqueued_at DESC""")).mappings().all()
    by_type = {row["job_type"]: dict(row) for row in runs}
    return {
        "enabled": True,
        "reachable": reachable(settings.litellm_base_url),
        "image_ocr": image_ocr.progress(db),
        "models": {"batch": settings.ai_batch_model, "writer": settings.ai_writer_model,
                   "person": settings.ai_person_model, "vision": settings.ai_vision_model},
        "jobs": [{"job_type": schedule.job_type, **{key: value for key, value in by_type.get(schedule.job_type, {}).items() if key != "job_type"}}
                 for schedule in SCHEDULES],
    }


@router.post("/api/v1/ai/jobs/{job_type}")
def run_now(job_type: str, db: Session = Depends(get_db), _user=Depends(current_user)):
    """Queue a scheduled job now, for its current period; the next scheduled run still happens."""
    settings = get_settings()
    schedule = next((item for item in SCHEDULES if item.job_type == job_type), None)
    if not settings.ai_enabled or schedule is None:
        raise HTTPException(404, "Unknown or disabled model job")
    local = datetime.now(UTC).astimezone(ZoneInfo(settings.crm_timezone))
    period = schedule.period(local.replace(hour=23, minute=59))
    from ..job_queue import enqueue
    job = enqueue(db, job_type, {"period": period}, refresh=True)
    if job_type == "ai.person_weeks":
        db.execute(text("UPDATE jobs SET priority=10 WHERE id=:job"), {"job": job})
    db.execute(text("""INSERT INTO scheduled_runs(job_type, period, job_id) VALUES(:job_type, :period, :job)
                       ON CONFLICT (job_type, period) DO UPDATE SET job_id = EXCLUDED.job_id, enqueued_at = now()"""),
               {"job_type": job_type, "period": period, "job": job})
    db.commit()
    return {"job_id": str(job), "job_type": job_type, "period": period}


@router.get("/api/v1/jobs/status")
def task_status(db: Session = Depends(get_db), _user=Depends(current_user)):
    """Queue health is independent of whether model features are enabled."""
    queues = db.execute(text("""SELECT job_type,coalesce(resource_class,job_resource(job_type)) AS resource_class,status,count(*) AS count,
        min(available_at) FILTER(WHERE status='queued' AND available_at<=now()) AS oldest_due_at,
        min(heartbeat_at) FILTER(WHERE status='running') AS oldest_heartbeat_at,
        max(completed_at) FILTER(WHERE status='complete') AS last_success_at,
        count(*) FILTER(WHERE status='running' AND lease_expires_at<now()) AS expired_leases
        FROM jobs GROUP BY job_type,2,status ORDER BY job_type,status""")).mappings().all()
    workers = db.execute(text("""SELECT worker_id,resource_class,heartbeat_at,job_id,draining,
        heartbeat_at < now()-interval '2 minutes' AS stale FROM worker_heartbeats
        WHERE stopped_at IS NULL AND heartbeat_at>now()-interval '7 days' ORDER BY resource_class,heartbeat_at DESC""")).mappings().all()
    sources = db.execute(text("""SELECT *,status='running' AND (
            heartbeat_at<now()-interval '2 minutes' OR
            coalesce((details->>'last_progress_at')::timestamptz,started_at)<now()-interval '15 minutes') AS stale,
        status='running' AND coalesce((details->>'last_progress_at')::timestamptz,started_at)
            <now()-interval '15 minutes' AS progress_stalled
        FROM source_runtime ORDER BY source,phase""")).mappings().all()
    watermarks = db.execute(text("""SELECT source,stream,updated_at AS last_import_at
        FROM source_cursors ORDER BY source,stream""")).mappings().all()
    outputs = db.execute(text("""SELECT kind,max(generated_at) AS last_generated_at,
        count(*) FILTER (WHERE generated_at>now()-interval '24 hours') AS generated_last_24h
        FROM ai_outputs GROUP BY kind ORDER BY kind""")).mappings().all()
    return {"queues": [dict(r) for r in queues], "workers": [dict(r) for r in workers], "sources": [dict(r) for r in sources],
            "watermarks": [dict(r) for r in watermarks], "outputs": [dict(r) for r in outputs],
            "scheduled_status_meaning": "Parent completion means dispatch completed; inspect child queues separately."}
class OCRControls(BaseModel):
    daily_limit: int | None = Field(default=None, ge=1, le=100000)
    enabled: bool | None = None


@router.patch("/api/v1/ai/image-ocr")
def update_image_ocr(body: OCRControls, db: Session = Depends(get_db), _user=Depends(current_user)):
    if not get_settings().ai_enabled:
        raise HTTPException(404, "AI is disabled")
    db.execute(text("""UPDATE image_ocr_settings SET daily_limit=coalesce(:limit,daily_limit),
        enabled=coalesce(:enabled,enabled),updated_at=now() WHERE id=true"""),
               {'limit': body.daily_limit, 'enabled': body.enabled})
    db.commit()
    return image_ocr.progress(db)
