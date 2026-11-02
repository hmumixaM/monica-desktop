"""Owned subprocesses, finite crash backoff, and cooperative shutdown."""

import os
import signal
import subprocess
import time
import json
from dataclasses import dataclass, field
from pathlib import Path

from .config import write_private


def identity(pid):
    result = subprocess.run(["/bin/ps", "-p", str(pid), "-o", "lstart=", "-o", "command="],
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    return result.stdout.strip() if result.returncode == 0 else ""


def recover_owned(path):
    """After an unclean supervisor exit, stop only recorded, identical owners."""
    if not path.exists():
        return
    records = json.loads(path.read_text())
    owned = []
    for record in records:
        pid = record.get("pid")
        if isinstance(pid, int) and pid > 1 and identity(pid) == record.get("identity") and record.get("identity"):
            owned.append(pid)
            try:
                # PostgreSQL SIGINT is a clean fast shutdown; other owners drain.
                os.kill(pid, signal.SIGINT if record.get("name") == "postgres" else signal.SIGTERM)
            except ProcessLookupError:
                pass
    deadline = time.monotonic() + 30
    while owned and time.monotonic() < deadline:
        owned = [pid for pid in owned if identity(pid)]
        time.sleep(0.2)
    for pid in owned:
        record = next(item for item in records if item["pid"] == pid)
        if identity(pid) == record["identity"]:
            try:
                os.killpg(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
    path.unlink(missing_ok=True)


def save_owned(path, children):
    records = []
    for child in children:
        if child.process and child.process.poll() is None:
            value = identity(child.process.pid)
            if value:
                records.append({"name": child.name, "pid": child.process.pid, "identity": value})
    write_private(path, records)


@dataclass
class Child:
    name: str
    command: list[str]
    env: dict
    cwd: Path
    log_path: Path
    background: bool = False
    restart: bool = True
    process: subprocess.Popen | None = None
    state: str = "stopped"
    error: str = ""
    failures: list[float] = field(default_factory=list)
    retry_at: float = 0
    started_at: float = 0

    def start(self):
        if self.process and self.process.poll() is None:
            return
        self.log_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        # Bound logs before a new process starts; do not move a live descriptor.
        if self.log_path.exists() and self.log_path.stat().st_size > 10 * 1024 * 1024:
            self.log_path.replace(self.log_path.with_suffix(".previous.log"))
        with self.log_path.open("ab", buffering=0) as output:
            os.chmod(self.log_path, 0o600)
            self.process = subprocess.Popen(self.command, cwd=self.cwd, env=self.env,
                                            stdin=subprocess.DEVNULL, stdout=output, stderr=output,
                                            start_new_session=True)
        self.started_at = time.monotonic()
        self.state, self.error = "running", ""

    def poll(self):
        if not self.process or self.state in {"stopped", "stopping", "completed", "failed"}:
            return
        code = self.process.poll()
        if code is None:
            return
        if self.state != "backoff":
            now = time.monotonic()
            self.failures = [stamp for stamp in self.failures if now - stamp < 300]
            self.failures.append(now)
            self.error = f"进程退出（{code}），详细信息见 {self.log_path.name}"
            if not self.restart:
                self.state = "completed" if code == 0 else "failed"
                return
            if len(self.failures) >= 5:
                self.state = "failed"
                return
            self.retry_at = now + min(60, 2 ** len(self.failures))
            self.state = "backoff"
        if time.monotonic() >= self.retry_at:
            self.start()

    def signal_stop(self):
        # Shutdown may cancel a pending operation before Manager.stop drains
        # the same children. Re-entering a Python signal handler can deadlock
        # its Event/Condition lock, so send the cooperative signal once.
        if self.state == "stopping":
            return
        self.state = "stopping"
        if self.process and self.process.poll() is None:
            # Signal the owner only so workers can drain their own children.
            self.process.send_signal(signal.SIGTERM)

    def finish_stop(self, deadline):
        if self.process:
            try:
                self.process.wait(timeout=max(0.01, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(self.process.pid, signal.SIGTERM)
                    self.process.wait(timeout=5)
                except (ProcessLookupError, subprocess.TimeoutExpired):
                    try:
                        os.killpg(self.process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    self.process.wait(timeout=5)
            # Some browser launchers exit before their descendants. The group
            # belongs to this Child even after the leader has been reaped.
            try:
                os.killpg(self.process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        self.state = "stopped"

    def public(self):
        return {"name": self.name, "state": self.state, "error": self.error,
                "pid": self.process.pid if self.process and self.process.poll() is None else None}
