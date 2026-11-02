"""Durable task ownership. All result transactions are fenced before commit."""

import json
from contextlib import contextmanager
from contextvars import ContextVar
from uuid import uuid4

from sqlalchemy import event, text
from sqlalchemy.exc import OperationalError

_ownership = ContextVar('job_ownership', default=None)
_pause = ContextVar('job_pause', default=None)


class JobPaused(BaseException):
    """Control flow, not a failed job; bypass handler error/retry catch blocks."""


def checkpoint():
    stop = _pause.get()
    if stop is not None and stop.is_set():
        raise JobPaused()


def interruptible_wait(seconds):
    import time
    stop = _pause.get()
    if stop is None:
        time.sleep(seconds)
    elif stop.wait(seconds):
        raise JobPaused()


class LeaseLost(RuntimeError):
    pass


def assert_owner(db, job):
    owned = db.execute(text("""SELECT id FROM jobs WHERE id=:id AND lease_token=:lease_token
        AND status='running' AND lease_expires_at>clock_timestamp() FOR UPDATE"""), job).first()
    if not owned:
        raise LeaseLost(str(job['id']))


def install_fence(engine):
    @event.listens_for(engine, 'rollback')
    def after_rollback(connection):
        connection.info.pop('job_acknowledged', None)

    @event.listens_for(engine, 'commit')
    def before_commit(connection):
        job = _ownership.get()
        try:
            if job is not None and not connection.info.get('job_acknowledged'):
                assert_owner(connection, job)
        except BaseException:
            connection.info.pop('job_acknowledged', None)
            # A commit-event exception can deactivate SQLAlchemy's transaction
            # before Connection.close() rolls back the DBAPI transaction. Manual
            # commits otherwise return INERROR/INTRANS connections to the pool.
            try:
                connection.connection.rollback()
            except Exception:  # noqa: BLE001 - discard any connection whose rollback failed
                connection.invalidate()
            raise
        connection.info.pop('job_acknowledged', None)


@contextmanager
def owned(job, stop=None):
    token = _ownership.set(job)
    pause_token = _pause.set(stop)
    try:
        yield
    finally:
        _ownership.reset(token)
        _pause.reset(pause_token)


def claim(engine, worker, resource='db', *, prefer_ai_intelligence=None, prefer_ai_profiles=None):
    with engine.begin() as db:
        db.execute(text("""UPDATE jobs SET status='failed',error='Lease expired after maximum attempts',
            completed_at=now(),lease_token=NULL,lease_expires_at=NULL
            WHERE resource_class=:resource AND status='running' AND lease_expires_at<now() AND attempts>=5"""),
            {'resource': resource})
        candidate = db.execute(text("""SELECT j.id,j.payload FROM jobs j
            WHERE j.resource_class=:resource AND j.available_at<=now()
              AND (j.status='queued' OR (j.status='running' AND
                   coalesce(j.lease_expires_at,j.locked_at+interval '15 minutes')<now()))
              AND (:resource<>'projection' OR NOT EXISTS (
                SELECT 1 FROM projection_inbox e WHERE e.provider=j.payload->>'provider'
                  AND e.projected_at IS NULL
                  AND e.id<CAST(j.payload->>'event_id' AS bigint)
                  AND (cardinality(e.subject_keys)=0 OR jsonb_array_length(coalesce(j.payload->'subjects','[]'::jsonb))=0
                    OR e.subject_keys && ARRAY(SELECT jsonb_array_elements_text(j.payload->'subjects')))))
              AND (:resource<>'media' OR NOT EXISTS (
                SELECT 1 FROM jobs r WHERE r.resource_class='media' AND r.status='running'
                  AND coalesce(r.lease_expires_at,r.locked_at+interval '15 minutes')>now() AND r.id<>j.id
                  AND (r.payload->>'entity_type',r.payload->>'entity_id',r.payload->>'role')=
                      (j.payload->>'entity_type',j.payload->>'entity_id',j.payload->>'role')))
            ORDER BY CASE WHEN :resource='ai' AND CAST(:prefer AS boolean) IS NOT NULL THEN
                CASE WHEN (j.job_type NOT IN ('ai.post_media','ai.post_media_one','ai.story_media_one'))
                    = CAST(:prefer AS boolean) THEN 1 ELSE 0 END ELSE 0 END DESC,
                CASE WHEN :resource='ai' AND CAST(:profiles AS boolean) IS NOT NULL THEN
                    CASE WHEN (j.job_type IN ('ai.profiles','ai.profile'))=CAST(:profiles AS boolean)
                        THEN 1 ELSE 0 END ELSE 0 END DESC,
                j.priority DESC,j.available_at,j.id FOR UPDATE OF j SKIP LOCKED LIMIT 1"""),
            {'resource': resource, 'prefer': prefer_ai_intelligence, 'profiles': prefer_ai_profiles}).mappings().first()
        if not candidate:
            return None
        if resource == 'media':
            # Different URLs can refer to the same attachment slot. Serialize
            # their claims, not their network waits or unrelated attachments.
            slot = {key: candidate['payload'][key] for key in ('entity_type', 'entity_id', 'role')}
            key = json.dumps(slot, sort_keys=True)
            if not db.execute(text('SELECT pg_try_advisory_xact_lock(hashtextextended(:key,0))'), {'key': key}).scalar_one():
                return None
            if db.execute(text("""SELECT 1 FROM jobs WHERE resource_class='media' AND status='running'
                AND coalesce(lease_expires_at,locked_at+interval '15 minutes')>now() AND id<>:id
                AND payload->>'entity_type'=:entity_type AND payload->>'entity_id'=:entity_id
                AND payload->>'role'=:role LIMIT 1"""), {**slot, 'id': candidate['id']}).first():
                return None
        row = db.execute(text("""UPDATE jobs SET status='running',locked_at=now(),locked_by=:worker,
          attempts=attempts+1,lease_token=:token,heartbeat_at=now(),lease_expires_at=now()+interval '2 minutes'
          WHERE id=:id RETURNING *"""),
          {'id': candidate['id'], 'worker': worker, 'token': uuid4()}).mappings().one()
        return dict(row)


