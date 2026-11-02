import fcntl
import json
import subprocess
import sys
import textwrap

import pytest
from fastapi.testclient import TestClient

from app import jobs
from app.main import app


def test_heavy_io_is_exclusive_across_processes_and_reentrant(tmp_path, monkeypatch):
    monkeypatch.setenv("STAGING_ROOT", str(tmp_path))
    with jobs.heavy_io("outer"):
        with jobs.heavy_io("nested"):
            holder = json.loads(jobs.lock_path().read_text())
            assert holder["name"] == "outer"
        probe = textwrap.dedent(f"""
            import fcntl
            with open({str(jobs.lock_path())!r}, "a+") as handle:
                try:
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    raise SystemExit(3)
        """)
        assert subprocess.run([sys.executable, "-c", probe], check=False).returncode == 3
    with jobs.lock_path().open("a+") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert handle.read() == ""


def test_liveness_probes_do_not_need_the_database():
    client = TestClient(app)
    for path in ("/health", "/api/v1/health"):
        response = client.get(path)
        assert response.status_code == 200
        assert response.json()["status"] == "ok"


def test_long_backfills_release_the_lock_between_batches(tmp_path, monkeypatch):
    from app import media_dimensions

    monkeypatch.setenv("STAGING_ROOT", str(tmp_path))
    held = []

    class Rows:
        def __init__(self, rows):
            self.rows = rows

        def mappings(self):
            return self

        def all(self):
            return self.rows

    batches = [[{"id": "a", "created_at": None}, {"id": "b", "created_at": None}], []]

    class FakeDb:
        def execute(self, *args, **kwargs):
            held.append(json.loads(jobs.lock_path().read_text())["name"])
            return Rows(batches.pop(0))

        def commit(self):
            pass

    monkeypatch.setattr(media_dimensions, "record_dimensions", lambda db, media_id: True)
    monkeypatch.setattr(jobs, "yield_heavy_io", lambda pause=0: held.append(jobs.lock_path().read_text()))
    report = media_dimensions.backfill(FakeDb())
    assert report["measured"] == 2
    assert held == ["media_dimensions backfill", "", "media_dimensions backfill"]


@pytest.mark.parametrize("ocr_daemon", [False, True])
def test_shutdown_cancels_process_waiting_for_disk_lock(tmp_path, monkeypatch, ocr_daemon):
    import os
    import select
    import signal

    monkeypatch.setenv('STAGING_ROOT', str(tmp_path))
    code = textwrap.dedent(f'''
        from app.jobs import heavy_io, ShutdownRequested
        from app.runtime_status import install_shutdown
        install_shutdown()
        try:
            if {ocr_daemon!r}:
                from app.ai import image_ocr
                class Session:
                    def __enter__(self): return self
                    def __exit__(self, *args): pass
                    def commit(self): pass
                image_ocr.SessionLocal = Session
                image_ocr.repair_text_only = lambda db: None
                image_ocr.lower_priority = lambda: None
                image_ocr.run()
            else:
                with heavy_io('child'):
                    raise SystemExit('entered protected work after shutdown')
        except ShutdownRequested:
            pass
        print('stopped before disk work', flush=True)
    ''')
    with jobs.heavy_io('parent'):
        original = jobs.lock_path().read_text()
        child = subprocess.Popen([sys.executable, '-c', code], env=os.environ.copy(),
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            assert select.select([child.stdout], [], [], 5)[0], 'waiter did not start'
            assert json.loads(child.stdout.readline())['heavy_io_waiting'] == ('image OCR' if ocr_daemon else 'child')
            child.send_signal(signal.SIGTERM)
            output, errors = child.communicate(timeout=3)
            assert child.returncode == 0, errors
            assert 'stopped before disk work' in output
            assert jobs.lock_path().read_text() == original
        finally:
            if child.poll() is None:
                child.kill()
                child.wait()


def test_shutdown_cancels_thread_waiting_for_disk_lock(tmp_path, monkeypatch):
    import threading

    from app import runtime_status

    stop = threading.Event()
    monkeypatch.setattr(runtime_status, 'STOP', stop)
    monkeypatch.setenv('STAGING_ROOT', str(tmp_path))
    attempted = threading.Event()
    cancelled = threading.Event()
    entered = threading.Event()

    def wait_for_permission():
        attempted.set()
        try:
            with jobs.heavy_io('other thread'):
                entered.set()
        except jobs.ShutdownRequested:
            cancelled.set()

    with jobs.heavy_io('owner'):
        waiter = threading.Thread(target=wait_for_permission, daemon=True)
        waiter.start()
        assert attempted.wait(1)
        stop.set()
        try:
            assert cancelled.wait(2), 'shutdown must not wait for the current owner'
            assert not entered.is_set()
        finally:
            stop.clear()
    waiter.join(2)
    assert not waiter.is_alive()
    with jobs.heavy_io('next owner'):
        assert json.loads(jobs.lock_path().read_text())['name'] == 'next owner'


def test_shutdown_preserves_nested_work_already_holding_permission(tmp_path, monkeypatch):
    import threading

    from app import runtime_status

    stop = threading.Event()
    monkeypatch.setattr(runtime_status, 'STOP', stop)
    monkeypatch.setenv('STAGING_ROOT', str(tmp_path))
    with jobs.heavy_io('committing batch'):
        stop.set()
        with jobs.heavy_io('nested part of batch'):
            assert json.loads(jobs.lock_path().read_text())['name'] == 'committing batch'
    stop.clear()
    with jobs.heavy_io('next batch'):
        pass


def test_background_io_uses_fair_best_effort_priority(monkeypatch):
    calls = []
    nice = []

    class Kernel:
        def syscall(self, *values):
            calls.append(tuple(value.value for value in values))
            return 0

    monkeypatch.setattr(jobs, '_lowered', False)
    monkeypatch.setattr(jobs.sys, 'platform', 'linux')
    monkeypatch.setattr(jobs.platform, 'machine', lambda: 'x86_64')
    monkeypatch.setattr(jobs.os, 'nice', nice.append)
    monkeypatch.setattr(jobs.ctypes, 'CDLL', lambda *args, **kwargs: Kernel())
    jobs.lower_priority()
    jobs.lower_priority()
    assert nice == [10]
    assert calls == [(251, 1, 0, (2 << 13) | 7)]
