import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import publishing
import collector


def test_local_snapshot_is_atomic_and_preserves_older_bytes(tmp_path):
    file = tmp_path / "profile.json"
    file.write_text("old")
    destination = tmp_path / "published"
    publish = publishing.publisher()
    publish(destination, [(file, "profiles/example/profile.json")])
    old = (destination / "CURRENT").resolve()
    file.write_text("new")
    publish(destination, [(file, "profiles/example/profile.json")])
    assert (old / "profiles/example/profile.json").read_text() == "old"
    assert (destination / "CURRENT/profiles/example/profile.json").read_text() == "new"
    assert (destination / "CURRENT/.crm-ready").is_file()
    current = (destination / "CURRENT").resolve()
    for invalid in ("../escape", "/absolute", ".publish-manifest.json"):
        with pytest.raises(ValueError):
            publish(destination, [(file, invalid)])
        assert (destination / "CURRENT").resolve() == current


def test_incomplete_story_is_not_published_and_retry_keeps_checkpoint(tmp_path, monkeypatch):
    source = tmp_path / "source/example"
    source.mkdir(parents=True)
    manifest = source / "one.manifest.json"
    manifest.write_text(json.dumps({"schema_version": 1, "mediaid": "1", "owner_id": "2", "files": ["one.jpg"]}))
    inventory, incomplete = publishing.complete_manifests(source.parent, "*/*.manifest.json")
    assert inventory == {} and incomplete == 1
    (source / "one.jpg").write_bytes(b"fixture-image")
    inventory, incomplete = publishing.complete_manifests(source.parent, "*/*.manifest.json")
    assert len(inventory) == 2 and incomplete == 0
    state = tmp_path / "state.json"
    def fail(*args):
        raise RuntimeError("publication interrupted")
    monkeypatch.setattr(publishing, "publisher", lambda: fail)
    with pytest.raises(RuntimeError):
        publishing.publish_inventory(source.parent, tmp_path / "destination", state, inventory)
    assert not state.exists()


