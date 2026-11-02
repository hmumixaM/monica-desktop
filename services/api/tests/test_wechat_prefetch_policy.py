import importlib.util
import sqlite3
import sys
from pathlib import Path

SCRIPT = Path(__file__).parents[3] / "infra" / "wechat" / "prefetch-sns-images.py"
SPEC = importlib.util.spec_from_file_location("wechat_prefetch_policy", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
prefetch = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = prefetch
SPEC.loader.exec_module(prefetch)


def test_moments_tids_since_excludes_old_failed_media(tmp_path: Path) -> None:
    database = tmp_path / "sns.db"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE SnsTimeLine (tid INTEGER, content TEXT)")
        connection.executemany(
            "INSERT INTO SnsTimeLine VALUES (?,?)",
            [
                (1, "<TimelineObject><createTime>1789707599</createTime></TimelineObject>"),
                (2, "<TimelineObject><createTime>1789707600</createTime></TimelineObject>"),
                (3, "<TimelineObject><createTime>1789800000</createTime></TimelineObject>"),
            ],
        )

    assert prefetch.moments_tids_since(database, 1789707600) == {2, 3}


def test_zero_key_uses_same_cache_identity_as_proxy() -> None:
    zero = prefetch.MediaResource(1, "wxid_test", "photo", "http://example.test/0", "0")
    empty = prefetch.MediaResource(1, "wxid_test", "photo", "http://example.test/0", "")

    assert zero.cache_name == empty.cache_name


def test_completed_download_appends_incremental_import_record(tmp_path: Path) -> None:
    queue = tmp_path / "media-import.jsonl"
    media = prefetch.MediaResource(
        42,
        "wxid_test",
        "photo",
        "http://example.test/0",
        "",
    )

    prefetch.append_import_queue(queue, media, 1234)

    record = __import__("json").loads(queue.read_text())
    assert record["cache_name"] == media.cache_name
    assert record["tid"] == 42
    assert record["tids"] == [42]
    assert record["username"] == "wxid_test"
    assert record["kind"] == "photo"
    assert record["byte_length"] == 1234


def test_failed_resources_back_off_exponentially_and_clear_on_success(tmp_path: Path) -> None:
    import argparse

    media = prefetch.MediaResource(7, "wxid_test", "photo", "http://example.test/gone", "k")
    fresh = prefetch.MediaResource(8, "wxid_test", "photo", "http://example.test/new", "k")
    args = argparse.Namespace(cache_dir=tmp_path, limit=None, daily_cap=100)
    failures: dict = {}
    prefetch.record_failure(failures, media, OSError("HTTP Error 500"), 1000.0)
    assert failures[media.cache_name]["next_attempt"] == 1000.0 + 60
    prefetch.record_failure(failures, media, OSError("HTTP Error 500"), 2000.0)
    assert failures[media.cache_name]["next_attempt"] == 2000.0 + 120
    for _ in range(10):
        prefetch.record_failure(failures, media, OSError("HTTP Error 500"), 3000.0)
    assert failures[media.cache_name]["next_attempt"] == 3000.0 + 3600

    assert prefetch.media_for_today([media, fresh], args, failures, now=4000.0) == [fresh]
    assert media in prefetch.media_for_today([media, fresh], args, failures, now=3000.0 + 31 * 86400)
    ledger = prefetch.failure_ledger_path(args)
    prefetch.write_failures(ledger, failures)
    assert prefetch.read_failures(ledger) == failures


def test_cache_hit_is_published_after_crash_and_for_new_post_reference(tmp_path):
    import json
    cache = tmp_path / "cache"
    cache.mkdir()
    queue = tmp_path / "queue.jsonl"
    item = prefetch.MediaResource(-42, "wxid", "photo", "http://example.test/photo", "", tids=(-42,))
    (cache / item.cache_name).write_bytes(b"cached image")
    assert prefetch.publish_cached_moments([item], cache, queue) == 1
    assert prefetch.publish_cached_moments([item], cache, queue) == 0
    shared = prefetch.replace(item, tids=(-42, -43))
    assert prefetch.publish_cached_moments([shared], cache, queue) == 1
    records = [json.loads(line) for line in queue.read_text().splitlines()]
    assert [record["tids"] for record in records] == [[-42], [-43]]


def test_empty_cache_file_is_not_successfully_published(tmp_path):
    item = prefetch.MediaResource(-42, "wxid", "photo", "http://example.test/photo", "")
    (tmp_path / item.cache_name).touch()
    assert prefetch.publish_cached_moments([item], tmp_path, tmp_path / "queue") == 0


def test_since_filter_precedes_dedup_for_reused_images(tmp_path):
    database = tmp_path / "sns.db"
    template = '<TimelineObject><createTime>{}</createTime><ContentObject><type>1</type><mediaList><media><type>2</type><url>http://example.test/photo</url></media></mediaList></ContentObject></TimelineObject>'
    with sqlite3.connect(database) as db:
        db.execute("CREATE TABLE SnsTimeLine(tid INTEGER, user_name TEXT, content TEXT)")
        db.executemany("INSERT INTO SnsTimeLine VALUES(?,?,?)", [(-1,"wxid",template.format(1)),(-2,"wxid",template.format(100))])
    resources = prefetch.read_media(database, {"1"}, since=50)
    assert len(resources) == 1 and resources[0].tids == (-2,)


def test_legacy_month_long_backoff_is_retried_immediately(tmp_path, monkeypatch):
    import json
    monkeypatch.setattr(prefetch.time, 'time', lambda: 1000)
    path = tmp_path / 'failures.json'
    path.write_text(json.dumps({'file': {'failures':10, 'next_attempt':1000+30*86400}}))
    assert prefetch.read_failures(path)['file']['next_attempt'] == 1000


def test_video_proxy_wait_allows_large_objects_without_delaying_photo_timeouts(monkeypatch):
    from contextlib import nullcontext
    calls = []
    class Response:
        def read(self, _size): return b''
    class Opener:
        def open(self, _url, *, timeout):
            calls.append(timeout)
            return nullcontext(Response())
    monkeypatch.setattr(prefetch, 'build_opener', lambda *a: Opener())
    for kind in ['video', 'live_photo', 'photo']:
        prefetch.fetch(prefetch.MediaResource(-1, 'wxid', kind, 'http://example.test/file', ''), 'http://proxy', 0, 0)
    assert calls == [900, 900, 90]


def test_proxy_response_is_published_without_shared_proxy_filesystem(tmp_path):
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    import pytest

    payload = b"\x89PNG\r\n\x1a\nfixture media bytes"
    class Handler(BaseHTTPRequestHandler):
        truncate = False
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(payload) + (5 if self.truncate else 0)))
            self.end_headers()
            self.wfile.write(payload)
        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        item = prefetch.MediaResource(-42, "wxid_test", "photo", "https://example.test/photo", "")
        base = f"http://127.0.0.1:{server.server_port}"
        size = prefetch.fetch(item, base, 0, 0, tmp_path)
        destination = tmp_path / item.cache_name
        assert size == len(payload)
        assert destination.read_bytes() == payload
        assert destination.stat().st_mode & 0o777 == 0o600
        # A truncated retry cannot replace the previous complete cache file.
        Handler.truncate = True
        with pytest.raises(ValueError, match="Incomplete media proxy"):
            prefetch.fetch(item, base, 0, 0, tmp_path)
        assert destination.read_bytes() == payload
        assert list(tmp_path.iterdir()) == [destination]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_cached_repair_persists_one_batch_instead_of_one_fsync_per_photo(tmp_path, monkeypatch):
    import json
    queue = tmp_path / 'queue'
    resources = [prefetch.MediaResource(-i-1, 'wxid', 'photo', f'http://example.test/{i}', '') for i in range(3)]
    for item in resources:
        (tmp_path / item.cache_name).write_bytes(b'image')
    calls = []
    def sync(_fd):
        calls.append(len(queue.read_text().splitlines()))
    monkeypatch.setattr(prefetch.os, 'fsync', sync)
    assert prefetch.publish_cached_moments(resources, tmp_path, queue) == 3
    assert calls == [3]
    assert len([json.loads(line) for line in queue.read_text().splitlines()]) == 3


