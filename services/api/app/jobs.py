"""Serialize heavy NAS disk work with a fair, low I/O priority.

Every background importer, backfill and maintenance command shares one lock
file under STAGING_ROOT, so at most one of them streams the RAID1 disks at a
time while the API keeps normal priority. Manual backfills run through the CLI:

    python -m app.jobs app.archive_media --instagram-root /data/staging/...
    python -m app.jobs -c "from app.x import y; y()"
"""

import argparse
import ctypes
import fcntl
import json
import os
import platform
import runpy
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path

IOPRIO_SET = {"x86_64": 251, "aarch64": 30}
IOPRIO_WHO_PROCESS = 1
IOPRIO_CLASS_BE = 2
IOPRIO_BE_LEVEL = 7
_lowered = False
_depth = 0
_thread_lock = threading.RLock()


def lock_path() -> Path:
    return Path(os.environ.get("STAGING_ROOT", "/data/staging")) / ".heavy-io.lock"


def lower_priority() -> None:
    """Lowest best-effort I/O class and nice 10, without starving on a busy NAS."""
    global _lowered
    if _lowered:
        return
    os.nice(10)
    if sys.platform == "linux":
        libc = ctypes.CDLL(None, use_errno=True)
        result = libc.syscall(
            ctypes.c_long(IOPRIO_SET[platform.machine()]),
            ctypes.c_int(IOPRIO_WHO_PROCESS),
            ctypes.c_int(0),
            ctypes.c_int((IOPRIO_CLASS_BE << 13) | IOPRIO_BE_LEVEL),
        )
        if result < 0:
            raise OSError(ctypes.get_errno(), "ioprio_set failed")
    _lowered = True


class ShutdownRequested(RuntimeError):
    """Shutdown before entering a new disk-work critical section."""


@contextmanager
def heavy_io(name: str):
    """Thread and process exclusive, reentrant only for the owning thread."""
    from .runtime_status import STOP

    while not _thread_lock.acquire(timeout=0.2):
        if STOP.is_set():
            raise ShutdownRequested("Stopped while waiting for heavy I/O permission")
    try:
        with _heavy_io(name):
            yield
    finally:
        _thread_lock.release()


@contextmanager
def _heavy_io(name: str):
    """Hold the NAS-wide heavy I/O lock; nested use in one process is a no-op."""
    global _depth
    if _depth:
        _depth += 1
        try:
            yield
        finally:
            _depth -= 1
        return
    from .runtime_status import STOP

    if STOP.is_set():
        raise ShutdownRequested("Stopped before heavy I/O work")
    path = lock_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            from .runtime_status import progress
            progress(stage='waiting_io')
            handle.seek(0)
            holder = handle.read().strip()
            print(json.dumps({"heavy_io_waiting": name, "holder": holder}), flush=True)
            started = time.monotonic()
            while True:
                if STOP.wait(0.2):
                    raise ShutdownRequested("Stopped while waiting for heavy I/O permission")
                try:
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    pass
            print(json.dumps({"heavy_io_acquired": name,
                              "waited_seconds": round(time.monotonic() - started, 1)}), flush=True)
            progress(stage='writing')
        if STOP.is_set():
            raise ShutdownRequested("Stopped before heavy I/O work")
        handle.seek(0)
        handle.truncate()
        handle.write(json.dumps({"name": name, "pid": os.getpid(), "since": time.time()}))
        handle.flush()
        _depth = 1
        try:
            yield
        finally:
            _depth = 0
            handle.seek(0)
            handle.truncate()
            handle.flush()
            fcntl.flock(handle, fcntl.LOCK_UN)


def yield_heavy_io(pause: float = 2.0) -> None:
    """Between batches of a long backfill: let importers waiting on the lock run."""
    time.sleep(pause)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("-c", dest="code", help="Python source to execute under the lock")
    parser.add_argument("module", nargs="?", help="module to run as __main__ under the lock")
    parser.add_argument("args", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    if bool(args.code) == bool(args.module):
        parser.error("pass exactly one of -c CODE or MODULE")
    lower_priority()
    name = args.module or "inline"
    with heavy_io(name):
        if args.code:
            exec(compile(args.code, "<app.jobs>", "exec"), {"__name__": "__main__"})  # noqa: S102 - operator shell input, like python -c
        else:
            sys.argv = [args.module, *args.args]
            runpy.run_module(args.module, run_name="__main__", alter_sys=True)


if __name__ == "__main__":
    main()
