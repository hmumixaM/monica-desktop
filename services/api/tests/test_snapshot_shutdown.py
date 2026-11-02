import shutil
import sqlite3
import subprocess
import sys
import threading
from contextlib import closing

import pytest

from app import jobs, runtime_status, sqlite_snapshot, sync_archives


@pytest.fixture(autouse=True)
def snapshot_gate(tmp_path, monkeypatch):
    monkeypatch.setenv('STAGING_ROOT', str(tmp_path / 'staging'))


def source_database(path, value, large=False):
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE current(value TEXT)')
        db.execute('INSERT INTO current VALUES (?)', (value,))
        if large:
            db.execute('CREATE TABLE padding(data BLOB)')
            db.execute('INSERT INTO padding VALUES (?)', (b'x' * (3 * 1024 * 1024),))


def test_sqlite_snapshot_still_copies_a_complete_database(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime_status, 'STOP', threading.Event())
    source = tmp_path / 'source.db'
    destination = tmp_path / 'snapshots'
    source_database(source, 'new', large=True)
    sync_archives.snapshot_sqlite([source], destination)
    with sqlite3.connect(destination / source.name) as db:
        assert db.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
        assert db.execute('SELECT value FROM current').fetchone()[0] == 'new'


def test_cancelled_sqlite_snapshot_preserves_previous_complete_database(tmp_path, monkeypatch):
    stop = threading.Event()
    monkeypatch.setattr(runtime_status, 'STOP', stop)
    source = tmp_path / 'source.db'
    destination = tmp_path / 'snapshots'
    destination.mkdir()
    source_database(source, 'new', large=True)
    source_database(destination / source.name, 'previous')
    real_popen = subprocess.Popen
    children = []

    def launch(*args, **kwargs):
        child = real_popen([sys.executable, '-c', 'import time; time.sleep(60)'], **kwargs)
        children.append(child)
        stop.set()
        return child

    monkeypatch.setattr(sqlite_snapshot.subprocess, 'Popen', launch)
    with pytest.raises(jobs.ShutdownRequested):
        sync_archives.snapshot_sqlite([source], destination)
    assert children[0].poll() is not None, 'child must exit before the shared disk gate is released'
    with sqlite3.connect(destination / source.name) as db:
        assert db.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
        assert db.execute('SELECT value FROM current').fetchone()[0] == 'previous'


def test_snapshot_timeout_kills_only_its_copy_and_preserves_previous(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime_status, 'STOP', threading.Event())
    source = tmp_path / 'source.db'
    destination = tmp_path / 'snapshots'
    destination.mkdir()
    source_database(source, 'new')
    source_database(destination / source.name, 'previous')
    real_popen = subprocess.Popen
    children = []

    def launch(*args, **kwargs):
        child = real_popen([sys.executable, '-c', 'import time; time.sleep(60)'], **kwargs)
        children.append(child)
        return child

    monkeypatch.setattr(sqlite_snapshot.subprocess, 'Popen', launch)
    with pytest.raises(TimeoutError):
        sqlite_snapshot.snapshot(source, destination, timeout=0.1)
    assert children[0].poll() is not None
    with closing(sqlite3.connect(destination / source.name)) as db:
        assert db.execute('SELECT value FROM current').fetchone()[0] == 'previous'


def test_snapshot_rebuilds_source_shm_and_ignores_old_destination_wal(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime_status, 'STOP', threading.Event())
    source = tmp_path / 'source.db'
    destination = tmp_path / 'snapshots'
    destination.mkdir()
    source_database(source, 'main')
    source_database(destination / source.name, 'previous')
    with closing(sqlite3.connect(source)) as writer, closing(sqlite3.connect(destination / source.name)) as old:
        writer.execute('PRAGMA journal_mode=WAL')
        writer.execute("UPDATE current SET value='from WAL'")
        writer.commit()
        old.execute('PRAGMA journal_mode=WAL')
        old.execute("UPDATE current SET value='old WAL'")
        old.commit()
        # A poisoned copy of the SHM would be unusable; it must never be copied.
        original_shm = (tmp_path / 'source.db-shm').read_bytes()
        captured = tmp_path / 'captured'
        captured.mkdir()
        for suffix in ('', '-wal'):
            shutil.copyfile(str(source) + suffix, str(captured / source.name) + suffix)
        (captured / 'source.db-shm').write_bytes(b'\xff' * 32768)
        # Simulate a decrypted main/WAL pair with stale SHM on a read-only
        # container mount. The source SHM must remain completely untouched.
        sync_archives.snapshot_sqlite([captured / source.name], destination)
        assert (captured / 'source.db-shm').read_bytes() == b'\xff' * 32768
        sync_archives.snapshot_sqlite([source], destination)
        assert (tmp_path / 'source.db-shm').read_bytes() == original_shm
        with closing(sqlite3.connect(destination / source.name)) as db:
            assert db.execute('SELECT value FROM current').fetchone()[0] == 'from WAL'
            assert db.execute('PRAGMA journal_mode').fetchone()[0] == 'delete'
        assert old.execute('SELECT value FROM current').fetchone()[0] == 'old WAL'
    previous = 'from WAL'
    for value in ('second', 'third'):
        with closing(sqlite3.connect(source)) as writer:
            writer.execute('UPDATE current SET value=?', (value,))
            writer.commit()
        with closing(sqlite3.connect(destination / source.name)) as frozen:
            assert frozen.execute('SELECT value FROM current').fetchone()[0] == previous
        sync_archives.snapshot_sqlite([source], destination)
        previous = value
    assert len(list(destination.glob('.snapshot-source.db-*'))) == 2
    with closing(sqlite3.connect(destination / source.name)) as db:
        assert db.execute('SELECT value FROM current').fetchone()[0] == 'third'