def test_cached_repair_flushes_completed_records_when_later_lookup_fails(tmp_path, monkeypatch):
    import pytest
    queue = tmp_path / 'queue'
    resources = [prefetch.MediaResource(-i-1, 'wxid', 'photo', f'http://example.test/{i}', '') for i in range(2)]
    for item in resources:
        (tmp_path / item.cache_name).write_bytes(b'image')
    lookup = prefetch.cached_path
    def fail_second(item, cache):
        if item.tid == -2:
            raise OSError('cache read failed')
        return lookup(item, cache)
    monkeypatch.setattr(prefetch, 'cached_path', fail_second)
    calls = []
    monkeypatch.setattr(prefetch.os, 'fsync', lambda _fd: calls.append(len(queue.read_text().splitlines())))
    with pytest.raises(OSError, match='cache read failed'):
        prefetch.publish_cached_moments(resources, tmp_path, queue)
    assert calls == [1]


def test_new_moments_precede_contact_media_and_due_failed_cdn_retries(tmp_path):
    import argparse
    fresh = prefetch.MediaResource(-42, 'wxid', 'photo', 'http://example.test/new', '')
    old = prefetch.MediaResource(-100, 'wxid', 'photo', 'http://example.test/expired', '')
    avatar = prefetch.MediaResource(0, 'wxid', 'avatar_big', 'http://example.test/avatar', '')
    args = argparse.Namespace(cache_dir=tmp_path, limit=None, daily_cap=2)
    failures = {old.cache_name: {'failures':3, 'next_attempt':0}}
    assert prefetch.media_for_today([old, avatar, fresh], args, failures, now=1) == [fresh, avatar]


def test_overlapping_download_batches_merge_only_their_own_failure_updates(tmp_path):
    path = tmp_path / 'failures.json'
    first = {'a': {'failures': 1, 'next_attempt': 0}}
    second = {}  # This producer read the ledger before the first completed.
    prefetch.write_failures(path, first, {'a': first['a']})
    second['b'] = {'failures': 2, 'next_attempt': 0}
    prefetch.write_failures(path, second, {'b': second['b']})
    assert set(prefetch.read_failures(path)) == {'a', 'b'}
    prefetch.write_failures(path, first, {'a': None})
    assert set(prefetch.read_failures(path)) == {'b'}


def test_concurrent_queue_writers_publish_complete_large_records(tmp_path):
    import json
    from concurrent.futures import ThreadPoolExecutor

    queue = tmp_path / 'queue'

    def write(i):
        item = prefetch.MediaResource(-i-1, 'wxid', 'photo', f'http://example.test/{i}', '',
                                     tids=tuple(range(-20000-i, -i)))
        prefetch.append_import_queue(queue, item, 100)

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(write, range(12)))
    records = [json.loads(line) for line in queue.read_text().splitlines()]
    assert {r['tid'] for r in records} == set(range(-12, 0))
    assert all(len(r['tids']) == 20000 for r in records)
