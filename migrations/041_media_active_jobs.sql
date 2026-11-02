-- Daily observation IDs retain refresh history, but an unfinished download for
-- the same URL and attachment slot must not accumulate across calendar days.
-- Keep every job payload and attempt record for audit and rollback review.
LOCK TABLE jobs IN SHARE ROW EXCLUSIVE MODE;
WITH ranked AS (
  SELECT id, status, row_number() OVER (
    PARTITION BY payload->>'url', payload->>'entity_type',
                 payload->>'entity_id', payload->>'role'
    ORDER BY (status='running') DESC, payload->>'check_day' DESC,
             available_at, id
  ) AS position
  FROM jobs WHERE job_type='media.download' AND status IN ('queued','running')
)
UPDATE jobs j SET status='superseded', completed_at=now(),
  error='Duplicate pending media attachment; retained active job owns refresh'
FROM ranked r WHERE j.id=r.id AND r.position>1 AND r.status='queued';

CREATE UNIQUE INDEX IF NOT EXISTS jobs_media_active_slot_idx ON jobs (
  digest(payload->>'url','sha256'), (payload->>'entity_type'),
  (payload->>'entity_id'), (payload->>'role')
) WHERE job_type='media.download' AND status IN ('queued','running');