def test_stopped_snapshot_does_not_replace_existing_data(tmp_path, monkeypatch):
    stop = threading.Event()
    monkeypatch.setattr(runtime_status, 'STOP', stop)
    source = tmp_path / 'source.db'
    destination = tmp_path / 'snapshots'
    destination.mkdir()
    source_database(source, 'new')
    source_database(destination / source.name, 'previous')
    stop.set()
    with pytest.raises(jobs.ShutdownRequested):
        sync_archives.snapshot_sqlite([source], destination)
    with sqlite3.connect(destination / source.name) as db:
        assert db.execute('SELECT value FROM current').fetchone()[0] == 'previous'


def test_invalid_source_does_not_publish_an_unvalidated_private_database(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime_status, 'STOP', threading.Event())
    source = tmp_path / 'source.db'
    source.write_bytes(b'not a SQLite database')
    destination = tmp_path / 'snapshots'
    destination.mkdir()
    source_database(destination / source.name, 'previous')
    with pytest.raises(RuntimeError, match='not a database'):
        sync_archives.snapshot_sqlite([source], destination)
    with closing(sqlite3.connect(destination / source.name)) as db:
        assert db.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
        assert db.execute('SELECT value FROM current').fetchone()[0] == 'previous'
    assert not list(destination.glob('.snapshot-*'))


def test_truncated_source_does_not_replace_the_previous_snapshot(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime_status, 'STOP', threading.Event())
    source = tmp_path / 'source.db'
    source_database(source, 'new')
    source.write_bytes(source.read_bytes()[:-100])
    destination = tmp_path / 'snapshots'
    destination.mkdir()
    source_database(destination / source.name, 'previous')
    with pytest.raises(RuntimeError):
        sync_archives.snapshot_sqlite([source], destination)
    with closing(sqlite3.connect(destination / source.name)) as db:
        assert db.execute('SELECT value FROM current').fetchone()[0] == 'previous'


def test_moments_scan_shutdown_keeps_the_previous_fingerprint_checkpoint(tmp_path, monkeypatch):
    from app import reconcile_sources

    source = tmp_path / 'sns.db'
    with closing(sqlite3.connect(source)) as db:
        db.execute('CREATE TABLE SnsTimeLine(tid INTEGER PRIMARY KEY,user_name TEXT,content TEXT)')
        db.executemany('INSERT INTO SnsTimeLine VALUES (?,?,?)',
                       [(n,'friend','<TimelineObject/>') for n in range(1500)])
        db.commit()
    fingerprints = tmp_path / 'fingerprints.json'
    fingerprints.write_text('{"previous":"complete"}')
    stop = threading.Event()
    monkeypatch.setattr(runtime_status,'STOP',stop)
    states = []
    monkeypatch.setattr(runtime_status, 'record', lambda source, name, status, *args: states.append(status))
    monkeypatch.setattr(reconcile_sources,'identities',lambda *_:{})
    seen=[]
    def progress(**fields):
        seen.append(fields)
        stop.set()
    monkeypatch.setattr(runtime_status,'progress',progress)
    def unexpected(*args,**kwargs):
        raise AssertionError('Projection started after shutdown')
    monkeypatch.setattr(sync_archives,'moments',unexpected)
    with pytest.raises(jobs.ShutdownRequested), runtime_status.phase('archives', 'moments'):
        sync_archives.reconcile_moments(None,source,fingerprints)
    assert seen == [{'stage':'fingerprints','processed':1000}]
    assert fingerprints.read_text() == '{"previous":"complete"}'
    assert states == ['running', 'pending'], 'a requested pause must not be reported as an import failure'


@pytest.mark.parametrize('failed,expected', [(False, 'pending'), (True, 'failed')])
def test_returned_story_checkpoint_is_pending_and_preserves_failures(monkeypatch, failed, expected):
    states = []
    monkeypatch.setattr(runtime_status, 'record', lambda source, name, status, *args: states.append(status))
    with runtime_status.phase('instagram', 'stories') as details:
        details.update(stopped_at_checkpoint=True, failed=failed)
    assert states == ['running', expected]
