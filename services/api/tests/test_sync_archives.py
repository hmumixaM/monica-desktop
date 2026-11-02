from pathlib import Path

from app import sync_archives


def test_source_signatures_ignore_cache_directory_changes(tmp_path: Path) -> None:
    database = tmp_path / "source.db"
    cache = tmp_path / "cache"
    database.write_text("")
    cache.mkdir()

    contacts = sync_archives.import_signature("contacts", [database], cache)
    moments = sync_archives.import_signature("moments", [database], cache)
    messages = sync_archives.import_signature("messages", [database], cache)
    (cache / "new-media.bin").write_bytes(b"new")

    assert contacts == sync_archives.import_signature("contacts", [database], cache)
    assert moments == sync_archives.import_signature("moments", [database], cache)
    assert all(item[0] != str(cache) for item in messages)


def test_contacts_resolve_before_moments_and_changed_authors_finish_in_same_pass(tmp_path, monkeypatch):
    import shutil
    import threading
    from contextlib import nullcontext

    from app import cache_warmup, runtime_status

    source = tmp_path / 'chatlog'
    for relative in ('contact/contact.db', 'sns/sns.db'):
        path = source / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'initial fixture')
    monkeypatch.setenv('CHATLOG_DB_ROOT', str(source))
    monkeypatch.setattr(runtime_status, 'STOP', threading.Event())
    monkeypatch.setattr(runtime_status, 'record', lambda *args, **kwargs: None)
    monkeypatch.setattr(sync_archives, 'SessionLocal', lambda: nullcontext(None))
    def snapshot(paths, destination):
        destination.mkdir(parents=True, exist_ok=True)
        for path in paths:
            shutil.copy2(path, destination / path.name)
    monkeypatch.setattr(sync_archives, 'snapshot_sqlite', snapshot)
    calls = []
    monkeypatch.setattr(sync_archives, 'sync_contacts', lambda *args: calls.append('contacts') or {})
    monkeypatch.setattr(sync_archives, 'reconcile_moments', lambda *args: calls.append('moments') or {})
    monkeypatch.setattr(sync_archives, 'recover_signed_media_queue', lambda *args: {})
    monkeypatch.setattr(sync_archives, 'consume_media_import_queue', lambda *args: {})
    monkeypatch.setattr(sync_archives, 'refresh_projections', lambda *args: {})
    monkeypatch.setattr(cache_warmup, 'warm', lambda *args: 0)
    sync_archives.cycle()
    assert calls == ['contacts', 'moments']
    sync_archives.cycle()
    assert calls == ['contacts', 'moments'], 'unchanged first import must not run Moments twice'
    (source / 'contact/contact.db').write_bytes(b'updated contact author mapping')
    sync_archives.cycle()
    assert calls == ['contacts', 'moments', 'contacts', 'moments']
    sync_archives.cycle()
    assert calls == ['contacts', 'moments', 'contacts', 'moments']


