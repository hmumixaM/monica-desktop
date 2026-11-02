"""Independent source phase health and cooperative process shutdown."""
import json
import logging
import os
import signal
import threading
import time
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import text

STOP = threading.Event()
_details = ContextVar('source_phase_details', default=None)


class IdleWait:
    """A wake notification ends idle time, never interrupts an active import."""

    def __init__(self):
        configured = os.environ.get('MONICA_DESKTOP_WAKE_FILE')
        self.path = Path(configured) if configured else None
        self.observed = self.signature()

    def signature(self):
        try:
            return self.path.stat().st_mtime_ns if self.path else None
        except FileNotFoundError:
            return None

    def wait(self, seconds):
        if self.path is None:
            return STOP.wait(seconds)
        deadline = time.time() + seconds
        while not STOP.is_set():
            current = self.signature()
            if current != self.observed:
                self.observed = current
                return False
            remaining = deadline - time.time()
            if remaining <= 0:
                return False
            STOP.wait(min(1, remaining))
        return True


def progress(**fields):
    """Advance only on actual work, never on a process heartbeat."""
    details = _details.get()
    if details is not None:
        details.update(fields, last_progress_at=datetime.now(UTC).isoformat())


def install_shutdown():
    signal.signal(signal.SIGTERM, lambda *_: STOP.set())
    signal.signal(signal.SIGINT, lambda *_: STOP.set())


def record(source, phase, status, details=None, error=None):
    from .db import engine
    with engine.begin() as db:
        db.execute(text("""INSERT INTO source_runtime(source,phase,status,started_at,last_success_at,next_retry_at,failures,error,details)
            VALUES(:source,:phase,:status,now(),CASE WHEN :status IN ('success','unchanged') THEN now() END,
                CASE WHEN :status IN ('failed','busy','pending') THEN now()+interval '1 minute' END,
                CASE WHEN :status='failed' THEN 1 ELSE 0 END,:error,CAST(:details AS jsonb))
            ON CONFLICT(source,phase) DO UPDATE SET status=excluded.status,heartbeat_at=now(),
                started_at=CASE WHEN :status='running' THEN now() ELSE source_runtime.started_at END,
                last_success_at=coalesce(excluded.last_success_at,source_runtime.last_success_at),
                next_retry_at=excluded.next_retry_at,error=excluded.error,details=excluded.details,
                failures=CASE WHEN :status='failed' THEN source_runtime.failures+1
                              WHEN :status IN ('success','unchanged') THEN 0 ELSE source_runtime.failures END"""),
            {'source': source, 'phase': phase, 'status': status, 'error': str(error)[:2000] if error else None,
             'details': json.dumps(details or {}, default=str)})


@contextmanager
def phase(source, name):
    record(source, name, 'running')
    done = threading.Event()
    details = {}
    token = _details.set(details)
    def pulse():
        from .db import engine
        while not done.wait(5 if os.environ.get('APP_ENV') == 'desktop' else 30):
            try:
                with engine.begin() as db:
                    db.execute(text('UPDATE source_runtime SET heartbeat_at=now(),details=CAST(:details AS jsonb) WHERE source=:source AND phase=:phase'),
                               {'source': source, 'phase': name, 'details': json.dumps(dict(details), default=str)})
            except Exception:
                # Expired heartbeats remain visible. The actual operation owns
                # its retry/checkpoint policy when the database returns.
                logging.getLogger(__name__).exception('Source heartbeat failed: %s/%s', source, name)
    thread = threading.Thread(target=pulse, daemon=True)
    thread.start()
    try:
        yield details
    except BaseException as exc:
        from .jobs import ShutdownRequested
        if isinstance(exc, ShutdownRequested):
            details['stopped_at_checkpoint'] = True
            record(source, name, 'pending', details)
        else:
            record(source, name, 'failed', details, exc)
        raise
    else:
        state = details.get('state', 'success')
        if details.get('failed'):
            state = 'failed'
        elif details.get('stopped_at_checkpoint'):
            state = 'pending'
        record(source, name, state if state in ('failed', 'unchanged', 'busy', 'pending') else 'success', details)
    finally:
        done.set()
        thread.join(timeout=6)
        _details.reset(token)
