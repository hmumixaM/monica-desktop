"""Recurring model jobs: each enqueues at most once per period, at a local time."""

from dataclasses import dataclass
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import text

from ..config import get_settings


@dataclass(frozen=True)
class Schedule:
    job_type: str
    at: time
    weekday: int | None = None  # Monday is 0; None runs daily
    hourly: bool = False  # every hour at `at.minute`

    def period(self, local: datetime) -> str | None:
        """The period this moment falls in once the run time has passed, else None."""
        if self.hourly:
            return local.strftime("%Y-%m-%dT%H") if local.minute >= self.at.minute else None
        if self.weekday is None:
            return local.date().isoformat() if local.time() >= self.at else None
        start = local.date() - timedelta(days=(local.weekday() - self.weekday) % 7)
        due = datetime.combine(start, self.at, local.tzinfo)
        return start.isoformat() if local >= due else None


SCHEDULES = (
    Schedule("ai.community_names", time(3, 0)),
    Schedule("ai.reconnect", time(6, 0)),
    Schedule("ai.digest", time(8, 0), weekday=0),
    Schedule("ai.person_weeks", time(8, 0), weekday=0),
    # Queues one `ai.profile` job per person; unchanged inputs skip the model.
    Schedule("ai.profiles", time(1, 0), weekday=6),
    # Queues one `ai.post_media_one` job per post whose images are unread or changed.
    Schedule("ai.post_media", time(0, 20), hourly=True),
)


def enqueue_due(db, now: datetime) -> list[str]:
    """Queue each schedule's job for its current period if not already queued."""
    settings = get_settings()
    if not settings.ai_enabled:
        return []
    local = now.astimezone(ZoneInfo(settings.crm_timezone))
    queued = []
    for schedule in SCHEDULES:
        period = schedule.period(local)
        if period is None:
            continue
        claimed = db.execute(text("""INSERT INTO scheduled_runs(job_type, period) VALUES(:job_type, :period)
                                     ON CONFLICT DO NOTHING RETURNING period"""),
                             {"job_type": schedule.job_type, "period": period}).scalar_one_or_none()
        if claimed:
            from ..job_queue import enqueue
            job_id = enqueue(db, schedule.job_type, {"period": period})
            if schedule.job_type == "ai.person_weeks":
                db.execute(text("UPDATE jobs SET priority=10 WHERE id=:job"), {"job": job_id})
            db.execute(text("UPDATE scheduled_runs SET job_id=:job WHERE job_type=:kind AND period=:period"),
                       {"job": job_id, "kind": schedule.job_type, "period": period})
            queued.append(schedule.job_type)
    return queued