def test_prefetch_wechat_downloads_without_batch_cap(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = tmp_path / "chatlog"
    cache = tmp_path / "cache"
    script = tmp_path / "prefetch.py"
    lock = tmp_path / "prefetch.lock"
    (source / "sns").mkdir(parents=True)
    (source / "contact").mkdir()
    cache.mkdir()
    script.write_text("")
    (source / "sns/sns.db").write_text("")
    (source / "contact/contact.db").write_text("")
    command = None

    def run(arguments, *, check, timeout):
        nonlocal command
        command = arguments
        assert check is True

    monkeypatch.setenv("WECHAT_PREFETCH_SCRIPT", str(script))
    monkeypatch.setenv("WECHAT_PREFETCH_LOCK", str(lock))
    monkeypatch.setenv("CHATLOG_BASE_URL", "http://chatlog.test:5030")
    monkeypatch.setenv("WECHAT_MOMENTS_SINCE", "1789707600")
    monkeypatch.setenv("WECHAT_MEDIA_IMPORT_QUEUE", str(cache / "queue.jsonl"))
    monkeypatch.setattr(sync_archives.subprocess, "run", run)

    import fcntl
    with lock.open('a') as legacy_lock:
        fcntl.flock(legacy_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        report = sync_archives.prefetch_wechat(source, cache)

    assert report["state"] == "complete"
    assert command is not None
    assert command[command.index("--daily-cap") + 1] == "1000000"
    assert command[command.index("--workers") + 1] == "1"
    assert command[command.index("--base-url") + 1] == "http://chatlog.test:5030"
    assert command[command.index("--cache-dir") + 1] == str(cache)
    assert command[command.index("--moments-since-timestamp") + 1] == "1789707600"
    assert command[command.index("--import-queue") + 1] == str(cache / "queue.jsonl")


def test_media_queue_checkpoint_reads_only_new_records(tmp_path: Path) -> None:
    queue = tmp_path / "queue.jsonl"
    state = tmp_path / "state.json"
    first = '{"version":1,"cache_name":"one.bin","tid":1,"kind":"photo"}\n'
    queue.write_text(first)

    records, offset = sync_archives.read_media_import_queue(queue, state)

    assert [record["cache_name"] for record in records] == ["one.bin"]
    sync_archives.write_media_import_checkpoint(state, offset)
    queue.write_text(
        queue.read_text() + '{"version":1,"cache_name":"two.bin","tid":2,"kind":"photo"}\n'
    )
    records, next_offset = sync_archives.read_media_import_queue(queue, state)
    assert [record["cache_name"] for record in records] == ["two.bin"]
    assert next_offset > offset


def test_media_queue_deduplicates_replayed_cache_names(tmp_path: Path) -> None:
    queue = tmp_path / "queue.jsonl"
    queue.write_text(
        '{"version":1,"cache_name":"same.bin","tid":1,"kind":"photo"}\n'
        '{"version":1,"cache_name":"same.bin","tid":2,"kind":"photo"}\n'
    )

    records, _ = sync_archives.read_media_import_queue(queue, tmp_path / "state.json")

    assert len(records) == 1
    assert records[0]["tids"] == [1, 2]


def test_unmatched_queue_media_is_set_aside_and_checkpoint_advances(tmp_path: Path, monkeypatch) -> None:
    from contextlib import nullcontext

    cache = tmp_path / "cache"
    snapshots = tmp_path / "snapshots"
    (snapshots / "moments").mkdir(parents=True)
    (snapshots / "moments" / "sns.db").write_bytes(b"")
    cache.mkdir()
    queue = cache / "media-import-queue.jsonl"
    queue.write_text(
        '{"version":1,"cache_name":"good.bin","tid":1,"kind":"photo"}\n'
        '{"version":1,"cache_name":"orphan.bin","tid":2,"kind":"photo"}\n'
    )
    monkeypatch.setenv("WECHAT_MEDIA_IMPORT_QUEUE", str(queue))
    monkeypatch.setattr(sync_archives, "SessionLocal", lambda: nullcontext(None))
    monkeypatch.setattr(sync_archives, "moments_cache",
                        lambda *args, **kwargs: {"linked_media": 1, "unmatched_queue_files": ["orphan.bin"]})

    report = sync_archives.consume_media_import_queue(snapshots, cache, tmp_path / "media")

    assert report["state"] == "complete" and report["unmatched"] == 1
    review = (snapshots / "media-import-unmatched.jsonl").read_text().splitlines()
    assert [__import__("json").loads(line)["cache_name"] for line in review] == ["orphan.bin"]
    assert sync_archives.consume_media_import_queue(snapshots, cache, tmp_path / "media") == {
        "records": 0, "state": "unchanged", "offset": queue.stat().st_size}


def test_only_changed_sqlite_files_are_snapshotted(tmp_path: Path) -> None:
    import os

    source = tmp_path / "source"
    snapshots = tmp_path / "snapshots"
    source.mkdir()
    snapshots.mkdir()
    one, two = source / "message_0.db", source / "message_1.db"
    for path in (one, two):
        path.write_bytes(b"db")
        (snapshots / path.name).write_bytes(b"old")
    stamp = [list(entry) for entry in sync_archives.signature([one, two])]
    assert sync_archives.changed_files([one, two], stamp, snapshots) == []
    wal = Path(str(two) + "-wal")
    wal.write_bytes(b"wal")
    assert sync_archives.changed_files([one, two], stamp, snapshots) == [two]
    os.remove(snapshots / one.name)
    assert sync_archives.changed_files([one, two], stamp, snapshots) == [one, two]


def test_moment_fingerprints_select_changed_posts_and_author_links() -> None:
    from app.reconcile_sources import changed_moments

    previous = {"1": ["a", None], "2": ["b", "p1"], "3": ["c", "p2"]}
    current = {"1": ["a", "p9"], "2": ["b", "p1"], "3": ["x", "p2"], "4": ["d", None]}
    assert changed_moments(previous, current) == {"1", "3", "4"}


def test_message_failure_does_not_block_media_or_advance_source_state(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("app.runtime_status.record", lambda *args, **kwargs: None)
    import json
    from contextlib import nullcontext

    from app import cache_warmup

    source = tmp_path / 'chatlog'
    (source / 'message').mkdir(parents=True)
    shard = source / 'message/message_9.db'
    shard.write_bytes(b'pending')
    monkeypatch.setenv('CHATLOG_DB_ROOT', str(source))
    calls = []

    class Lease:
        def execute(self, *args, **kwargs):
            return self

        def scalar_one(self):
            return True

        def commit(self):
            pass

    monkeypatch.setattr(sync_archives, 'engine', type('Engine', (), {'connect': lambda _: nullcontext(Lease())})())
    monkeypatch.setattr(sync_archives, 'SessionLocal', lambda: nullcontext(None))
    monkeypatch.setattr(sync_archives, 'snapshot_sqlite', lambda *args: None)

    def fail(*args, **kwargs):
        raise RuntimeError('unreadable shard')

    monkeypatch.setattr(sync_archives, 'ingest', fail)
    monkeypatch.setattr(sync_archives, 'prefetch_wechat', lambda *args: calls.append('download') or {})
    monkeypatch.setattr(sync_archives, 'consume_media_import_queue', lambda *args: calls.append('link') or {})
    monkeypatch.setattr(sync_archives, 'refresh_projections', lambda *args: {})
    monkeypatch.setattr(cache_warmup, 'warm', lambda *args: 0)

    sync_archives.cycle()

    assert calls == ['link']
    sync_archives.download_wechat_media()
    assert calls == ['link', 'download']
    state = json.loads((tmp_path / 'staging/continuous-snapshots/state.json').read_text())
    assert 'messages' not in state
    assert 'unreadable shard' in capsys.readouterr().out


def test_pending_message_shard_is_retried_without_source_changes(tmp_path, monkeypatch):
    monkeypatch.setattr("app.runtime_status.record", lambda *args, **kwargs: None)
    import json
    from contextlib import nullcontext

    from app import cache_warmup

    source = tmp_path / 'chatlog'
    (source / 'message').mkdir(parents=True)
    shard = source / 'message/message_9.db'
    shard.write_bytes(b'pending')
    monkeypatch.setenv('CHATLOG_DB_ROOT', str(source))

    class Lease:
        def execute(self, *args, **kwargs):
            return self

        def scalar_one(self):
            return True

        def commit(self):
            pass

    monkeypatch.setattr(sync_archives, 'engine', type('Engine', (), {'connect': lambda _: nullcontext(Lease())})())
    monkeypatch.setattr(sync_archives, 'SessionLocal', lambda: nullcontext(None))
    monkeypatch.setattr(sync_archives, 'snapshot_sqlite', lambda *args: None)
    monkeypatch.setattr(sync_archives, 'ingest', lambda *args, **kwargs: {'pending_files': ['message_9.db']})
    monkeypatch.setattr(sync_archives, 'prefetch_wechat', lambda *args: {})
    monkeypatch.setattr(sync_archives, 'consume_media_import_queue', lambda *args: {})
    monkeypatch.setattr(sync_archives, 'refresh_projections', lambda *args: {})
    monkeypatch.setattr(cache_warmup, 'warm', lambda *args: 0)

    sync_archives.cycle()

    snapshots = tmp_path / 'staging/continuous-snapshots'
    state = json.loads((snapshots / 'state.json').read_text())
    assert state['messages'] == []
    assert sync_archives.changed_files([shard], state['messages'], snapshots / 'messages') == [shard]


def test_signed_sns_queue_roundtrip_and_quarantine_recovery(tmp_path):
    import base64
    import json
    queue, state = tmp_path / "queue.jsonl", tmp_path / "state.json"
    record = {"version": 1, "cache_name": "photo.bin", "kind": "photo", "tid": -3415012451541445976, "tids": [-3415012451541445976]}
    raw = (json.dumps(record) + "\n").encode()
    quarantine = tmp_path / "media-queue-quarantine"
    quarantine.mkdir()
    path = quarantine / "123.json"
    path.write_text(json.dumps({"reason": "Invalid message IDs", "raw_base64": base64.b64encode(raw).decode()}))
    assert sync_archives.recover_signed_media_queue(tmp_path, queue) == {"recovered_records": 1}
    rows, offset = sync_archives.read_media_import_queue(queue, state)
    assert rows == [record]
    sync_archives.write_media_import_checkpoint(state, offset)
    assert sync_archives.recover_signed_media_queue(tmp_path, queue) == {"recovered_records": 0}
    assert sync_archives.read_media_import_queue(queue, state) == ([], offset)
    assert path.with_suffix(".replayed").is_file()


def test_queue_ids_reject_corrupt_values_but_accept_signed_int64():
    assert sync_archives.valid_media_tids([-(2**63), 2**63 - 1, "-42", "0", None])
    for value in [2**63, -(2**63)-1, True, 1.2, "1.2", "--1", "1;DROP TABLE"]:
        assert not sync_archives.valid_media_tids([value])


def test_quarantine_recovery_leaves_other_corruption_for_review(tmp_path):
    import base64
    import json
    directory = tmp_path / "media-queue-quarantine"
    directory.mkdir()
    for i, raw in enumerate([b'not json', json.dumps({"version":1,"cache_name":"bad.bin","kind":"photo","tids":[-(2**64)]}).encode()]):
        (directory / f"{i}.json").write_text(json.dumps({"reason":"Invalid message IDs", "raw_base64":base64.b64encode(raw).decode()}))
    assert sync_archives.recover_signed_media_queue(tmp_path, tmp_path / "queue") == {"recovered_records": 0}
    assert len(list(directory.glob("*.json"))) == 2


def test_empty_message_retries_do_not_repeat_full_prefetch(tmp_path, monkeypatch):
    from contextlib import nullcontext

    from app import cache_warmup
    monkeypatch.setattr('app.runtime_status.record', lambda *a, **kw: None)
    source = tmp_path / 'source'
    (source / 'message').mkdir(parents=True)
    (source / 'message/message_9.db').write_bytes(b'pending')
    monkeypatch.setenv('CHATLOG_DB_ROOT', str(source))
    class Lease:
        def execute(self, *a, **kw): return self
        def scalar_one(self): return True
        def commit(self): pass
    monkeypatch.setattr(sync_archives, 'engine', type('Engine', (), {'connect': lambda _: nullcontext(Lease())})())
    monkeypatch.setattr(sync_archives, 'SessionLocal', lambda: nullcontext(None))
    monkeypatch.setattr(sync_archives, 'snapshot_sqlite', lambda *a: None)
    monkeypatch.setattr(sync_archives, 'ingest', lambda *a, **kw: {'pending_files': ['message_9.db']})
    calls = []
    monkeypatch.setattr(sync_archives, 'prefetch_wechat', lambda *a: calls.append(1) or {'state':'complete'})
    monkeypatch.setattr(sync_archives, 'consume_media_import_queue', lambda *a: {})
    monkeypatch.setattr(sync_archives, 'refresh_projections', lambda *a: {})
    monkeypatch.setattr(cache_warmup, 'warm', lambda *a: 0)
    sync_archives.cycle()
    sync_archives.download_wechat_media()
    sync_archives.cycle()
    sync_archives.download_wechat_media()
    assert calls == [1]
    # A source update bypasses the hourly retry delay.
    (source / 'sns').mkdir()
    (source / 'sns/sns.db').write_bytes(b'new source')
    monkeypatch.setattr(sync_archives, 'reconcile_moments', lambda *a: {})
    sync_archives.cycle()
    sync_archives.download_wechat_media()
    assert calls == [1, 1]


def test_downloads_have_no_archive_connection_even_when_import_is_busy(monkeypatch):
    from contextlib import contextmanager

    connected = False
    locked = False
    calls = []
    available = True

    class Lease:
        def execute(self, statement, parameters=None):
            nonlocal locked
            locked = available if 'pg_try_advisory_lock' in str(statement) else False
            return self

        def scalar_one(self):
            return locked

        def commit(self):
            pass

    @contextmanager
    def connect(_):
        nonlocal connected
        connected = True
        try:
            yield Lease()
        finally:
            connected = False

    def download():
        assert not connected and not locked
        calls.append('download')

    def link():
        assert connected and locked
        calls.append('link')

    def fail_import():
        assert locked
        raise RuntimeError('source failed')

    monkeypatch.setattr(sync_archives, 'engine', type('Engine', (), {'connect': connect})())
    monkeypatch.setattr(sync_archives, 'cycle', list)
    monkeypatch.setattr(sync_archives, 'download_wechat_media', download)
    monkeypatch.setattr(sync_archives, 'link_downloaded_media', link)
    assert sync_archives.iteration() == []
    assert calls == ['download', 'link']
    calls.clear()
    available = False
    assert sync_archives.iteration() == ['busy', 'busy']
    assert calls == ['download']
    available = True
    monkeypatch.setattr(sync_archives, 'cycle', fail_import)
    assert sync_archives.iteration() == ['cycle']
    assert calls == ['download', 'download', 'link']
    assert not connected and not locked


def test_real_postgres_archive_lock_released_before_download(crm_db, monkeypatch):
    from sqlalchemy import text

    engine = crm_db.get_bind().engine
    observed = []

    def check(expected):
        with engine.connect() as probe:
            held = probe.execute(text("""SELECT EXISTS(SELECT 1 FROM pg_locks
                WHERE locktype='advisory' AND classid=0 AND objid=11803360 AND granted)""")).scalar_one()
        assert held is expected
        observed.append(held)

    def fail_source():
        check(True)
        raise RuntimeError('source failed')

    monkeypatch.setattr(sync_archives, 'engine', engine)
    monkeypatch.setattr(sync_archives, 'cycle', fail_source)
    monkeypatch.setattr(sync_archives, 'download_wechat_media', lambda: check(False))
    monkeypatch.setattr(sync_archives, 'link_downloaded_media', lambda: check(True))
    assert sync_archives.iteration() == ['cycle']
    check(False)
    assert observed == [True, False, True, False]


def test_recovery_batch_crash_after_durable_append_replays_without_loss(tmp_path, monkeypatch):
    import base64
    import json
    directory = tmp_path / 'media-queue-quarantine'
    directory.mkdir()
    queue = tmp_path / 'queue'
    for i in range(2):
        record = {'version':1, 'kind':'photo', 'cache_name':f'{i}.bin', 'tids':[-i-1]}
        (directory / f'{i}.json').write_text(json.dumps({'reason':'Invalid message IDs',
            'raw_base64':base64.b64encode(json.dumps(record).encode()).decode()}))
    rename = Path.rename
    def crash(path, target):
        if path == directory / '1.json':
            raise OSError('simulated crash before marker')
        return rename(path, target)
    monkeypatch.setattr(Path, 'rename', crash)
    import pytest
    with pytest.raises(OSError, match='simulated crash'):
        sync_archives.recover_signed_media_queue(tmp_path, queue)
    assert len(queue.read_text().splitlines()) == 2
    monkeypatch.setattr(Path, 'rename', rename)
    assert sync_archives.recover_signed_media_queue(tmp_path, queue)['recovered_records'] == 1
    rows, _ = sync_archives.read_media_import_queue(queue, tmp_path / 'state')
    assert {row['cache_name'] for row in rows} == {'0.bin', '1.bin'}


def test_cached_media_failure_keeps_moments_fingerprint_for_retry(tmp_path, monkeypatch):
    import json

    fingerprints = tmp_path / 'fingerprints.json'
    old = {'-42': ['old', None]}
    fingerprints.write_text(json.dumps(old))
    monkeypatch.setattr(sync_archives, 'moment_fingerprints', lambda *_: {'-42': ['new', None]})
    monkeypatch.setattr(sync_archives, 'moments', lambda *a, **kw: {})

    def fail(*args, **kwargs):
        assert kwargs['tids'] == {-42}
        raise OSError('media store temporarily busy')

    monkeypatch.setattr(sync_archives, 'moments_cache', fail)
    import pytest
    with pytest.raises(OSError, match='temporarily busy'):
        sync_archives.reconcile_moments(None, tmp_path / 'sns.db', fingerprints)
    assert json.loads(fingerprints.read_text()) == old


def test_cached_media_shutdown_keeps_moments_fingerprint(tmp_path, monkeypatch):
    import json

    import pytest

    from app.jobs import ShutdownRequested

    fingerprints = tmp_path / 'fingerprints.json'
    old = {'-42': ['old', None]}
    fingerprints.write_text(json.dumps(old))
    monkeypatch.setattr(sync_archives, 'moment_fingerprints', lambda *_: {'-42': ['new', None]})
    monkeypatch.setattr(sync_archives, 'moments', lambda *a, **kw: {})
    monkeypatch.setattr(sync_archives, 'moments_cache', lambda *a, **kw: {'stopped_at_checkpoint': True})
    with pytest.raises(ShutdownRequested):
        sync_archives.reconcile_moments(None, tmp_path / 'sns.db', fingerprints)
    assert json.loads(fingerprints.read_text()) == old
