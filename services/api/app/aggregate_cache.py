"""Versioned aggregate cache shared by API releases, with stale-while-revalidate.

A cached value is keyed by namespace + caller key and stamped with the data
versions of the domains it reads. A version change is a miss that recomputes
synchronously, so edits and imports are never hidden. Within one set of versions
a value older than `ttl` (time-relative content such as "last 30 days") is
served immediately and refreshed on a background thread.
"""

import functools
import hashlib
import inspect
import json
import logging
import os
import sys
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from fastapi import Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse, Response

from .data_versions import current

FORMAT = 2
DEFAULT_TTL = 900
MEMORY_ENTRIES = 512
MEMORY_BYTES = int(os.environ.get("AGGREGATE_CACHE_BYTES", "67108864"))
MAX_ENTRY_BYTES = 4 * 1024 * 1024
_memory_sizes: dict[str, int] = {}
_flights: dict[str, list] = {}
log = logging.getLogger(__name__)

_memory: OrderedDict[str, dict] = OrderedDict()
_memory_lock = threading.Lock()
_refreshing: set[str] = set()
_refresher = ThreadPoolExecutor(max_workers=1, thread_name_prefix="aggregate-refresh")


def _root() -> Path:
    return Path(os.environ.get("STAGING_ROOT", "/data/staging")) / "api-cache" / "aggregates"


def _digest(namespace: str, key: Any) -> str:
    encoded = json.dumps([FORMAT, namespace, key], ensure_ascii=False, default=str, sort_keys=True)
    return hashlib.sha256(encoded.encode()).hexdigest()


def _path(namespace: str, digest: str) -> Path:
    return _root() / namespace / f"{digest}.json"


def _load(namespace: str, digest: str) -> dict | None:
    with _memory_lock:
        record = _memory.get(digest)
        if record is not None:
            _memory.move_to_end(digest)
            return record
    path = _path(namespace, digest)
    if not path.is_file():
        return None
    record = json.loads(path.read_text())
    _remember(digest, record)
    return record


def _size(value, seen=None):
    seen = set() if seen is None else seen
    if id(value) in seen:
        return 0
    seen.add(id(value))
    size = sys.getsizeof(value)
    if isinstance(value, dict):
        size += sum(_size(k, seen) + _size(v, seen) for k, v in value.items())
    elif isinstance(value, (list, tuple)):
        size += sum(_size(v, seen) for v in value)
    return size


def _remember(digest: str, record: dict) -> None:
    size = _size(record)
    with _memory_lock:
        _memory.pop(digest, None)
        _memory_sizes.pop(digest, None)
        if size > min(MAX_ENTRY_BYTES, MEMORY_BYTES):
            return
        _memory[digest] = record
        _memory_sizes[digest] = size
        while len(_memory) > MEMORY_ENTRIES or sum(_memory_sizes.values()) > MEMORY_BYTES:
            key, _ = _memory.popitem(last=False)
            _memory_sizes.pop(key, None)


@contextmanager
def _singleflight(digest):
    with _memory_lock:
        flight = _flights.setdefault(digest, [threading.RLock(), 0])
        flight[1] += 1
    try:
        with flight[0]:
            yield
    finally:
        with _memory_lock:
            flight[1] -= 1
            if not flight[1]:
                _flights.pop(digest, None)


def _store(namespace: str, digest: str, record: dict) -> None:
    path = _path(namespace, digest)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{digest}.{os.getpid()}.{threading.get_ident()}.tmp")
    temporary.write_text(json.dumps(record, ensure_ascii=False, separators=(",", ":")))
    temporary.replace(path)
    _remember(digest, record)


def _compute(db, namespace, digest, domains, compute) -> dict:
    versions = current(db, domains)
    value = jsonable_encoder(compute(db))
    etag = hashlib.sha256(json.dumps([digest, versions, value], sort_keys=True,
                                     ensure_ascii=False, default=str).encode()).hexdigest()[:32]
    record = {"versions": versions, "computed_at": time.time(), "etag": etag, "value": value}
    # A write committed while computing would otherwise be hidden under its
    # own newer version; such a result is returned but not stored.
    if current(db, domains) == versions:
        _store(namespace, digest, record)
    return record


def _refresh_later(namespace, digest, domains, compute) -> None:
    with _memory_lock:
        if digest in _refreshing:
            return
        _refreshing.add(digest)

    def run():
        from .db import ApiSessionLocal

        try:
            with _singleflight(digest), ApiSessionLocal() as db:
                _compute(db, namespace, digest, domains, compute)
        except Exception:
            log.exception("Aggregate refresh failed: %s/%s", namespace, digest)
        finally:
            with _memory_lock:
                _refreshing.discard(digest)

    _refresher.submit(run)


