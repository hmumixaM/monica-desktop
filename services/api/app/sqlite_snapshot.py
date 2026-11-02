"""Bounded snapshots of externally refreshed SQLite files on read-only mounts.

Never open the source's shared-memory file. Decrypted archives can replace the
main file without rebuilding SHM; SQLite's backup loop can then spin forever.
Copy a stable main/WAL pair privately and let SQLite rebuild its own SHM.
This module's subprocess entry point deliberately uses only the standard library.
"""
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from contextlib import closing
from pathlib import Path


def _signature(path):
    try:
        stat = path.stat()
        return stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns
    except FileNotFoundError:
        return None


def build(source: Path, directory: Path):
    progress_path = directory / 'progress.json'
    last_report = 0
    last_stage = None

    def report(**details):
        nonlocal last_report, last_stage
        if time.monotonic() - last_report < 1 and details.get('stage') == last_stage:
            return
        temporary = progress_path.with_suffix('.tmp')
        temporary.write_text(json.dumps(details))
        temporary.replace(progress_path)
        last_report = time.monotonic()
        last_stage = details.get('stage')

    private = directory / 'source.db'
    paths = [source, Path(str(source) + '-wal')]
    for attempt in range(3):
        before = [_signature(path) for path in paths]
        if before[0] is None:
            raise FileNotFoundError(source)
        for path, signature in zip(paths, before):
            target = private if path == source else Path(str(private) + '-wal')
            target.unlink(missing_ok=True)
            if signature is None:
                continue
            with path.open('rb') as original, target.open('wb') as output:
                if sys.platform == 'linux':
                    import fcntl

                    try:
                        # NAS sources and staging share Btrfs. A private CoW
                        # clone avoids streaming the entire archive twice.
                        fcntl.ioctl(output.fileno(), 0x40049409, original.fileno())
                        report(stage='clone', file=path.name, copied_bytes=signature[1], total_bytes=signature[1])
                        continue
                    except OSError:
                        output.seek(0)
                        output.truncate()
                copied = 0
                while data := original.read(1024 * 1024):
                    output.write(data)
                    copied += len(data)
                    report(stage='copy', file=path.name, copied_bytes=copied, total_bytes=signature[1])
        if before == [_signature(path) for path in paths]:
            break
    else:
        raise RuntimeError('SQLite main/WAL changed during three snapshot attempts')
    with closing(sqlite3.connect(private)) as original:
        # The captured main/WAL pair is already isolated from its producer.
        # Materialize committed WAL pages into that private main file instead
        # of rewriting every page through SQLite's backup API on Btrfs.
        original.execute('PRAGMA synchronous=OFF')
        report(stage='checkpoint')
        with closing(original.execute('PRAGMA wal_checkpoint(TRUNCATE)')) as result:
            if result.fetchone()[0]:
                raise RuntimeError('Private SQLite checkpoint is unexpectedly busy')
        with closing(original.execute('PRAGMA journal_mode=DELETE')) as result:
            if result.fetchone()[0] != 'delete':
                raise RuntimeError('Private SQLite snapshot retained WAL mode')
        # Full quick_check rereads every table/index and can itself monopolize
        # the NAS disks for many minutes. This is an isolated copy of a stable
        # SQLite pair: validate its schema/header and checkpoint layout here;
        # importers validate the actual records as they stream them. Full-file
        # integrity audits belong outside the routine refresh critical section.
        report(stage='validate_header')
        original.execute('SELECT count(*) FROM sqlite_master').fetchone()
        pages = original.execute('PRAGMA page_count').fetchone()[0]
        page_size = original.execute('PRAGMA page_size').fetchone()[0]
        if pages * page_size != private.stat().st_size:
            raise RuntimeError('Private SQLite snapshot has an inconsistent file size')
    private.replace(directory / 'ready.db')
    for suffix in ('-wal', '-shm'):
        Path(str(private) + suffix).unlink(missing_ok=True)
    report(stage='fsync')
    with (directory / 'ready.db').open('rb') as output:
        os.fsync(output.fileno())


def snapshot(source: Path, destination: Path, *, timeout=None):
    from .jobs import ShutdownRequested, heavy_io
    from .runtime_status import STOP, progress

    timeout = float(os.environ.get('SQLITE_SNAPSHOT_TIMEOUT_SECONDS', '600')) if timeout is None else timeout
    destination.mkdir(parents=True, exist_ok=True)
    if STOP.is_set():
        raise ShutdownRequested('Stopped before SQLite snapshot')
    # A fresh generation also avoids applying an obsolete destination WAL to a
    # newly replaced main file. SQLite resolves the symlink before opening WAL.
    directory = Path(tempfile.mkdtemp(prefix=f'.snapshot-{source.name}-', dir=destination))
    published = False
    try:
        progress(stage='waiting_for_disk', file=source.name)
        with heavy_io('snapshot ' + source.name):
            with (directory / 'error.log').open('w') as errors:
                child = subprocess.Popen([sys.executable, __file__, str(source), str(directory)],
                                         stdout=subprocess.DEVNULL, stderr=errors)
            started = time.monotonic()
            last_progress = None
            try:
                while child.poll() is None:
                    if STOP.wait(0.2):
                        raise ShutdownRequested('Stopped while preparing SQLite snapshot')
                    if time.monotonic() - started >= timeout:
                        raise TimeoutError(f'SQLite snapshot exceeded {timeout:g}s: {source.name}')
                    try:
                        raw = (directory / 'progress.json').read_text()
                    except FileNotFoundError:
                        continue
                    if raw != last_progress:
                        progress(**json.loads(raw))
                        last_progress = raw
                if child.returncode:
                    raise RuntimeError((directory / 'error.log').read_text()[-2000:])
                if STOP.is_set():
                    raise ShutdownRequested('Stopped before publishing SQLite snapshot')
            finally:
                # Only this isolated copy process is terminated. Confirm exit
                # before releasing the shared I/O gate or removing its files.
                if child.poll() is None:
                    child.terminate()
                    try:
                        child.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        child.kill()
                        child.wait()
            pointer = directory / 'pointer'
            pointer.symlink_to(Path(directory.name) / 'ready.db')
            pointer.replace(destination / source.name)
            published = True
            progress(stage='snapshot_complete', file=source.name)
        # Keep the current and preceding generation for this database. Readers
        # already holding an older DELETE-journal file can finish on their FD.
        generations = sorted(destination.glob(f'.snapshot-{source.name}-*'),
                             key=lambda item: item.stat().st_mtime_ns, reverse=True)
        for old in generations[2:]:
            shutil.rmtree(old)
    finally:
        if not published:
            shutil.rmtree(directory, ignore_errors=True)


if __name__ == '__main__':
    build(Path(sys.argv[1]), Path(sys.argv[2]))
