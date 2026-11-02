"""Rebuild person analysis after reversible source-account ownership changes."""

from sqlalchemy import text

from ..config import get_settings
from ..job_queue import enqueue


def refresh_after_link(db, profile_ids):
    """Invalidate derived prose, then queue analysis of the new source ownership.

    A source account can be moved out of a profile containing other people, so
    copying its old Memory would risk carrying those people's facts along.
    Raw source data and link history remain untouched. Existing weekly notes
    are rebuilt for their original weeks, never relabelled as current news.
    """
    ids = sorted({str(pid) for pid in profile_ids if pid})
    weeks = list(db.execute(text("""DELETE FROM person_weekly_updates
        WHERE profile_id=ANY(CAST(:ids AS uuid[])) RETURNING week_start"""), {'ids': ids}).scalars())
    for profile_id in ids:
        db.execute(text("""DELETE FROM ai_outputs WHERE
            (subject=:id AND kind IN ('profile_memory','profile_overview','profile_updates'))
            OR (split_part(subject, ':', 1)=:id AND kind IN ('profile_memory_chunk','profile_week_chunk'))"""),
                   {'id': profile_id})
        if not get_settings().ai_enabled:
            continue
        from .person_memory import candidates
        if not candidates(db, profile_id):
            continue
        enqueue(db, 'ai.profile', {'profile_id': profile_id}, refresh=True)
        for week in sorted(set(weeks)):
            enqueue(db, 'ai.person_week', {'profile_id': profile_id, 'week_start': week.isoformat(),
                                          'subject_key': f'{profile_id}:{week}'}, refresh=True)
