#!/usr/bin/env python3
"""Exercise an assembled runtime in a fresh private folder; no provider login."""
import argparse
import hashlib
import hmac
import http.cookiejar
import json
import os
import secrets
import signal
import socket
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import HTTPCookieProcessor, Request, build_opener, urlopen
from uuid import uuid4


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle-root", required=True, type=Path)
    parser.add_argument("--work-dir", required=True, type=Path)
    args = parser.parse_args()
    bundle, work = args.bundle_root.resolve(), args.work_dir.resolve()
    if work.exists():
        raise SystemExit("Use a new work directory; existing data is never overwritten")
    work.mkdir(parents=True, mode=0o700)
    data, logs = work / "data", work / "logs"
    env = {key: os.environ[key] for key in ("HOME", "USER", "TMPDIR", "LANG") if key in os.environ}
    env.update(PATH="/usr/bin:/bin:/usr/sbin:/sbin", PYTHONHOME=str(bundle / "python"),
               PYTHONPATH=str(bundle / "desktop"), PYTHONNOUSERSITE="1", PYTHONDONTWRITEBYTECODE="1")
    python = str(bundle / "python/bin/python3")
    reserved = socket.socket()
    try:
        reserved.bind(("127.0.0.1", 17394))
        reserved.listen()
    except OSError:
        reserved.close()  # Another local process already exercises port fallback.
    output = (work / "supervisor.log").open("wb")
    process = subprocess.Popen([python, "-m", "monica_desktop", "serve", "--bundle-root", str(bundle),
                                "--data-root", str(data), "--logs-root", str(logs)], env=env, stdout=output, stderr=output)
    checks = []
    control = None

    def call(command="status", payload=None):
        req = Request(f"http://127.0.0.1:{control['port']}/control/" + command,
                      headers={"Authorization": "Bearer " + control["token"], "Content-Type": "application/json"},
                      data=None if payload is None else json.dumps(payload).encode())
        return json.load(urlopen(req, timeout=10))

    def wait(predicate, timeout=180):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            value = call()
            if value["error"] and not value["operation"]:
                raise RuntimeError(value["error"])
            if not value["operation"] and predicate(value):
                return value
            if process.poll() is not None:
                raise RuntimeError("Supervisor exited")
            time.sleep(0.2)
        raise TimeoutError("Runtime check timed out")

    def action(name, payload=None):
        call(name, payload or {})
        return wait(lambda _: True)

    try:
        deadline = time.monotonic() + 15
        control_path = data / "run/control.json"
        while not control_path.exists() and time.monotonic() < deadline:
            time.sleep(0.1)
        control = json.loads(control_path.read_text())
        status = wait(lambda s: s["state"] == "running")
        web = status["web_url"]
        assert not web.endswith(":17394") and status["profiles"] == 0
        assert status["posts_imported"] == 0 and status["messages_imported"] == 0
        assert status["import_phases"] == []
        checks.append("fresh database initialization and occupied-port fallback")
        password, email = secrets.token_urlsafe(24), "desktop-smoke@example.test"
        action("create_account", {"email": email, "password": password})
        jar = http.cookiejar.CookieJar()
        browser = build_opener(HTTPCookieProcessor(jar))
        csrf = json.load(browser.open(web + "/api/v1/auth/csrf"))["csrf_token"]
        with browser.open(Request(web + "/api/v1/auth/login", data=json.dumps({"email": email, "password": password}).encode(),
                                  headers={"Content-Type": "application/json", "X-CSRF-Token": csrf, "Origin": web})) as response:
            assert response.status == 200
        assert any(cookie.name == "crm_session" and not cookie.secure for cookie in jar)
        assert json.load(browser.open(web + "/api/v1/me"))["email"] == email
        assert browser.open(web + "/login").status == 200
        for req in (Request(web + "/api/v1/me", headers={"Origin": "https://example.com"}),
                    Request(web + "/api/v1/auth/logout", data=b"{}", headers={"Content-Type": "application/json"})):
            try:
                browser.open(req)
                raise AssertionError("Expected boundary rejection")
            except HTTPError as error:
                assert error.code == 403
        checks.append("account bootstrap, HTTP session, authenticated API, Web, Origin and CSRF")
        # Exercise the actual bundled input dispatcher AND projection worker.
        # This is a synthetic local source batch; no provider request is made.
        secret = json.loads((data / 'settings.json').read_text())['secrets']['import']
        def import_fixture(key, biography):
            stamp, nonce = str(int(time.time())), uuid4().hex
            body = json.dumps({'source': 'instagram', 'stream': 'profiles', 'batch_id': key,
                'idempotency_key': key, 'observed_at': datetime.now(UTC).isoformat(), 'records': [{
                    'external_id': '900000001', 'content': {'display_name': 'CDC Fixture',
                        'username': 'cdc_fixture', 'fields': {'instagram.biography': biography}}}]}).encode()
            signature = hmac.new(secret.encode(), b'.'.join([stamp.encode(), nonce.encode(), body]), hashlib.sha256).hexdigest()
            with urlopen(Request(web + '/api/v1/imports/batches', data=body, headers={
                'Content-Type': 'application/json', 'X-Import-Timestamp': stamp,
                'X-Import-Nonce': nonce, 'X-Import-Signature': signature}), timeout=15) as response:
                accepted = json.load(response)
            assert accepted['status'] == 'queued'
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline:
                detail = json.load(browser.open(web + '/api/v1/imports/batches/' + accepted['batch_id']))
                if detail.get('status') == 'complete' or detail.get('batch', {}).get('status') == 'complete':
                    return
                time.sleep(.2)
            raise AssertionError('Bundled input projection did not complete')

        import_fixture('history-smoke-1', 'Local CDC verification')
        import_fixture('history-smoke-2', 'Local CDC verification updated')
        pipeline = json.load(browser.open(web + '/api/v1/pipeline/status'))
        assert len(pipeline['consumers']) == 4 and pipeline['projection']['count'] == 0
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            events = json.load(browser.open(web + '/api/v1/cdc/events'))
            for event in events:
                if event['provider'] != 'crm' or event['relation_name'] != 'profile_field_current':
                    continue
                detail = json.load(browser.open(web + '/api/v1/cdc/events/' + str(event['id'])))
                if any(field['before'] == 'Local CDC verification' and field['after'] == 'Local CDC verification updated'
                       for field in detail['changed_fields']):
                    break
            else:
                time.sleep(.5)
                continue
            break
        else:
            raise AssertionError('Asynchronous WAL history did not arrive')
        checks.append('independent durable input dispatch/projection and delayed native pgoutput history for four databases')
        visual = {'web': web, 'screenshots': str(work / 'screenshots'), 'cookies': [
            {'name': cookie.name, 'value': cookie.value, 'url': web,
             'httpOnly': cookie.name == 'crm_session', 'sameSite': 'Lax'} for cookie in jar]}
        browser_env = {**env, 'PLAYWRIGHT_BROWSERS_PATH': str(bundle / 'browsers')}
        subprocess.run([python, '-B', str(Path(__file__).with_name('verify_history_ui.py'))],
                       input=json.dumps(visual), text=True, env=browser_env, check=True, timeout=90)
        checks.append('bundled Chromium: authenticated Web history list, before/after dialog, source filter and narrow layout')
        paused = action("pause")
        assert paused["state"] == "paused"
        saved = action("save_config", {"config": {"timezone": "America/Chicago"}})
        assert saved["state"] == "paused"
        assert not any(s["pid"] and (s["name"].startswith("worker-") or s["name"].endswith("-sync")) for s in saved["services"])
        checks.append("pause and settings save do not restart background jobs")
        resumed = action("resume")
        assert resumed["state"] == "running"
        assert any(s["name"] == "worker-scheduler" and s["pid"] for s in resumed["services"])
        action("wake")
        checks.append("resume and sleep recovery with AI disabled")
        # Force a clean but unexpected database exit, then recover the entire tree.
        postgres = next(s["pid"] for s in resumed["services"] if s["name"] == "postgres")
        old_api = next(s["pid"] for s in resumed["services"] if s["name"] == "api")
        os.kill(postgres, signal.SIGINT)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if call()["state"] == "error":
                break
            time.sleep(0.2)
        else:
            raise AssertionError("Database loss was not detected")
        recovered = action("resume")
        assert recovered["state"] == "running" and recovered["account_created"]
        assert next(s["pid"] for s in recovered["services"] if s["name"] == "api") != old_api
        checks.append("database loss detection and complete dependency-tree recovery")
        owned = [s["pid"] for s in recovered["services"] if s["pid"]]
        call("shutdown", {})
        process.wait(timeout=60)
        assert process.returncode == 0 and not control_path.exists()
        for pid in owned:
            try:
                os.kill(pid, 0)
                raise AssertionError("Owned process survived shutdown")
            except ProcessLookupError:
                pass
        checks.append("graceful shutdown without surviving owned services")
        result = {"passed": checks, "provider_requests": False}
        (work / "results.json").write_text(json.dumps(result, indent=2))
        print(json.dumps(result, indent=2))
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=270)
        output.close()
        reserved.close()


if __name__ == "__main__":
    main()
