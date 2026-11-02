from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path

from linkedin_cli.conf import WORKER_IDLE_TIMEOUT_S
from linkedin_cli.session import WorkerLinkedInSession, linkedin_cli_home, session_lock
from linkedin_cli.session import _locks_dir


CONNECT_TIMEOUT_S = 60


def _worker_dir() -> Path:
    path = linkedin_cli_home() / "workers"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _safe_name(name: str) -> str:
    return "".join(char if char.isalnum() or char in {"-", "_", "."} else "_" for char in name)


def socket_path(name: str) -> Path:
    return _worker_dir() / f"{_safe_name(name)}.sock"


def _log_path(name: str) -> Path:
    path = linkedin_cli_home() / "logs"
    path.mkdir(parents=True, exist_ok=True)
    return path / f"worker-{_safe_name(name)}.log"


@contextmanager
def _startup_lock(name: str):
    import fcntl

    path = _locks_dir() / f"{name}.worker.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def _send_request(path: Path, payload: dict, *, timeout: float | None = None) -> dict:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        if timeout is not None:
            client.settimeout(timeout)
        client.connect(str(path))
        client.sendall(json.dumps(payload).encode("utf-8") + b"\n")
        chunks = []
        while True:
            chunk = client.recv(65536)
            if not chunk:
                break
            chunks.append(chunk)
    if not chunks:
        raise RuntimeError("linkedin-cli worker closed the connection without a response")
    return json.loads(b"".join(chunks).decode("utf-8"))


def _try_request(name: str, payload: dict) -> dict | None:
    path = socket_path(name)
    if not path.exists():
        return None
    try:
        return _send_request(path, payload)
    except (ConnectionRefusedError, FileNotFoundError, OSError):
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        return None


def _recent_log(name: str, *, max_lines: int = 40) -> str:
    path = _log_path(name)
    try:
        lines = path.read_text(errors="replace").splitlines()
    except FileNotFoundError:
        return ""
    return "\n".join(lines[-max_lines:])


def _startup_error(name: str, reason: str) -> RuntimeError:
    message = f"linkedin-cli worker for session {name!r} {reason}"
    recent_log = _recent_log(name)
    if recent_log:
        message = f"{message}\nRecent worker log ({_log_path(name)}):\n{recent_log}"
    return RuntimeError(message)


def _start_worker(name: str) -> subprocess.Popen:
    log = _log_path(name).open("ab", buffering=0)
    env = os.environ.copy()
    env["LINKEDIN_CLI_WORKER"] = "1"
    return subprocess.Popen(
        [sys.executable, "-m", "linkedin_cli.worker", name],
        stdin=subprocess.DEVNULL,
        stdout=log,
        stderr=log,
        env=env,
        close_fds=True,
        start_new_session=True,
    )


def _terminate_worker_startup(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()


def _wait_for_worker(name: str, process: subprocess.Popen | None = None) -> None:
    deadline = time.monotonic() + CONNECT_TIMEOUT_S
    while time.monotonic() < deadline:
        if process is not None and process.poll() is not None:
            raise _startup_error(name, f"exited before startup completed with code {process.returncode}")
        try:
            response = _send_request(socket_path(name), {"ping": True}, timeout=1)
            if response.get("returncode") == 0:
                return
        except (ConnectionRefusedError, FileNotFoundError, OSError):
            time.sleep(0.2)
    raise _startup_error(name, f"did not start within {CONNECT_TIMEOUT_S} seconds")


def run_via_worker(name: str, argv: list[str]) -> int:
    payload = {"argv": argv}
    response = _try_request(name, payload)
    if response is None:
        with _startup_lock(name):
            response = _try_request(name, payload)
            if response is None:
                process = _start_worker(name)
                try:
                    _wait_for_worker(name, process)
                except Exception:
                    _terminate_worker_startup(process)
                    raise
                response = _send_request(socket_path(name), payload)

    stdout = response.get("stdout") or ""
    stderr = response.get("stderr") or ""
    if stdout:
        sys.stdout.write(stdout)
        sys.stdout.flush()
    if stderr:
        sys.stderr.write(stderr)
        sys.stderr.flush()
    return int(response.get("returncode") or 0)


def stop_worker(name: str) -> None:
    response = _try_request(name, {"shutdown": True})
    if response is None:
        return
    path = socket_path(name)
    deadline = time.monotonic() + 10
    while path.exists() and time.monotonic() < deadline:
        time.sleep(0.1)


def _execute_request(session: WorkerLinkedInSession, argv: list[str]) -> dict:
    import contextlib
    import io

    from linkedin_cli.cli import _execute_verb, _parse_args
    from playwright._impl._errors import TargetClosedError

    stdout = io.StringIO()
    stderr = io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        try:
            args = _parse_args(argv)
            session.ensure_browser()
            try:
                returncode = _execute_verb(args, session)
            except TargetClosedError:
                session.close()
                session.ensure_browser()
                returncode = _execute_verb(args, session)
        except SystemExit as exc:
            returncode = int(exc.code or 0)
        except Exception as exc:  # noqa: BLE001
            returncode = 1
            print(f"error: {type(exc).__name__}: {exc}", file=sys.stderr)
    return {"returncode": returncode, "stdout": stdout.getvalue(), "stderr": stderr.getvalue()}


def serve(name: str) -> int:
    path = socket_path(name)
    session = WorkerLinkedInSession(
        name,
        min_pace=5.0,
        max_pace=8.0,
        username=os.environ.get("LINKEDIN_USERNAME"),
        password=os.environ.get("LINKEDIN_PASSWORD"),
    )
    with session_lock(name), session:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(str(path))
        server.listen(64)
        server.settimeout(1)
        idle_deadline = time.monotonic() + WORKER_IDLE_TIMEOUT_S
        shutdown = False
        try:
            while not shutdown and time.monotonic() < idle_deadline:
                try:
                    conn, _ = server.accept()
                except socket.timeout:
                    continue
                idle_deadline = time.monotonic() + WORKER_IDLE_TIMEOUT_S
                with conn:
                    raw = b""
                    while not raw.endswith(b"\n"):
                        chunk = conn.recv(65536)
                        if not chunk:
                            break
                        raw += chunk
                    try:
                        request = json.loads(raw.decode("utf-8")) if raw else {}
                        if request.get("shutdown"):
                            shutdown = True
                            response = {"returncode": 0, "stdout": "", "stderr": ""}
                        elif request.get("ping"):
                            response = {"returncode": 0, "stdout": "", "stderr": ""}
                        else:
                            response = _execute_request(session, list(request.get("argv") or []))
                    except Exception as exc:  # noqa: BLE001
                        response = {"returncode": 1, "stdout": "", "stderr": f"error: worker: {exc}\n"}
                    try:
                        conn.sendall(json.dumps(response).encode("utf-8"))
                    except BrokenPipeError:
                        pass
        finally:
            server.close()
            try:
                path.unlink()
            except FileNotFoundError:
                pass
    return 0


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    if len(args) != 1:
        print("usage: python -m linkedin_cli.worker <session>", file=sys.stderr)
        return 2
    return serve(args[0])


if __name__ == "__main__":
    raise SystemExit(main())
