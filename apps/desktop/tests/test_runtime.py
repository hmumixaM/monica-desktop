"""Desktop boundaries that cannot be covered by mocked frontend tests."""

import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "runtime"))
from monica_desktop.config import Config, write_private
from monica_desktop.manager import Manager
from monica_desktop.ops import public_phase
from monica_desktop.processes import Child, identity, recover_owned


class ConfigTests(unittest.TestCase):
    def test_secret_round_trip_and_public_redaction(self):
        with tempfile.TemporaryDirectory() as folder:
            config = Config(Path(folder))
            config.update({"ai": {"api_key": "test-secret"}, "wechat": {"data_key": "a" * 64}})
            config.update({"ai": {"api_key": ""}, "secrets": {"session": "attacker"}})
            public = json.dumps(config.public())
            self.assertNotIn("test-secret", public)
            self.assertNotIn("a" * 64, public)
            self.assertNotIn("secrets", config.public())
            self.assertTrue(config.public()["ai"]["api_key_set"])
            self.assertEqual(Config(Path(folder)).value["ai"]["api_key"], "test-secret")
            self.assertNotEqual(config.value["secrets"]["session"], "attacker")
            self.assertEqual(config.path.stat().st_mode & 0o777, 0o600)

    def test_connection_cannot_be_forged_by_settings(self):
        with tempfile.TemporaryDirectory() as folder:
            config = Config(Path(folder))
            config.update({"wechat": {"connected": True}})
            self.assertFalse(config.value["wechat"]["connected"])
            with self.assertRaises(ValueError):
                config.update({"ai": {"enabled": True}})
            self.assertFalse(config.value["ai"]["enabled"])