def heartbeat(engine, job):
    try:
        with engine.begin() as db:
            return bool(db.execute(text("""UPDATE jobs SET heartbeat_at=now(),lease_expires_at=now()+interval '2 minutes'
                WHERE id=:id AND lease_token=:lease_token AND status='running'
                  AND lease_expires_at>clock_timestamp()"""), job).rowcount)
    except OperationalError as error:
        if getattr(error.orig, 'sqlstate', None) != '55P03':
            raise
        # Media's cross-store result fence holds this row until commit. A brief
        # lock timeout is not lease loss. Observe committed ownership without
        # renewing it: an expired lease or the hard execution budget still ends
        # a stuck child, and a replacement token can never pass this check.
        with engine.connect() as db:
            return db.execute(text("""SELECT 1 FROM jobs WHERE id=:id AND lease_token=:lease_token
                AND status='running' AND lease_expires_at>clock_timestamp()"""), job).first() is not None


def finish(db, job, error=None, terminal=False):
    assert_owner(db, job)
    row = db.execute(text("""UPDATE jobs SET
        status=CASE WHEN rerun_requested THEN 'queued' WHEN CAST(:error AS text) IS NULL THEN 'complete'
                    WHEN :terminal OR attempts>=5 THEN 'failed' ELSE 'queued' END,
        available_at=CASE WHEN rerun_requested THEN now() ELSE now()+interval '1 minute' END,
        attempts=CASE WHEN rerun_requested THEN 0 ELSE attempts END,
        completed_at=CASE WHEN CAST(:error AS text) IS NULL THEN now() END,error=:error,
        rerun_requested=false,lease_token=NULL,lease_expires_at=NULL,locked_by=NULL,locked_at=NULL
        WHERE id=:id AND lease_token=:lease_token RETURNING id"""),
                     {**job, 'error': error, 'terminal': terminal}).first()
    # Connection.info survives pooling; clear this immediately on commit/rollback.
    db.info['job_acknowledged'] = True
    return row is not None


def enqueue(db, kind, payload, *, refresh=False, priority=0):
    """Atomic insert/promote; a refresh while running survives as a follow-up."""
    return db.execute(text("""INSERT INTO jobs(job_type,payload,priority) VALUES(:kind,CAST(:payload AS jsonb),:priority)
        ON CONFLICT(job_type,dedupe_key) WHERE status IN ('queued','running') AND dedupe_key IS NOT NULL
        DO UPDATE SET available_at=CASE WHEN :refresh AND jobs.status='queued' THEN now() ELSE jobs.available_at END,
          priority=greatest(jobs.priority,EXCLUDED.priority),
          rerun_requested=jobs.rerun_requested OR (:refresh AND jobs.status='running')
        RETURNING id"""), {'kind': kind, 'payload': json.dumps(payload), 'refresh': refresh, 'priority': priority}).scalar_one()


def commit_result(db):
    """Let the worker acknowledge the final result in this same transaction."""
    if not db.info.get('worker_job'):
        db.commit()


def current_job():
    return _ownership.get()


def relinquish(db, job):
    """Only after the child has exited: retry a deployment pause without penalty."""
    assert_owner(db, job)
    db.execute(text("""UPDATE jobs SET status='queued',available_at=now(),attempts=greatest(0,attempts-1),
        error=NULL,completed_at=NULL,lease_token=NULL,lease_expires_at=NULL,heartbeat_at=NULL,
        locked_by=NULL,locked_at=NULL WHERE id=:id AND lease_token=:lease_token"""), job)
    db.info['job_acknowledged'] = True


def defer(db, job, reason, delay_seconds=21600):
    """Wait for a dependency without consuming retries or reporting success.

    OCR completion promotes queued visual jobs immediately. Preserve its wakeup
    if the result arrived while this job was still assembling its inputs.
    """
    assert_owner(db, job)
    db.execute(text("""UPDATE jobs SET status='queued',
        available_at=CASE WHEN rerun_requested THEN now() ELSE now()+make_interval(secs=>:delay) END,
        attempts=greatest(0,attempts-1),error=:reason,completed_at=NULL,
        lease_token=NULL,lease_expires_at=NULL,heartbeat_at=NULL,locked_by=NULL,locked_at=NULL,
        rerun_requested=false WHERE id=:id AND lease_token=:lease_token"""),
        {**job, 'reason': reason, 'delay': delay_seconds})
    db.info['job_acknowledged'] = True
