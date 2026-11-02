"""The desktop owns the processes; the existing CRM owns all business data."""

import json
import os
import socket
import subprocess
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen

from .config import Config, discover_wechat, write_private
from .processes import Child, recover_owned, save_owned


def now():
    return datetime.now(UTC).isoformat()


def request_json(url, body=None, timeout=5):
    headers = {"Content-Type": "application/json"} if body is not None else {}
    data = json.dumps(body).encode() if body is not None else None
    try:
        with urlopen(Request(url, data=data, headers=headers), timeout=timeout) as response:
            return json.load(response)
    except HTTPError as exc:
        # Avoid exposing upstream response bodies (which can contain credentials).
        raise ValueError(f"请求未成功（HTTP {exc.code}）") from None


class Manager:
    def __init__(self, bundle: Path, root: Path, logs: Path):
        self.bundle, self.root, self.logs = bundle.resolve(), root.resolve(), logs.resolve()
        for path in (root, logs, root / "run", root / "raw", root / "media", root / "staging",
                     root / "exports", root / "backups", root / "chatlog", root / "chatlog/work",
                     root / "chatlog/cache", root / "sns-cache", root / "collectors", root / "faces"):
            path.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(root, 0o700)
        self.config = Config(root)
        self.children: dict[str, Child] = {}
        self.ports = {}
        self.state, self.error, self.operation = "stopped", "", ""
        self.action_lock = threading.Lock()
        self.lock = threading.RLock()
        self.closed = threading.Event()
        self.metrics = {"account_created": False, "profiles": 0, "pending_jobs": 0,
                        "posts_imported": 0, "messages_imported": 0, "import_phases": []}
        self.snapshot_observation = None
        self.sources = {source: {"name": source, "state": "disconnected", "error": ""}
                        for source in ("wechat", "instagram", "linkedin", "faces", "ai")}
        self.last_metrics = 0
        self.python = str(bundle / "python/bin/python3")
        self.drain_seconds = int(os.environ.get("MONICA_DESKTOP_DRAIN_SECONDS", "240"))
        self.journal = root / "run/processes.json"
        recover_owned(self.journal)

    def port(self, preferred=0):
        for port in (preferred, 0):
            with socket.socket() as sock:
                try:
                    sock.bind(("127.0.0.1", port))
                except OSError:
                    continue
                chosen = sock.getsockname()[1]
                if chosen not in self.ports.values():
                    return chosen
        raise RuntimeError("无法分配本地端口")

    @property
    def web_url(self):
        return f"http://127.0.0.1:{self.ports['web']}" if "web" in self.ports else ""

    def face_model_root(self):
        bundled = self.bundle / 'models/insightface'
        local = self.root / 'models/insightface'
        for root in (bundled, local):
            if all((root / 'models/buffalo_l' / name).is_file() for name in ('det_10g.onnx', 'w600k_r50.onnx')):
                return root
        return local

    def environment(self):
        config = self.config.value
        # Never inherit shell DATABASE_URL, Python packages or server credentials.
        env = {key: os.environ[key] for key in ("HOME", "USER", "TMPDIR", "LANG", "LC_ALL") if key in os.environ}
        env.update({
            "PATH": os.pathsep.join([str(self.bundle / "python/bin"), str(self.bundle / "node/bin"),
                                     str(self.bundle / "postgres/bin"), "/usr/bin", "/bin", "/usr/sbin", "/sbin"]),
            "PYTHONHOME": str(self.bundle / "python"), "PYTHONNOUSERSITE": "1", "PYTHONUNBUFFERED": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONPATH": os.pathsep.join(str(self.bundle / path) for path in ("desktop", "api", "faces", "collectors", "infra")),
            "APP_ENV": "desktop", "DESKTOP_ORIGIN": self.web_url,
            "CORS_ORIGINS": self.web_url, "CRM_TIMEZONE": config["timezone"],
            "SESSION_SECRET": config["secrets"]["session"], "IMPORT_HMAC_SECRET": config["secrets"]["import"],
            "LINKEDIN_COLLECTION_TOKEN": config["secrets"]["linkedin"], "MONICA_API_URL": self.web_url,
            "MONICA_SNAPSHOT_PUBLISHER": str(self.bundle / "infra/snapshot_publish.py"),
            "MIGRATIONS_ROOT": str(self.bundle / "migrations"),
            "RAW_ROOT": str(self.root / "raw"), "MEDIA_ROOT": str(self.root / "media"),
            "STAGING_ROOT": str(self.root / "staging"), "EXPORT_ROOT": str(self.root / "exports"),
            "CHATLOG_DB_ROOT": str(self.root / "chatlog/work/db_storage"),
            "SNS_CACHE_ROOT": str(self.root / "sns-cache"),
            "WECHAT_PREFETCH_SCRIPT": str(self.bundle / "wechat/prefetch-sns-images.py"),
            "WECHAT_MEDIA_IMPORT_QUEUE": str(self.root / "sns-cache/media-import-queue.jsonl"),
            "CHATLOG_ALPHA_URL": f"http://127.0.0.1:{self.ports.get('chatlog', 5030)}",
            "CHATLOG_BASE_URL": f"http://127.0.0.1:{self.ports.get('chatlog', 5030)}",
            "ARCHIVE_SYNC_SECONDS": "300", "WECHAT_PREFETCH_LIMIT": "1000",
            "CDC_ENABLED": "true", "ASYNC_PROJECTION_ENABLED": "true", "MONICA_ARCHIVE_SEPARATED": "1",
            "AI_ENABLED": str(config["ai"]["enabled"]).lower(),
            "LITELLM_BASE_URL": config["ai"]["base_url"], "LITELLM_API_KEY": config["ai"]["api_key"],
            "AI_BATCH_MODEL": config["ai"]["batch_model"], "AI_PERSON_MODEL": config["ai"]["batch_model"],
            "AI_WRITER_MODEL": config["ai"]["batch_model"], "AI_VISION_MODEL": config["ai"]["vision_model"],
            "MONICA_FACES_API": self.web_url, "MONICA_FACES_SECRET": config["secrets"]["import"],
            "MONICA_FACES_TOKEN": config["secrets"]["faces_token"], "MONICA_FACES_CACHE": str(self.root / "faces"),
            "MONICA_FACES_MODEL_ROOT": str(self.face_model_root()),
            "MONICA_FACES_PROVIDERS": '["CoreMLExecutionProvider","CPUExecutionProvider"]',
            "OMP_NUM_THREADS": "2", "OPENBLAS_NUM_THREADS": "2",
            "PLAYWRIGHT_BROWSERS_PATH": str(self.bundle / "browsers"),
            "MONICA_DESKTOP_DATA_ROOT": str(self.root), "NODE_ENV": "production",
            "MONICA_DESKTOP_WAKE_FILE": str(self.root / "run/wake"),
            "CADDY_DATA_DIR": str(self.root / "caddy"), "XDG_DATA_HOME": str(self.root / "xdg"),
            "XDG_CONFIG_HOME": str(self.root / "config"),
        })
        if "postgres" in self.ports:
            password = quote(config["secrets"]["database"], safe="")
            env["DATABASE_URL"] = f"postgresql+psycopg://monica:{password}@127.0.0.1:{self.ports['postgres']}/crm"
        return env

    def spawn(self, name, command, *, extra=None, background=False, restart=True):
        with self.lock:
            current = self.children.get(name)
            if current and current.process and current.process.poll() is None:
                return current
            env = self.environment()
            env.update(extra or {})
            child = Child(name, list(map(str, command)), env, self.root, self.logs / f"{name}.log", background, restart)
            self.children[name] = child
            child.start()
            save_owned(self.journal, self.children.values())
            return child

    def oneshot(self, name, command, timeout=180):
        child = self.spawn(name, command, restart=False)
        try:
            code = child.process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self.stop_children([child], grace=5)
            raise RuntimeError(f"{name} 超时；请查看日志") from None
        child.state = "completed" if code == 0 else "failed"
        if code:
            raise RuntimeError(f"{name} 失败；请查看 {child.log_path.name}")

    def db_operation(self, operation, timeout=15):
        result = subprocess.run([self.python, "-m", "monica_desktop.ops", operation], env=self.environment(), cwd=self.root,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
        if result.returncode:
            log = self.logs / f"{operation}.log"
            with log.open("ab") as out:
                os.chmod(log, 0o600)
                out.write(result.stderr)
            raise RuntimeError(f"{operation} 未成功；请查看 {log.name}")
        return json.loads(result.stdout.splitlines()[-1])

    def wait_http(self, url, name, timeout=60):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            child = self.children.get(name)
            if child and child.process.poll() is not None:
                raise RuntimeError(f"{name} 启动失败；请查看 {child.log_path.name}")
            try:
                with urlopen(url, timeout=2) as response:
                    if response.status == 200:
                        return
            except (OSError, ValueError, HTTPError):
                pass
            if self.closed.wait(0.4):
                raise RuntimeError("应用正在退出")
        raise RuntimeError(f"{name} 尚未就绪；请查看日志或检查配置")

    def start(self, *, paused=False):
        if self.state in {"running", "paused"}:
            return
        if any(child.process and child.process.poll() is None for child in self.children.values()):
            self.stop()
        self.state, self.error = "starting", ""
        for executable in ("python/bin/python3", "node/bin/node", "postgres/bin/postgres", "caddy/caddy"):
            if not (self.bundle / executable).is_file():
                raise RuntimeError(f"安装包缺少运行组件：{executable}")
        self.ports = {}
        for name in ("web", "postgres", "api", "next", "chatlog"):
            self.ports[name] = self.port(self.config.value["web_port"] if name == "web" else 0)
        pg = self.root / "postgres"
        pg_bin = self.bundle / "postgres/bin"
        if not (pg / "PG_VERSION").exists():
            password = self.root / "run/initdb-password"
            password.write_text(self.config.value["secrets"]["database"])
            os.chmod(password, 0o600)
            try:
                self.oneshot("database-init", [pg_bin / "initdb", "-L", self.bundle / "postgres/share/postgresql@16",
                    "-D", pg, "-U", "monica", "--encoding=UTF8",
                    "--locale=C", "--auth-local=trust", "--auth-host=scram-sha-256", "--pwfile", password])
            finally:
                password.unlink(missing_ok=True)
        self.spawn("postgres", [pg_bin / "postgres", "-D", pg, "-h", "127.0.0.1", "-p", self.ports["postgres"],
            "-k", "", "-c", "max_connections=120", "-c", "shared_buffers=128MB",
            "-c", "wal_level=logical", "-c", "max_replication_slots=8",
            "-c", "max_slot_wal_keep_size=512MB"], restart=False)
        deadline = time.monotonic() + 30
        while True:
            result = subprocess.run([str(pg_bin / "pg_isready"), "-h", "127.0.0.1", "-p", str(self.ports["postgres"]), "-U", "monica"],
                                    env=self.environment(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if result.returncode == 0:
                break
            if time.monotonic() > deadline or self.children["postgres"].process.poll() is not None:
                raise RuntimeError("数据库未就绪；请查看 postgres.log")
            time.sleep(0.3)
        self.oneshot("database-migrate", [self.python, "-m", "monica_desktop.ops", "initialize"], timeout=300)
        self.spawn("api", [self.python, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", self.ports["api"], "--no-access-log"])
        self.wait_http(f"http://127.0.0.1:{self.ports['api']}/api/v1/ready", "api")
        self.spawn("web", [self.bundle / "node/bin/node", self.bundle / "web/apps/web/server.js"],
                   extra={"PORT": str(self.ports["next"]), "HOSTNAME": "127.0.0.1"})
        self.wait_http(f"http://127.0.0.1:{self.ports['next']}/login", "web")
        caddy = self.root / "run/Caddyfile"
        caddy.write_text("{\n admin off\n auto_https off\n}\n" + self.web_url + " {\n bind 127.0.0.1\n"
            + f" handle /api/* {{\n  reverse_proxy 127.0.0.1:{self.ports['api']}\n }}\n"
            + f" handle /health {{\n  reverse_proxy 127.0.0.1:{self.ports['api']}\n }}\n"
            + f" handle {{\n  reverse_proxy 127.0.0.1:{self.ports['next']}\n }}\n}}\n")
        self.spawn("gateway", [self.bundle / "caddy/caddy", "run", "--config", caddy, "--adapter", "caddyfile"])
        self.wait_http(self.web_url + "/api/v1/ready", "gateway")
        self.metrics.update(self.db_operation("status"))
        self.state = "paused" if paused else "running"
        # Capture stays light and independent of paused projections. This keeps
        # replication slots from retaining WAL while the user browses paused.
        self.spawn('cdc-capture', [self.python, '-m', 'app.cdc', 'watch'])
        self.reset_source_states(paused=paused)
        if not paused:
            self.start_background()

    def reset_source_states(self, *, paused=False):
        enabled = {"wechat": self.config.value["wechat"]["connected"],
                   "ai": self.config.value["ai"]["enabled"],
                   **{key: value["enabled"] for key, value in self.config.value["sources"].items()}}
        for name, source in self.sources.items():
            if paused or not enabled[name]:
                source.update(state="paused" if enabled[name] else "disconnected", error="")

    def start_background(self):
        if self.state != "running":
            return
        self.reset_source_states()
        self.spawn('source-dispatch', [self.python, '-m', 'app.projection_feed', 'watch'], background=True)
        for resource in ("db", "media", "projection", "ai", "scheduler"):
            if resource == "ai" and not self.config.value["ai"]["enabled"]:
                continue
            self.spawn("worker-" + resource, [self.python, self.bundle / "worker.py"],
                       extra={"WORKER_RESOURCE": resource}, background=True)
        self.spawn('worker-projection-2', [self.python, self.bundle / 'worker.py'],
                   extra={'WORKER_RESOURCE': 'projection'}, background=True)
        if self.config.value["ai"]["enabled"]:
            self.spawn("image-ocr", [self.python, "-m", "app.ai.image_ocr"], background=True)
            self.sources["ai"]["state"] = "running"
        if self.config.value["wechat"]["connected"]:
            try:
                self.start_chatlog()
            except Exception as exc:
                self.sources["wechat"].update(state="error", error=str(exc))
        self.start_archive_lanes()
        for provider in ("instagram", "linkedin"):
            if self.config.value["sources"][provider]["enabled"]:
                self.spawn_collector(provider, "watch")
        if self.config.value["sources"]["instagram"]["enabled"]:
            self.spawn("story-sync", [self.python, "-m", "app.sync_instagram_stories", "--watch", "--interval", "28800"], background=True)
        if self.config.value["sources"]["linkedin"]["enabled"]:
            self.spawn("linkedin-sync", [self.python, "-m", "app.sync_linkedin", "--source",
                self.root / "raw/linkedin/linkedin_enrichment.incoming.json", "--watch"], background=True)
        if self.config.value["sources"]["faces"]["enabled"]:
            models = self.face_model_root() / 'models/buffalo_l'
            if all((models / name).is_file() for name in ('det_10g.onnx', 'w600k_r50.onnx')):
                self.spawn("faces", [self.python, "-m", "monica_faces.run", "watch"], background=True)
                self.sources["faces"].update(state="running", error="")
            else:
                self.sources["faces"].update(state="needs_models", error="请将 det_10g.onnx 和 w600k_r50.onnx 放入数据目录的 models/insightface/models/buffalo_l/，然后恢复任务。")

    def start_archive_lanes(self):
        lanes = []
        if self.config.value['wechat']['connected']:
            lanes.extend(('contacts', 'moments', 'messages', 'media', 'download'))
        if self.config.value['sources']['instagram']['enabled']:
            lanes.extend(('instagram', 'instagram-media'))
        if lanes:
            lanes.append('maintenance')
        for lane in lanes:
            self.spawn('archive-' + lane, [self.python, '-m', 'app.sync_archives', '--lane', lane], background=True)

    def stop_children(self, children, grace=None):
        deadline = time.monotonic() + (self.drain_seconds if grace is None else grace)
        for child in children:
            child.signal_stop()
        for child in children:
            child.finish_stop(deadline)

    def pause(self):
        if self.state not in {"running", "paused"}:
            raise ValueError("服务尚未运行，请先启动")
        self.state = "pausing"
        self.stop_children([child for child in self.children.values() if child.background])
        for source in self.sources.values():
            if source["state"] in {"running", "connected"}:
                source["state"] = "paused"
        self.state = "paused"

    def resume(self):
        if self.state not in {"running", "paused"}:
            return self.start()
        self.state = "running"
        self.start_background()
        self.wake()

    def stop(self):
        self.state = "stopping"
        self.stop_children([child for child in self.children.values() if child.background])
        self.stop_children([child for child in self.children.values() if not child.background and child.name != "postgres"], grace=15)
        pg = self.children.get("postgres")
        if pg and pg.process and pg.process.poll() is None:
            # Fast shutdown rolls back active transactions but writes a clean checkpoint.
            subprocess.run([str(self.bundle / "postgres/bin/pg_ctl"), "-D", str(self.root / "postgres"),
                            "stop", "-m", "fast", "-w", "-t", "30"], env=self.environment(),
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=35)
            pg.finish_stop(time.monotonic() + 5)
        self.state = "stopped"
        save_owned(self.journal, self.children.values())

    def start_chatlog(self):
        settings = self.config.value["wechat"]
        path = Path(settings["data_dir"])
        source = path.parent if path.name == "db_storage" else path
        self.check_wechat_account(source)
        if not (source / "db_storage").is_dir():
            raise ValueError("微信目录中没有 db_storage，请重新选择账号目录")
        # Reserve before the child can publish even a partial snapshot. A failed
        # readiness check still belongs to this account and can be retried.
        write_private(self.root / "chatlog/account.json", {"data_dir": str(source.resolve())})
        private = self.root / "chatlog/config"
        write_private(private / "chatlog-server.json", {
            "platform": "darwin", "version": 4, "data_dir": str(source),
            "work_dir": str(self.root / "chatlog/work"), "data_key": settings["data_key"], "img_key": settings["img_key"],
            "http_addr": f"127.0.0.1:{self.ports['chatlog']}", "auto_decrypt": True,
            "wal_enabled": True, "auto_decrypt_debounce": 1, "save_decrypted_media": False,
        })
        self.spawn("chatlog", [self.bundle / "chatlog/chatlog", "serve", "--config-dir", private],
                   extra={"CHATLOG_WCDB_CACHE_DIR": str(self.root / "chatlog/cache")}, background=True, restart=False)
        self.sources["wechat"].update(state="connecting", error="")
        self.wait_http(f"http://127.0.0.1:{self.ports['chatlog']}/api/v1/ready", "chatlog", timeout=120)
        # Authentication/configuration errors don't loop on first connection;
        # a verified daemon can recover from subsequent transient crashes.
        self.children["chatlog"].restart = True
        self.sources["wechat"].update(state="connected", last_success=now(), error="")

    def observe_snapshots(self, value):
        observation = (value.get("last_pass_at"), value.get("snapshot_files"), value.get("snapshots_ready"))
        previous = self.snapshot_observation
        changed = observation != previous
        ready_now = value.get("snapshots_ready") and (previous is None or not previous[2])
        if changed and self.state == "running" and (ready_now or value.get("updated_last_pass", 0) > 0):
            # Wake only the importer; don't enqueue analysis or provider scans
            # every time the snapshot exporter checks an unchanged source.
            (self.root / "run/wake").touch()
        self.snapshot_observation = observation

    def check_wechat_account(self, source):
        # Persist identity separately from editable settings: a failed connection
        # or a settings save must never allow mixing two accounts' snapshots.
        marker = self.root / "chatlog/account.json"
        if marker.exists() and Path(json.loads(marker.read_text())["data_dir"]).resolve() != source.resolve():
            raise ValueError("实验版每个数据目录连接一个微信账号；切换账号请使用独立的 Monica 数据目录")

    def connect_wechat(self, payload):
        if self.state not in {"running", "paused"}:
            self.start()
        path = Path(payload.get("data_dir") or self.config.value["wechat"]["data_dir"]).expanduser().resolve()
        if path.name == "db_storage":
            path = path.parent
        if not (path / "db_storage").is_dir():
            raise ValueError("请选择包含 db_storage 的微信账号目录")
        candidate = dict(self.config.value["wechat"])
        file = payload.get("config_file") or str(path / "chatlog.json")
        if Path(file).is_file():
            raw = json.loads(Path(file).read_text())
            if not isinstance(raw, dict):
                raise ValueError("Chatlog 配置格式不正确")
            for key in ("data_key", "img_key"):
                if isinstance(raw.get(key), str) and raw[key]:
                    candidate[key] = raw[key]
        candidate.update({key: value for key, value in payload.items() if key in {"data_key", "img_key"} and value})
        if len(candidate.get("data_key", "")) != 64:
            raise ValueError("目录已找到，需要有效的 64 位数据库密钥；请选择已有 Chatlog 配置或填写密钥")
        try:
            bytes.fromhex(candidate["data_key"])
        except ValueError:
            raise ValueError("数据库密钥应为 64 位十六进制字符串") from None
        candidate.update(data_dir=str(path), config_file=file, connected=False)
        # Do not reuse another account's decrypted snapshots or import cursors.
        self.check_wechat_account(path)
        self.stop_children([child for name, child in self.children.items() if name == 'chatlog' or name.startswith('archive-')])
        self.config.value["wechat"] = candidate
        self.config.save()
        try:
            self.start_chatlog()
        except Exception as exc:
            if "chatlog" in self.children:
                self.stop_children([self.children["chatlog"]], grace=10)
            self.sources["wechat"].update(state="error", error=str(exc))
            raise
        write_private(self.root / "chatlog/account.json", {"data_dir": str(path)})
        self.config.value["wechat"]["connected"] = True
        self.config.save()
        if self.state == "paused":
            self.stop_children([self.children["chatlog"]])
            self.sources["wechat"]["state"] = "paused"
        else:
            self.start_archive_lanes()

    def spawn_collector(self, provider, action):
        if provider not in {"instagram", "linkedin"}:
            raise ValueError("不支持此来源")
        command = [self.python, self.bundle / "collectors/collector.py", "--provider", provider, "--data-root", self.root]
        username = self.config.value["sources"][provider].get("username")
        if provider == "instagram" and username:
            command += ["--username", username]
        command += [action]
        child = self.spawn(f"{provider}-{action}", command, background=True, restart=action == "watch")
        self.sources[provider].update(state="connecting" if action == "login" else "running", error="")
        return child

    def source_login(self, payload):
        provider = payload.get("source")
        if provider not in {"instagram", "linkedin"}:
            raise ValueError("请选择 Instagram 或 LinkedIn")
        if self.state not in {"running", "paused"}:
            self.start()
        if payload.get("username"):
            self.config.update({"sources": {provider: {"username": payload["username"]}}})
        self.stop_children([child for name, child in self.children.items() if name.startswith(provider + "-")])
        child = self.spawn_collector(provider, "login")
        try:
            code = child.process.wait(timeout=600)
        except subprocess.TimeoutExpired:
            self.stop_children([child], grace=5)
            raise ValueError("登录超时，请重新打开登录窗口") from None
        child.state = "completed" if code == 0 else "failed"
        if code:
            self.sources[provider].update(state="error", error="尚未完成登录，请重试并处理浏览器中的验证")
            raise ValueError(self.sources[provider]["error"])
        self.config.update({"sources": {provider: {"enabled": True}}})
        self.sources[provider].update(state="connected", last_success=now(), error="")
        self.start_background()

    def collect(self, payload):
        provider = payload.get("source")
        if self.state != "running":
            raise ValueError("请先恢复后台运行")
        if provider == "wechat":
            return self.wake()
        watch = self.children.get(f"{provider}-watch")
        if watch and watch.process and watch.process.poll() is None:
            # The adapter's wake file preserves budget/checkpoint ownership.
            (self.root / "collectors" / provider / "wake").touch()
        else:
            self.spawn_collector(provider, "once")

    def wake(self):
        if self.state != "running":
            return
        self.db_operation("wake", timeout=20)
        # Tell source loops to run a due scan without restarting in-flight work.
        (self.root / "run/wake").touch()
        for provider in ("instagram", "linkedin"):
            folder = self.root / "collectors" / provider
            if folder.exists():
                (folder / "wake").touch()

    def create_account(self, payload):
        if self.state not in {"running", "paused"}:
            self.start()
        request_json(self.web_url + "/api/v1/auth/bootstrap", {"email": payload.get("email", ""), "password": payload.get("password", "")}, timeout=20)
        self.metrics.update(self.db_operation("status"))

    def save_config(self, payload):
        old_state = self.state
        proposed = payload.get("config", {}).get("wechat", {}).get("data_dir")
        if proposed:
            source = Path(proposed).expanduser()
            self.check_wechat_account(source.parent if source.name == "db_storage" else source)
        self.config.update(payload.get("config", {}))
        if old_state in {"running", "paused"}:
            self.stop()
            self.start(paused=old_state == "paused")

    def test_ai(self):
        result = self.db_operation("test-ai", timeout=75)
        self.sources["ai"].update(state="connected", last_success=now(), error="")
        return result

    def status(self):
        with self.lock:
            return {"state": self.state, "operation": self.operation, "error": self.error,
                    "web_url": self.web_url if self.state in {"running", "paused", "pausing"} else "",
                    "services": [child.public() for child in self.children.values()],
                    "sources": list(self.sources.values()), "config": self.config.public(),
                    "configured": bool(self.metrics["account_created"]), **self.metrics,
                    "wechat_candidates": discover_wechat(), "logs_dir": str(self.logs)}

    def dispatch(self, command, payload):
        methods = {"start": self.start, "stop": self.stop, "pause": self.pause, "resume": self.resume,
                   "wake": self.wake, "test_ai": self.test_ai,
                   "save_config": lambda: self.save_config(payload), "connect_wechat": lambda: self.connect_wechat(payload),
                   "source_login": lambda: self.source_login(payload), "collect": lambda: self.collect(payload),
                   "create_account": lambda: self.create_account(payload)}
        if command not in methods:
            raise ValueError("未知操作")
        if not self.action_lock.acquire(blocking=False):
            raise ValueError("上一项操作正在进行，请稍候")
        self.operation, self.error = command, ""

        def run():
            try:
                methods[command]()
            except Exception as exc:
                self.error = str(exc)
                if self.state in {"starting", "stopping"}:
                    try:
                        self.stop()
                    except Exception:
                        pass
                    self.state = "error"
            finally:
                self.operation = ""
                self.action_lock.release()
        threading.Thread(target=run, name=command, daemon=True).start()
        return {"accepted": True}

    def monitor(self):
        while not self.closed.wait(2):
            if self.action_lock.locked():
                continue
            with self.lock:
                before = [(name, child.process.pid if child.process else None) for name, child in self.children.items()]
                for child in list(self.children.values()):
                    child.poll()
                pg = self.children.get("postgres")
                if self.state in {"running", "paused"} and pg and pg.process and pg.process.poll() is not None:
                    self.error = "数据库已停止，请查看日志后重新启动"
                    self.state = "error"
                after = [(name, child.process.pid if child.process else None) for name, child in self.children.items()]
                if before != after:
                    save_owned(self.journal, self.children.values())
            if self.state in {"running", "paused"} and time.monotonic() - self.last_metrics > 10:
                self.last_metrics = time.monotonic()
                try:
                    self.metrics.update(self.db_operation("status"))
                except Exception:
                    pass
                if self.config.value["wechat"]["connected"] and self.state == "running":
                    try:
                        value = request_json(f"http://127.0.0.1:{self.ports['chatlog']}/api/v1/desktop/status")
                        self.sources["wechat"]["details"] = value
                        self.observe_snapshots(value)
                        if value.get("failed_files"):
                            self.sources["wechat"].update(state="error", error=f"有 {value['failed_files']} 个微信数据库快照更新失败，请查看日志")
                        else:
                            self.sources["wechat"].update(state="connected" if value.get("snapshots_ready") else "connecting", error="")
                            if value.get("last_pass_at"):
                                self.sources["wechat"]["last_success"] = value["last_pass_at"]
                    except Exception:
                        self.sources["wechat"].update(state="error", error="Chatlog 未响应，请查看微信连接状态")
                for provider in ("instagram", "linkedin") if self.state == "running" else ():
                    if not self.config.value["sources"][provider]["enabled"]:
                        continue
                    path = self.root / "collectors" / provider / "status.json"
                    if path.is_file():
                        try:
                            data = json.loads(path.read_text())
                            self.sources[provider].update({key: data[key] for key in ("state", "last_success", "error") if key in data})
                        except (OSError, ValueError):
                            pass