class AppStoreEditionTests(unittest.TestCase):
    def test_manual_archive_uses_atomic_snapshots_and_rejects_linked_files(self):
        from monica_desktop.archive_import import stage
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / 'archive'
            (source / 'profiles/alice').mkdir(parents=True)
            profile = source / 'profiles/alice/profile.json'
            profile.write_text('{"username":"alice"}')
            with patch.object(sys, 'path', [str(Path(__file__).resolve().parents[3] / 'infra'), *sys.path]):
                result = stage('instagram', source, root / 'data')
                current = root / 'data/staging/instagram-current/CURRENT'
                previous = current.resolve()
                self.assertEqual(result['files'], 1)
                self.assertTrue((current / '.crm-ready').is_file())
                self.assertEqual((current / 'profiles/alice/profile.json').read_text(), profile.read_text())
                (source / 'profiles/alice/linked.json').symlink_to(profile)
                with self.assertRaisesRegex(ValueError, '符号链接'):
                    stage('instagram', source, root / 'data')
                self.assertEqual(current.resolve(), previous)

    def test_store_edition_cannot_reenable_removed_collectors(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            bundle = root / 'bundle'
            bundle.mkdir()
            (bundle / 'capabilities.json').write_text(json.dumps({
                'distribution': 'app-store-lite', 'disabled_features': ['instagram', 'linkedin', 'faces']}))
            config = Config(root / 'data')
            config.update({'sources': {'linkedin': {'enabled': True}}})
            manager = Manager(bundle, root / 'data', root / 'logs')
            self.assertFalse(manager.config.value['sources']['linkedin']['enabled'])
            # Loading a reduced edition does not destroy standalone settings.
            self.assertTrue(Config(root / 'data').value['sources']['linkedin']['enabled'])
            manager.reset_source_states()
            self.assertEqual(manager.sources['linkedin']['state'], 'unavailable')
            with self.assertRaises(ValueError):
                manager.source_login({'source': 'linkedin'})
            with self.assertRaises(ValueError):
                manager.save_config({'config': {'sources': {'faces': {'enabled': True}}}})
            manager.state = 'running'
            manager.config.value['wechat']['connected'] = True
            with patch.object(manager, 'spawn') as spawn, patch.object(manager, 'start_chatlog'):
                manager.start_background()
            names = [call.args[0] for call in spawn.call_args_list]
            self.assertIn('archive-messages', names)
            self.assertIn('worker-projection', names)
            self.assertNotIn('linkedin-watch', names)
            self.assertNotIn('faces', names)


class ManagerTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.manager = Manager(self.root / "bundle", self.root / "data", self.root / "logs")

    def test_ready_and_new_snapshots_wake_imports_without_unchanged_poll_storm(self):
        manager = self.manager
        manager.state = "running"
        with patch("monica_desktop.manager.Path.touch") as touch:
            manager.observe_snapshots({"snapshots_ready": False, "snapshot_files": 0})
            touch.assert_not_called()
            ready = {"snapshots_ready": True, "snapshot_files": 13, "last_pass_at": "first", "updated_last_pass": 13}
            manager.observe_snapshots(ready)
            manager.observe_snapshots(ready)
            self.assertEqual(touch.call_count, 1)
            manager.observe_snapshots({**ready, "last_pass_at": "unchanged", "updated_last_pass": 0})
            self.assertEqual(touch.call_count, 1)
            manager.observe_snapshots({**ready, "last_pass_at": "new", "updated_last_pass": 1})
            self.assertEqual(touch.call_count, 2)
            manager.state = "paused"
            manager.observe_snapshots({**ready, "last_pass_at": "paused"})
            self.assertEqual(touch.call_count, 2)

    def test_archive_lanes_and_projection_workers_are_independent(self):
        manager = self.manager
        manager.state = 'running'
        manager.config.value['wechat']['connected'] = True
        manager.config.value['sources']['instagram']['enabled'] = True
        manager.config.value['ai']['enabled'] = False
        with patch.object(manager, 'spawn') as spawn, patch.object(manager, 'start_chatlog'), patch.object(manager, 'spawn_collector'):
            manager.start_background()
        names = [call.args[0] for call in spawn.call_args_list]
        self.assertIn('worker-projection', names)
        self.assertNotIn('worker-ai', names)
        for lane in ('contacts', 'moments', 'messages', 'media', 'download', 'instagram', 'instagram-media', 'maintenance'):
            self.assertIn('archive-' + lane, names)
        self.assertNotIn('archive-sync', names)

    def test_external_face_weights_gate_only_the_face_worker(self):
        manager = self.manager
        manager.state = 'running'
        manager.config.value['sources']['faces']['enabled'] = True
        with patch.object(manager, 'spawn') as spawn:
            manager.start_background()
        self.assertEqual(manager.sources['faces']['state'], 'needs_models')
        self.assertNotIn('faces', [call.args[0] for call in spawn.call_args_list])
        self.assertIn('worker-projection', [call.args[0] for call in spawn.call_args_list])
        models = manager.root / 'models/insightface/models/buffalo_l'
        models.mkdir(parents=True)
        for name in ('det_10g.onnx', 'w600k_r50.onnx'):
            (models / name).write_bytes(b'local-model-fixture')
        with patch.object(manager, 'spawn') as spawn:
            manager.start_background()
        self.assertIn('faces', [call.args[0] for call in spawn.call_args_list])
        self.assertEqual(manager.sources['faces']['state'], 'running')

    def test_import_progress_exposes_only_counters_and_detects_stale_phase(self):
        from datetime import UTC, datetime, timedelta
        result = public_phase({"source": "archives", "phase": "messages", "status": "running",
            "heartbeat_at": datetime.now(UTC) - timedelta(minutes=3), "last_success_at": None,
            "details": {"messages_inserted": 500, "processed": "private", "total": -1,
                        "path": "/private/source", "body": "private message", "error": "secret"}})
        self.assertEqual(result["state"], "interrupted")
        self.assertEqual(result["progress"], {"messages_inserted": 500})
        self.assertNotIn("private", json.dumps(result))
        self.assertNotIn("secret", json.dumps(result))

    def test_saving_while_paused_never_starts_background_work(self):
        manager = self.manager
        for executable in ("python/bin/python3", "node/bin/node", "postgres/bin/postgres", "caddy/caddy"):
            path = manager.bundle / executable
            path.parent.mkdir(parents=True, exist_ok=True)
            path.touch()
        (manager.root / "postgres").mkdir()
        (manager.root / "postgres/PG_VERSION").write_text("17")
        manager.state = "paused"
        manager.config.value["sources"]["instagram"]["enabled"] = True

        def stop():
            manager.state = "stopped"

        with patch.object(manager, "stop", side_effect=stop), \
             patch.object(manager, "port", side_effect=range(18000, 18005)), \
             patch.object(manager, "spawn"), patch.object(manager, "oneshot"), \
             patch.object(manager, "wait_http"), patch.object(manager, "db_operation", return_value={}), \
             patch.object(manager, "start_background") as background, \
             patch("monica_desktop.manager.subprocess.run", return_value=SimpleNamespace(returncode=0)):
            manager.save_config({"config": {"timezone": "UTC"}})
        self.assertEqual(manager.state, "paused")
        self.assertEqual(manager.config.value["timezone"], "UTC")
        self.assertEqual(manager.sources["instagram"]["state"], "paused")
        background.assert_not_called()

    def test_account_marker_blocks_switching_even_if_settings_already_changed(self):
        manager = self.manager
        original = self.root / "wechat-original"
        different = self.root / "wechat-different"
        (original / "db_storage").mkdir(parents=True)
        (different / "db_storage").mkdir(parents=True)
        write_private(manager.root / "chatlog/account.json", {"data_dir": str(original)})
        # The immutable marker must remain authoritative after a failed attempt
        # or an older app has already saved a different editable path.
        manager.config.value["wechat"].update(data_dir=str(different), data_key="a" * 64)
        manager.state = "running"
        with patch.object(manager, "stop") as stop, \
             patch.object(manager, "start_chatlog") as start_chatlog, \
             patch.object(manager, "stop_children") as stop_children:
            with self.assertRaisesRegex(ValueError, "一个微信账号"):
                manager.save_config({"config": {"wechat": {"data_dir": str(different)}}})
            with self.assertRaisesRegex(ValueError, "一个微信账号"):
                manager.connect_wechat({"data_dir": str(different)})
            stop.assert_not_called()
            stop_children.assert_not_called()
            start_chatlog.assert_not_called()
        self.assertEqual(json.loads((manager.root / "chatlog/account.json").read_text())["data_dir"], str(original))

    def test_account_marker_accepts_same_account_db_storage_and_symlink(self):
        manager = self.manager
        original = self.root / "wechat-original"
        (original / "db_storage").mkdir(parents=True)
        alias = self.root / "wechat-alias"
        alias.symlink_to(original, target_is_directory=True)
        write_private(manager.root / "chatlog/account.json", {"data_dir": str(original)})
        manager.save_config({"config": {"wechat": {"data_dir": str(alias / "db_storage")}}})
        self.assertEqual(manager.config.value["wechat"]["data_dir"], str(alias / "db_storage"))

    def test_first_failed_connection_still_reserves_snapshot_identity(self):
        manager = self.manager
        account = self.root / "wechat-original"
        (account / "db_storage").mkdir(parents=True)
        manager.config.value["wechat"].update(data_dir=str(account), data_key="a" * 64)
        manager.ports["chatlog"] = 18000
        with patch.object(manager, "spawn"), \
             patch.object(manager, "wait_http", side_effect=RuntimeError("initial export failed")):
            with self.assertRaisesRegex(RuntimeError, "initial export"):
                manager.start_chatlog()
        with self.assertRaisesRegex(ValueError, "一个微信账号"):
            manager.check_wechat_account(self.root / "different-account")
        manager.check_wechat_account(account)

    def test_disabled_sources_do_not_keep_running_or_error_states(self):
        manager = self.manager
        for source in manager.sources.values():
            source.update(state="running", error="old connection error")
        manager.config.value["sources"]["instagram"]["enabled"] = True
        manager.reset_source_states(paused=True)
        self.assertEqual(manager.sources["instagram"]["state"], "paused")
        for name in ("wechat", "linkedin", "faces", "ai"):
            self.assertEqual(manager.sources[name]["state"], "disconnected")
            self.assertEqual(manager.sources[name]["error"], "")

    def test_monitor_does_not_restore_stale_disabled_collector_state(self):
        manager = self.manager
        manager.state = "running"
        write_private(manager.root / "collectors/instagram/status.json", {"state": "running", "error": "stale"})
        manager.closed = Mock()
        manager.closed.wait.side_effect = [False, True]
        with patch.object(manager, "db_operation", return_value={}), \
             patch("monica_desktop.manager.time.monotonic", return_value=100):
            manager.monitor()
        self.assertEqual(manager.sources["instagram"]["state"], "disconnected")
        self.assertEqual(manager.sources["instagram"]["error"], "")


class ProcessTests(unittest.TestCase):
    def test_repeated_stop_does_not_reenter_cooperative_signal_handler(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            program = """import pathlib, signal, threading, time
root = pathlib.Path.cwd()
lock = threading.Lock()
stopped = False
def stop(*_):
    global stopped
    with lock:
        (root / 'received').write_text('one cooperative stop')
        time.sleep(.2)
        stopped = True
signal.signal(signal.SIGTERM, stop)
(root / 'ready').touch()
while not stopped:
    time.sleep(.01)
"""
            child = Child("drain", [sys.executable, "-c", program], dict(os.environ), root, root / "test.log")
            child.start()
            try:
                deadline = time.monotonic() + 5
                while not (root / "ready").exists() and time.monotonic() < deadline:
                    time.sleep(.01)
                self.assertTrue((root / "ready").exists())
                child.signal_stop()
                while not (root / "received").exists() and time.monotonic() < deadline:
                    time.sleep(.01)
                self.assertTrue((root / "received").exists())
                child.signal_stop()
                child.finish_stop(time.monotonic() + 1)
                self.assertEqual(child.process.returncode, 0)
            finally:
                if child.process.poll() is None:
                    child.process.kill()
                    child.process.wait(timeout=5)

    def test_restart_backoff_is_bounded(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            child = Child("crash", [sys.executable, "-c", "raise SystemExit(2)"], dict(os.environ), root, root / "test.log")
            for _ in range(5):
                child.start()
                child.process.wait(timeout=5)
                child.poll()
            self.assertEqual(child.state, "failed")
            child.poll()
            self.assertIsNotNone(child.process.returncode)

    def test_orphan_recovery_rejects_reused_pid(self):
        with tempfile.TemporaryDirectory() as folder:
            journal = Path(folder) / "processes.json"
            process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], start_new_session=True)
            try:
                write_private(journal, [{"name": "other", "pid": process.pid, "identity": "different owner"}])
                recover_owned(journal)
                self.assertIsNone(process.poll())
            finally:
                process.terminate()
                process.wait(timeout=5)


class ControlTests(unittest.TestCase):
    def test_control_requires_native_token_and_rejects_browser_origin(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            environment = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1] / "runtime"))
            process = subprocess.Popen([sys.executable, "-m", "monica_desktop", "serve", "--bundle-root", folder,
                "--data-root", str(root / "data"), "--logs-root", str(root / "logs"), "--no-start"], env=environment,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            try:
                control = root / "data/run/control.json"
                deadline = time.monotonic() + 10
                while not control.exists() and time.monotonic() < deadline:
                    time.sleep(0.05)
                self.assertTrue(control.exists())
                info = json.loads(control.read_text())
                url = f"http://127.0.0.1:{info['port']}/control/status"
                for headers, expected in (({}, 401), ({"Authorization": "Bearer " + info["token"], "Origin": "https://example.com"}, 403)):
                    with self.assertRaises(HTTPError) as caught:
                        urlopen(Request(url, headers=headers), timeout=2)
                    self.assertEqual(caught.exception.code, expected)
                headers = {"Authorization": "Bearer " + info["token"]}
                with urlopen(Request(url, headers=headers), timeout=2) as response:
                    self.assertEqual(json.load(response)["state"], "stopped")
                req = Request(url.replace("status", "shutdown"), headers=headers, data=b"{}")
                with urlopen(req, timeout=2) as response:
                    self.assertEqual(response.status, 202)
                process.wait(timeout=10)
                self.assertEqual(process.returncode, 0)
                self.assertFalse(control.exists())
            finally:
                if process.poll() is None:
                    process.terminate()
                process.communicate(timeout=10)


if __name__ == "__main__":
    unittest.main()
