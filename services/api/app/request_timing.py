"""Request timings without recording SQL, parameters, URLs, or user content."""
from contextvars import ContextVar
from dataclasses import dataclass
from time import perf_counter

from sqlalchemy import event
from sqlalchemy.engine import Engine


@dataclass
class RequestTiming:
    sql_seconds: float = 0
    queries: int = 0


current_timing: ContextVar[RequestTiming | None] = ContextVar('request_timing', default=None)


@event.listens_for(Engine, 'before_cursor_execute')
def before_query(connection, cursor, statement, parameters, context, executemany):
    timing = current_timing.get()
    if timing is not None:
        context.request_timer = (timing, perf_counter())


@event.listens_for(Engine, 'after_cursor_execute')
def after_query(connection, cursor, statement, parameters, context, executemany):
    timer = getattr(context, 'request_timer', None)
    if timer is not None:
        timing, started = timer
        timing.sql_seconds += perf_counter() - started
        timing.queries += 1


async def request_timing(request, call_next):
    # The mutable record follows FastAPI's context into its worker thread.
    # SQL acquisition/planning/I/O is visible alongside total application time;
    # pool checkout, result conversion, and encoding remain in the difference.
    timing = RequestTiming()
    token = current_timing.set(timing)
    started = perf_counter()
    try:
        response = await call_next(request)
        response.headers['Server-Timing'] = (
            f'app;dur={(perf_counter() - started) * 1000:.1f}, '
            f'sql;dur={timing.sql_seconds * 1000:.1f}, '
            f'queries;desc="{timing.queries}"'
        )
        return response
    finally:
        current_timing.reset(token)