@functools.cache
def source_fingerprint(func: Callable) -> str:
    """Hash of the file defining `func`: a release that changes it gets fresh entries, others keep sharing them."""
    return hashlib.sha256(Path(inspect.getsourcefile(inspect.unwrap(func))).read_bytes()).hexdigest()[:16]


# Domains advanced by background imports rather than by the person using the UI.
IMPORT_DOMAINS = ("messages", "posts", "media", "contact_events")


def cached_record(db, namespace: str, domains, key: Any, compute: Callable[[Any], Any], *,
                  ttl: int = DEFAULT_TTL, force: bool = False, stale_on_change: bool = False,
                  stale_domains=(), source: Callable | None = None) -> dict:
    """Return {"value", "etag", "versions", "computed_at"} for one aggregate.

    After a version change the previous value is served while a background
    refresh runs when `stale_on_change` is set (global aggregates too heavy for
    an interactive statement timeout), or when only `stale_domains` changed
    (imports); any other change, such as a user's edit, recomputes now.
    """
    digest = _digest(namespace, [source_fingerprint(source or compute), key])
    if not force:
        record = _load(namespace, digest)
        if record is not None:
            versions = current(db, domains)
            fresh_versions = record["versions"] == versions
            changed = {d for d in versions if record["versions"].get(d) != versions[d]}
            if fresh_versions or stale_on_change or (stale_domains and changed <= set(stale_domains)):
                if not fresh_versions or time.time() - record["computed_at"] > ttl:
                    _refresh_later(namespace, digest, domains, compute)
                return record
    with _singleflight(digest):
        record = _load(namespace, digest)
        if not force and record is not None and record["versions"] == current(db, domains):
            return record
        return _compute(db, namespace, digest, domains, compute)


def cached_aggregate(db, namespace: str, domains, key: Any, compute: Callable[[Any], Any], *,
                     ttl: int = DEFAULT_TTL, force: bool = False, source: Callable | None = None) -> Any:
    return cached_record(db, namespace, domains, key, compute, ttl=ttl, force=force, source=source)["value"]


def cached_response(request: Request, db, namespace: str, domains, key: Any,
                    compute: Callable[[Any], Any], *, ttl: int = DEFAULT_TTL,
                    stale_on_change: bool = False, stale_domains=(), source: Callable | None = None) -> Response:
    """JSON response with a version ETag; If-None-Match revalidation returns 304."""
    record = cached_record(db, namespace, domains, key, compute, ttl=ttl, stale_on_change=stale_on_change,
                           stale_domains=stale_domains, source=source)
    etag = f'W/"{record["etag"]}"'
    headers = {"ETag": etag, "Cache-Control": "private, max-age=60, stale-while-revalidate=600"}
    if etag in request.headers.get("if-none-match", ""):
        return Response(status_code=304, headers=headers)
    return JSONResponse(record["value"], headers=headers)


SESSION_ARGUMENTS = {"db", "_user", "user", "request"}


def cached_endpoint(namespace: str, domains, *, ttl: int = DEFAULT_TTL,
                    when: Callable[[dict], bool] | None = None, stale_on_change: bool = False,
                    stale_domains=IMPORT_DOMAINS, source: Callable | None = None):
    """Cache a JSON GET endpoint keyed by its non-session arguments.

    Over HTTP the response carries the version ETag and honours If-None-Match.
    Direct calls (tests, other endpoints) return the plain cached value.
    `when` limits caching to some arguments, such as a paginated first page.
    """

    def decorate(func):
        signature = inspect.signature(func)
        own_request = "request" in signature.parameters
        parameters = list(signature.parameters.values())
        if not own_request:
            parameters.append(inspect.Parameter("request", inspect.Parameter.KEYWORD_ONLY,
                                                annotation=Request, default=None))

        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            request = kwargs.get("request") if own_request else kwargs.pop("request", None)
            bound = signature.bind(*args, **kwargs)
            bound.apply_defaults()
            arguments = dict(bound.arguments)
            if when is not None and not when(arguments):
                return func(**arguments)
            key = {name: value for name, value in arguments.items() if name not in SESSION_ARGUMENTS}

            def compute(session):
                return func(**{**arguments, "db": session})

            if request is None:
                return cached_aggregate(arguments["db"], namespace, domains, key, compute, ttl=ttl, source=source or func)
            # Only HTTP reads may be served stale; direct callers get current data.
            return cached_response(request, arguments["db"], namespace, domains, key, compute, ttl=ttl,
                                   stale_on_change=stale_on_change, stale_domains=stale_domains, source=source or func)

        wrapper.__signature__ = signature.replace(parameters=parameters)
        wrapper.cache_namespace = namespace
        return wrapper

    return decorate


def clear_memory() -> None:
    with _memory_lock:
        _memory.clear()
        _memory_sizes.clear()
