"""Authenticated private control plane for the native tray, never the web UI."""

import argparse
import fcntl
import hmac
import json
import os
import secrets
import signal
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .config import write_private
from .manager import Manager


def serve(args):
    os.umask(0o077)
    args.data_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock = (args.data_root / "instance.lock").open("a")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise SystemExit("Monica 已在此数据目录运行") from None
    logs = args.logs_root or Path.home() / "Library/Logs/Monica"
    manager = Manager(args.bundle_root, args.data_root, logs)
    token = secrets.token_urlsafe(48)
    shutdown_requested = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def reply(self, code, value):
            encoded = json.dumps(value, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            try:
                self.wfile.write(encoded)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def authorized(self):
            # Native Rust sends no Origin. Browsers never receive this token.
            if self.headers.get("Origin") or self.headers.get("Host") != f"127.0.0.1:{server.server_port}":
                self.reply(403, {"error": "拒绝浏览器控制请求"})
                return False
            value = self.headers.get("Authorization", "")
            if not hmac.compare_digest(value, "Bearer " + token):
                self.reply(401, {"error": "控制凭据无效"})
                return False
            return True

        def do_GET(self):
            if not self.authorized():
                return
            if self.path != "/control/status":
                return self.reply(404, {"error": "未知路径"})
            self.reply(200, manager.status())

        def do_POST(self):
            if not self.authorized():
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 <= length <= 128 * 1024:
                    return self.reply(413, {"error": "请求过大"})
                payload = json.loads(self.rfile.read(length) or b"{}")
                if not isinstance(payload, dict):
                    raise ValueError("请求必须是对象")
                command = self.path.removeprefix("/control/")
                if not self.path.startswith("/control/"):
                    return self.reply(404, {"error": "未知路径"})
                if command == "shutdown":
                    self.reply(202, {"accepted": True})
                    shutdown_requested.set()
                    return
                self.reply(202, manager.dispatch(command, payload))
            except (ValueError, TypeError) as exc:
                self.reply(400, {"error": str(exc)})

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    control_file = args.data_root / "run/control.json"
    write_private(control_file, {"port": server.server_port, "token": token, "pid": os.getpid()})
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: shutdown_requested.set())
    threading.Thread(target=server.serve_forever, daemon=True).start()
    threading.Thread(target=manager.monitor, daemon=True).start()
    if not args.no_start:
        manager.dispatch("start", {})
    try:
        while not shutdown_requested.wait(2):
            if args.parent_pid:
                try:
                    os.kill(args.parent_pid, 0)
                except ProcessLookupError:
                    break
    finally:
        manager.closed.set()
        # Cancel a pending headed login/collection so shutdown cannot wait ten minutes.
        for child in list(manager.children.values()):
            if child.background:
                child.signal_stop()
        with manager.action_lock:
            manager.stop()
        server.shutdown()
        server.server_close()
        control_file.unlink(missing_ok=True)
        fcntl.flock(lock, fcntl.LOCK_UN)
        lock.close()


def main():
    parser = argparse.ArgumentParser(description="Monica private desktop runtime")
    parser.add_argument("command", choices=["serve"])
    parser.add_argument("--bundle-root", required=True, type=Path)
    parser.add_argument("--data-root", type=Path, default=Path.home() / "Library/Application Support/Monica")
    parser.add_argument("--logs-root", type=Path)
    parser.add_argument("--parent-pid", type=int)
    parser.add_argument("--no-start", action="store_true")
    serve(parser.parse_args())


if __name__ == "__main__":
    main()