def test_cooldown_survives_login_and_completed_refresh(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    marker = state / "COOLDOWN_REQUIRED"
    marker.write_text("provider checkpoint")
    assert collector.instagram_blocked(tmp_path) == "access_checkpoint"
    collector.refresh_completed_profiles(tmp_path)
    assert marker.read_text() == "provider checkpoint"


def test_no_login_never_starts_provider_request(tmp_path, monkeypatch):
    monkeypatch.setattr(collector, "run_child", lambda *_args, **_kwargs: pytest.fail("provider request without login"))
    with pytest.raises(collector.NeedsLogin):
        collector.instagram_cycle(tmp_path, tmp_path)
    with pytest.raises(collector.NeedsLogin):
        collector.linkedin_cycle(tmp_path, tmp_path)


def test_vendored_instagram_code_and_data_paths_are_separate(tmp_path, monkeypatch):
    monkeypatch.setenv("MONICA_INSTAGRAM_ROOT", str(tmp_path))
    source = ROOT / "vendor/instagram/archive_followees.py"
    spec = importlib.util.spec_from_file_location("desktop_instagram_followees", source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.BASE_DIR == tmp_path
    assert module.SOURCE_DIR == source.parent


def test_wake_received_during_cycle_starts_one_followup(tmp_path, monkeypatch):
    import time

    observed = collector.wake_signature(tmp_path)
    (tmp_path / "wake").touch()
    monkeypatch.setattr(collector.STOP, "wait", lambda *_: pytest.fail("wake notification should end idle immediately"))
    collector.wait_for_schedule(tmp_path, time.time() + 3600, observed)


def test_instagram_publishes_only_the_current_phase_and_keeps_completed_partial_work(tmp_path, monkeypatch):
    root = tmp_path / "instagram"
    for name in ("state", "sessions", "logs"):
        (root / name).mkdir(parents=True, exist_ok=True)
    (root / "auth.json").write_text('{"username":"fixture"}')
    (root / "sessions/session-fixture").touch()
    runs, publications = [], []
    def child(_root, args, **_kwargs):
        name = Path(args[0]).name
        runs.append(name)
        if name == "archive_highlights.py":
            raise RuntimeError("fixture checkpoint")
    monkeypatch.setattr(collector, "run_child", child)
    monkeypatch.setattr(publishing, "instagram", lambda *args, kinds: publications.append(kinds))
    with pytest.raises(RuntimeError, match="checkpoint"):
        collector.instagram_cycle(root, tmp_path)
    assert runs == ["followee_refresh.py", "archive_stories.py", "archive_highlights.py"]
    assert publications == [("stories",), ("highlights",)]


def test_story_publication_does_not_scan_profile_or_highlight_history(tmp_path, monkeypatch):
    root = tmp_path / "collector"
    folder = root / "data/stories/example"
    folder.mkdir(parents=True)
    (folder / "one.jpg").write_bytes(b"fixture")
    (folder / "one.manifest.json").write_text(json.dumps({"schema_version":1,"mediaid":"1","owner_id":"2","files":["one.jpg"]}))
    scanned = []
    glob = Path.glob
    def tracked(path, pattern):
        scanned.append(path)
        return glob(path, pattern)
    monkeypatch.setattr(Path, "glob", tracked)
    result = publishing.instagram(root, tmp_path / "app", kinds=("stories",))
    assert set(result) == {"stories"} and result["stories"]["files"] == 2
    assert scanned == [root / "data/stories"]


def test_completed_instagram_cycle_scans_each_archive_kind_once(tmp_path, monkeypatch):
    root = tmp_path / "instagram"
    (root / "sessions").mkdir(parents=True)
    (root / "auth.json").write_text('{"username":"fixture"}')
    (root / "sessions/session-fixture").touch()
    publications = []
    monkeypatch.setattr(collector, "run_child", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(publishing, "instagram", lambda *args, kinds: publications.extend(kinds))
    collector.instagram_cycle(root, tmp_path)
    assert publications == ["stories", "highlights", "profiles"]


def linkedin_fixture(root, *, urls=2, size=1024):
    import hashlib
    (root / "assets").mkdir(parents=True)
    (root / "state").mkdir()
    data = b"x" * size
    digest = hashlib.sha256(data).hexdigest()
    path = root / "assets" / (digest + ".png")
    path.write_bytes(data)
    store = collector.linkedin_store(root)
    for number in range(urls):
        store.record_asset(source_url=f"https://media.licdn.com/fixture-{number}.png", content_hash=digest,
            media_type="image/png", byte_count=len(data), local_path=path.name, captured_at="2026-01-01T00:00:00+00:00")
    return store, path


def test_linkedin_verifies_duplicate_content_once_and_skips_unchanged_bytes(tmp_path):
    store, path = linkedin_fixture(tmp_path / "collector", urls=25, size=1024*1024)
    app = tmp_path / "app"
    first = publishing.linkedin(path.parents[1], app, store)
    second = publishing.linkedin(path.parents[1], app, store)
    assert first["hashed_bytes"] == 1024*1024
    assert second["unchanged"] and second["hashed_bytes"] == 0
    # Losing publication state must be recoverable even if source fingerprints
    # are unchanged; rebuilding does not need to rehash source bytes.
    (app / "raw/linkedin/CURRENT").unlink()
    repaired = publishing.linkedin(path.parents[1], app, store)
    assert repaired["files"] == 2 and repaired["hashed_bytes"] == 0


def test_linkedin_cached_asset_corruption_is_detected_even_with_preserved_mtime(tmp_path):
    import os
    root = tmp_path / "collector"
    store, path = linkedin_fixture(root)
    publishing.linkedin(root, tmp_path / "app", store)
    old_cursor = (root / "state/published-linkedin.json").read_bytes()
    before = path.stat()
    path.write_bytes(b"y" * before.st_size)
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
    with pytest.raises(ValueError, match="checksum"):
        publishing.linkedin(root, tmp_path / "app", store)
    assert (root / "state/published-linkedin.json").read_bytes() == old_cursor
