"""Worker entry point for model jobs."""

from sqlalchemy.orm import Session

from ..config import get_settings
from ..job_queue import defer, finish


def run_job(engine, job: dict) -> dict:
    from . import communities, digest, person_weekly, post_media, profiles, reconnect

    runners = {"ai.community_names": communities.name_all, "ai.reconnect": reconnect.refresh,
               "ai.reconnect_one": reconnect.refresh_one, "ai.digest": digest.build_week,
               "ai.person_weeks": person_weekly.enqueue_week, "ai.person_week": person_weekly.build_one,
               "ai.post_media": post_media.scan, "ai.post_media_one": post_media.read_post,
               "ai.story_media_one": post_media.read_story,
               "ai.profiles": profiles.enqueue_all, "ai.profile": profiles.refresh_one}
    with Session(engine) as db:
        db.info['worker_job'] = job
        result = (runners[job['job_type']](db, job['payload'] or {}) if get_settings().ai_enabled
                  else {'skipped': 'AI_ENABLED is off'})
        if result.get('waiting_for_image_reading'):
            defer(db.connection(), job, 'Waiting for combined image/OCR reading')
        else:
            finish(db.connection(), job)
        db.commit()
        return result
