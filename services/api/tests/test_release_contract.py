"""Publication and release contracts exercised without production SSH/Docker."""
import importlib.util
import io
import json
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

INFRA = Path(__file__).resolve().parents[3]/'infra'


def module(filename):
    spec = importlib.util.spec_from_file_location(filename.replace('-', '_'), INFRA/filename)
    loaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded)
    return loaded


def archive(files, manifest=True):
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode='w') as tar:
        for name, data in files.items():
            entry = tarfile.TarInfo(name); entry.size = len(data)
            tar.addfile(entry, io.BytesIO(data))
        if manifest:
            data = json.dumps({name: len(data) for name, data in files.items()}).encode()
            entry = tarfile.TarInfo('.publish-manifest.json'); entry.size = len(data)
            tar.addfile(entry, io.BytesIO(data))
    return output.getvalue()


def test_snapshot_failure_preserves_pointer_and_old_bytes(tmp_path):
    publisher = module('snapshot_publish.py')
    def install(files, manifest=True):
        return subprocess.run([sys.executable, '-c', publisher.INSTALL, str(tmp_path)],
                              input=archive(files, manifest), capture_output=True, timeout=10, check=False)
    assert install({'profile.json': b'old', 'media/a.jpg': b'asset'}).returncode == 0
    old = (tmp_path/'CURRENT').resolve()
    assert install({'profile.json': b'broken'}, manifest=False).returncode != 0
    assert (tmp_path/'CURRENT').resolve() == old
    assert install({'profile.json': b'new'}).returncode == 0
    assert (old/'profile.json').read_bytes() == b'old'
    assert (tmp_path/'CURRENT/profile.json').read_bytes() == b'new'
    assert (tmp_path/'CURRENT/media/a.jpg').read_bytes() == b'asset'
    current = (tmp_path/'CURRENT').resolve()
    assert install({'../escape': b'bad'}).returncode != 0
    assert (tmp_path/'CURRENT').resolve() == current


def test_manifest_requires_complete_fixed_digests_and_matching_compose(tmp_path):
    import hashlib
    deploy = module('deploy-release.py')
    (tmp_path/'docker-compose.yml').write_text('services: {}')
    value = {'version': 1, 'commit': 'a'*40, 'ci_run': '123',
             'compose_sha256': hashlib.sha256((tmp_path/'docker-compose.yml').read_bytes()).hexdigest(),
             'images': {role: f'ghcr.io/hmumixam/monica-next-{role}@sha256:'+'b'*64 for role in ('api','worker','web')}}
    assert deploy.validate_manifest(value, tmp_path) == value
    with pytest.raises(ValueError):
        deploy.validate_manifest({**value, 'images': {**value['images'], 'worker': 'worker:latest'}}, tmp_path)
    with pytest.raises(ValueError):
        deploy.validate_manifest({**value, 'compose_sha256': 'wrong'}, tmp_path)


def test_background_rollback_forces_restart_of_draining_old_container(tmp_path):
    deploy = module('deploy-release.py')
    release = deploy.Deployment(tmp_path, {'commit': 'a'*40, 'compose_sha256': 'b'*64})
    release.background_changed = ['worker', 'worker-ai']
    release.background_before = {'worker': {'Image': 'sha256:old'}, 'worker-ai': None}
    def absent(_name):
        raise RuntimeError('No replacement was started')
    release.service = absent
    calls = []
    release.run = lambda args: calls.append(args)
    release.restore_background()
    assert any('--force-recreate' in args and args[-1] == 'worker' for args in calls)
    assert any('stop' in args and args[-1] == 'worker-ai' for args in calls)


def test_interrupted_release_restores_runtime_state_and_background(tmp_path):
    deploy = module('deploy-release.py')
    release = deploy.Deployment(tmp_path, {'commit': 'a'*40, 'compose_sha256': 'b'*64})
    config = tmp_path/'Caddyfile'; config.write_text('new routes')
    old = {'handler': 'reverse_proxy', 'upstreams': [{'dial': 'old-api:8000'}, {'dial': 'old-web:3000'}]}
    release.save_journal({'release': 'a'*40, 'proxy': 'proxy', 'config_path': str(config), 'config': 'old routes',
                          'runtime': old, 'state': {'release': 'old'}, 'background': {}, 'changed': []})
    release.run = lambda _args: ''
    release.runtime_config = lambda _proxy: old
    release.recover()
    assert config.read_text() == 'old routes'
    assert json.loads(release.state_path.read_text()) == {'release': 'old'}
    assert not release.journal_path.exists()


def test_private_registry_guard_refuses_public_and_unverifiable_targets(monkeypatch):
    import urllib.error
    guard = module('check-private-package.py')
    monkeypatch.setenv('GITHUB_REPOSITORY_OWNER', 'test-owner')
    monkeypatch.setenv('GH_TOKEN', 'test-token')
    monkeypatch.setattr(guard.urllib.request, 'urlopen', lambda *_args, **_kwargs: io.BytesIO(b'{"visibility":"private"}'))
    guard.check('api')
    monkeypatch.setattr(guard.urllib.request, 'urlopen', lambda *_args, **_kwargs: io.BytesIO(b'{"visibility":"public"}'))
    with pytest.raises(RuntimeError, match='private'):
        guard.check('api')
    def missing(*_args, **_kwargs):
        raise urllib.error.HTTPError('https://example', 404, 'missing', None, None)
    monkeypatch.setattr(guard.urllib.request, 'urlopen', missing)
    guard.check('api', allow_missing=True)
    with pytest.raises(urllib.error.HTTPError):
        guard.check('api')


def test_background_start_failure_has_durable_rollback_record(tmp_path, monkeypatch):
    deploy = module('deploy-release.py')
    release = deploy.Deployment(tmp_path, {'commit': 'a'*40, 'compose_sha256': 'b'*64})
    monkeypatch.setattr(deploy.staged, 'BACKGROUND_SERVICES', ('worker',))
    old = {'Name': '/old-worker', 'Image': 'sha256:old', 'State': {'Running': False}}
    new = {'Name': '/new-worker', 'Image': 'sha256:new', 'State': {'Running': False}}
    release.journal = {'background': {}, 'changed': []}
    release.service = lambda _name: 'worker'
    started = []
    release.inspect = lambda name: ({'Id': 'sha256:new'} if name.startswith('monica-next-') else new if started else old)
    def run(args):
        if 'up' in args:
            saved = json.loads(release.journal_path.read_text())
            assert saved['background']['worker'] == old and saved['changed'] == ['worker']
            started.append(args)
        return ''
    release.run = run
    with pytest.raises(RuntimeError, match='Background image/start mismatch: worker'):
        release.background()
    assert release.journal_path.exists()
    restored = []
    def rollback(args):
        if '-f' in args:
            config = json.loads((tmp_path/'.background-rollback.json').read_text())
            assert config['services']['worker']['image'] == 'sha256:old'
        restored.append(args)
    release.run = rollback
    release.restore_background()
    assert any('--force-recreate' in args for args in restored)
    assert any('stop' in args for args in restored)  # Retain intentionally stopped state.


def test_background_drain_timeout_does_not_start_replacement(tmp_path, monkeypatch):
    deploy = module('deploy-release.py')
    release = deploy.Deployment(tmp_path, {'commit': 'a'*40, 'compose_sha256': 'b'*64})
    monkeypatch.setattr(deploy.staged, 'BACKGROUND_SERVICES', ('worker',))
    old = {'Name': '/busy-worker', 'Image': 'sha256:old', 'State': {'Running': True}}
    release.journal = {'background': {}, 'changed': []}
    release.service = lambda _name: 'worker'
    release.inspect = lambda name: {'Id': 'sha256:new'} if name.startswith('monica-next-') else old
    ticks = iter([0, 961])
    monkeypatch.setattr(deploy.time, 'monotonic', lambda: next(ticks))
    calls = []
    release.run = calls.append
    with pytest.raises(RuntimeError, match='busy-worker'):
        release.background()
    assert any('TERM' in args for args in calls)
    assert not any('up' in args for args in calls)
    assert json.loads(release.journal_path.read_text())['background']['worker'] == old


def test_legacy_bridge_journals_original_worker_before_stopping(tmp_path, monkeypatch):
    deploy = module('deploy-release.py')
    release = deploy.Deployment(tmp_path, {'commit': 'a'*40, 'compose_sha256': 'b'*64})
    old = {'Name': '/legacy-worker', 'Image': 'sha256:old', 'State': {'Running': True}}
    release.service = lambda name: name
    release.inspect = lambda _name: old
    release.run = lambda _args: '0'
    release.journal = {'background': {}, 'changed': []}
    def drain(_release, api, worker):
        saved = json.loads(release.journal_path.read_text())
        assert saved['background']['worker'] == old
        assert saved['changed'] == ['worker']
        assert (api, worker) == ('api', 'worker')
    monkeypatch.setattr(deploy.legacy, 'drain', drain)
    release.prepare_legacy_worker()
    assert release.background_before['worker'] == old
    assert release.background_changed == ['worker']


def test_manual_api_work_blocks_migration_and_switch(tmp_path):
    deploy = module('deploy-release.py')
    release = deploy.Deployment(tmp_path, {'commit': 'a'*40, 'compose_sha256': 'b'*64})
    release.service = lambda _name: 'live-api'
    release.manual_tasks = lambda _name: [{'pid': 12, 'script': 'confirm_posters.py'}]
    with pytest.raises(RuntimeError, match='Manual API tasks still active'):
        release.assert_no_manual_tasks()
    assert not release.journal_path.exists()


def test_rollback_drains_all_new_lanes_before_restoring_legacy_worker(tmp_path):
    deploy = module('deploy-release.py')
    release = deploy.Deployment(tmp_path, {'commit': 'a'*40, 'compose_sha256': 'b'*64})
    release.background_changed = ['worker', 'worker-ai', 'scheduler']
    release.background_before = {'worker': {'Name': '/worker', 'Image': 'sha256:old', 'State': {'Running': True}},
                                 'worker-ai': None, 'scheduler': None}
    release.service = lambda name: name
    release.inspect = lambda name: {'Name': '/'+name.lstrip('/'), 'Image': 'sha256:new', 'State': {'Running': True}}
    calls = []
    release.run = lambda args: calls.append(args)
    release.restore_background()
    first_up = next(i for i, args in enumerate(calls) if 'up' in args)
    drained = [args[-1] for args in calls[:first_up] if '--time' in args]
    assert drained == ['/scheduler', '/worker-ai', '/worker']
    assert calls[first_up][-1] == 'worker'


def test_failed_drain_retains_original_without_blocking_other_role_recovery(tmp_path):
    deploy = module('deploy-release.py')
    release = deploy.Deployment(tmp_path, {'commit': 'a'*40, 'compose_sha256': 'b'*64})
    release.background_changed = ['worker', 'image-ocr']
    release.background_before = {name: {'Name': '/'+name, 'Image': 'sha256:old', 'State': {'Running': True}}
                                 for name in release.background_changed}
    release.service = lambda name: name
    release.inspect = lambda name: {'Name': name, 'Image': 'sha256:old',
                                    'State': {'Running': name.lstrip('/') == 'image-ocr'}}
    calls = []
    release.run = lambda args: calls.append(args)
    with pytest.raises(RuntimeError, match='Other roles recovered'):
        release.restore_background()
    assert any('up' in args and args[-1] == 'worker' for args in calls)
    assert not any(args[-1].lstrip('/') == 'image-ocr' for args in calls)
    assert json.loads(release.journal_path.read_text())['changed'] == ['image-ocr']


@pytest.mark.parametrize('name,target', [('linkedin-sync', 'app.sync_linkedin'),
    ('archive-sync', 'app.sync_archives'), ('instagram-story-sync', 'app.sync_instagram_stories')])
def test_legacy_source_interrupts_only_its_python_module(tmp_path, name, target):
    deploy = module('deploy-release.py')
    release = deploy.Deployment(tmp_path, {'commit': 'a'*40, 'compose_sha256': 'b'*64})
    calls = []
    def run(args):
        calls.append(args)
        return 'legacy' if len(calls) == 1 else ''
    release.run = run
    release.stop_background(name, 'container')
    assert len(calls) == 3
    assert calls[1][-2:] == ['--restart=no', 'container']
    assert calls[2][-1] == target
    assert 'signal.SIGINT' in calls[2][-2]
    assert all('kill' not in args for args in calls)


def test_native_source_uses_graceful_term(tmp_path):
    deploy = module('deploy-release.py')
    release = deploy.Deployment(tmp_path, {'commit': 'a'*40, 'compose_sha256': 'b'*64})
    calls = []
    def run(args):
        calls.append(args)
        return 'native'
    release.run = run
    release.stop_background('linkedin-sync', 'container')
    assert calls[-1][-3:] == ['--signal', 'TERM', 'container']


def test_failed_legacy_drain_restores_restart_policy_on_retained_process(tmp_path):
    deploy = module('deploy-release.py')
    release = deploy.Deployment(tmp_path, {'commit': 'a'*40, 'compose_sha256': 'b'*64})
    old = {'Name': '/source', 'Image': 'sha256:old', 'State': {'Running': True},
           'HostConfig': {'RestartPolicy': {'Name': 'unless-stopped', 'MaximumRetryCount': 0}}}
    release.background_changed = ['linkedin-sync']
    release.background_before = {'linkedin-sync': old}
    release.inspect = lambda _: {**old, 'HostConfig': {'RestartPolicy': {'Name': 'no', 'MaximumRetryCount': 0}}}
    calls = []
    release.run = lambda args: calls.append(args)
    with pytest.raises(RuntimeError, match='journal retained'):
        release.restore_background()
    assert calls == [[release.docker, 'update', '--restart=unless-stopped', '/source']]


@pytest.mark.parametrize('local', ['exact', 'missing', 'different_digest', 'unrecorded_digest'])
def test_registry_pull_reuses_only_the_exact_installed_digest(tmp_path, local):
    deploy = module('deploy-release.py')
    images = {role: f'ghcr.io/hmumixam/monica-next-{role}@sha256:'+('b'*64)
              for role in ('api', 'worker', 'web')}
    release = deploy.Deployment(tmp_path, {'commit': 'a'*40, 'compose_sha256': 'b'*64, 'images': images})
    def inspect(image):
        if image == images['api'] and local == 'missing':
            raise subprocess.CalledProcessError(1, ['docker', 'inspect', image])
        if image == images['api'] and local == 'unrecorded_digest':
            return {'RepoDigests': None}
        return {'RepoDigests': [image if local == 'exact' or image != images['api'] else 'another@sha256:'+'c'*64]}
    release.inspect = inspect
    calls = []
    release.run = lambda args: calls.append(args)
    release.pull()
    pulls = [args[-1] for args in calls if args[1] == 'pull']
    assert pulls == ([] if local == 'exact' else [images['api']])
    assert [args[-1] for args in calls if args[1] == 'tag'] == [
        'monica-next-'+role+':'+'a'*40 for role in images]


@pytest.mark.parametrize('acknowledged', [True, False])
def test_legacy_handoff_requires_database_ack_not_docker_client_exit(monkeypatch, acknowledged):
    from types import SimpleNamespace

    bridge = module('legacy-worker-drain.py')
    class Client:
        def __init__(self):
            self.stdin = io.StringIO()
            self.stdout = io.StringIO('DRAINED\n'+('RELEASED\n' if acknowledged else ''))
            self.terminated = False
        def poll(self):
            return None if acknowledged and not self.terminated else 0
        def terminate(self):
            self.terminated = True
        def wait(self, timeout):
            assert self.terminated  # A successful DB handoff must not wait for Docker's exit RPC.
            return 0
    client = Client()
    monkeypatch.setattr(bridge.subprocess, 'Popen', lambda *_args, **_kwargs: client)
    monkeypatch.setattr(bridge.select, 'select', lambda *_args: ([client.stdout], [], []))
    stopped = []
    release = SimpleNamespace(docker='docker', inspect=lambda _: {'State': {'Running': not stopped}},
                              run=lambda args: stopped.append(args))
    if acknowledged:
        bridge.drain(release, 'api', 'worker')
        assert client.terminated  # Only the lingering exec client is cleaned up.
    else:
        with pytest.raises(RuntimeError, match='did not acknowledge database release'):
            bridge.drain(release, 'api', 'worker')
    assert stopped == [['docker', 'stop', '--time', '30', 'worker']]


def test_legacy_guard_releases_real_database_gate_before_acknowledgement(crm_db):
    import os
    import select

    from sqlalchemy import create_engine, text
    from sqlalchemy.exc import OperationalError

    bridge = module('legacy-worker-drain.py')
    schema = crm_db.execute(text('SELECT current_schema()')).scalar_one()
    crm_db.commit()
    url = crm_db.bind.engine.url.update_query_dict({'options': f'-c search_path={schema},public'})
    env = {**os.environ, 'DATABASE_URL': url.render_as_string(hide_password=False)}
    engine = create_engine(url)
    process = subprocess.Popen([sys.executable, '-u', '-c', bridge.GUARD], env=env,
                               cwd=INFRA.parent/'services/api', stdin=subprocess.PIPE,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        assert select.select([process.stdout], [], [], 5)[0]
        assert process.stdout.readline().strip() == 'DRAINED'
        with engine.connect() as db:
            db.execute(text("SET LOCAL lock_timeout='150ms'"))
            with pytest.raises(OperationalError):
                db.execute(text("INSERT INTO jobs(job_type,payload) VALUES('metrics.refresh','{}')"))
            db.rollback()
            process.stdin.write('STOPPED\n'); process.stdin.flush()
            assert select.select([process.stdout], [], [], 5)[0]
            assert process.stdout.readline().strip() == 'RELEASED'
            db.execute(text("SET LOCAL lock_timeout='150ms'"))
            db.execute(text("INSERT INTO jobs(job_type,payload) VALUES('metrics.refresh','{}')"))
            db.commit()
        assert process.wait(timeout=5) == 0
    finally:
        process.stdin.close()
        if process.poll() is None:
            process.terminate(); process.wait(timeout=5)
        engine.dispose()


def test_rollback_starts_untouched_original_after_draining_replacements(tmp_path):
    deploy = module('deploy-release.py')
    release = deploy.Deployment(tmp_path, {'commit': 'a'*40, 'compose_sha256': 'b'*64})
    old = {'Id': 'original', 'Name': '/worker', 'Image': 'sha256:old', 'State': {'Running': True}}
    release.background_changed = ['worker', 'worker-ai']
    release.background_before = {'worker': old, 'worker-ai': None}
    release.service = lambda name: name
    calls = []
    def inspect(name):
        if name.lstrip('/') == 'worker':
            return {**old, 'State': {'Running': any('start' in args for args in calls)}}
        return {'Name': '/worker-ai', 'Image': 'sha256:new', 'State': {'Running': True}}
    release.inspect = inspect
    release.run = lambda args: calls.append(args)
    release.restore_background()
    stop = next(i for i, args in enumerate(calls) if args[1] == 'stop' and args[-1] == '/worker-ai')
    start = next(i for i, args in enumerate(calls) if args[1] == 'start')
    assert stop < start
    assert calls[start][-1] == '/worker'
    assert not any('--force-recreate' in args for args in calls)


def test_recovery_retry_recognizes_an_already_restarted_original(tmp_path):
    deploy = module('deploy-release.py')
    release = deploy.Deployment(tmp_path, {'commit': 'a'*40, 'compose_sha256': 'b'*64})
    old = {'Id': 'original', 'Name': '/worker', 'Image': 'sha256:old',
           'State': {'Running': True, 'StartedAt': 'before-drain'}}
    release.background_changed = ['worker']
    release.background_before = {'worker': old}
    release.inspect = lambda _: {**old, 'State': {'Running': True, 'StartedAt': 'recovered-process'}}
    calls = []
    release.run = lambda args: calls.append(args)
    release.restore_background()
    assert calls == []
    assert not release.journal_path.exists()


def test_background_signals_all_before_waiting_and_starts_one_bounded_batch(tmp_path, monkeypatch):
    deploy = module('deploy-release.py')
    names = ('worker', 'worker-ai')
    monkeypatch.setattr(deploy.staged, 'BACKGROUND_SERVICES', names)
    release = deploy.Deployment(tmp_path, {'commit': 'a'*40, 'compose_sha256': 'b'*64})
    release.journal = {'background': {}, 'changed': []}
    release.service = lambda name: name
    calls, stopped = [], []
    started = False
    def inspect(name):
        if name.startswith('monica-next-'):
            return {'Id': 'new'}
        return {'Name': name, 'Image': 'new' if started else 'old', 'State': {'Running': True}}
    def stop(name, container):
        assert json.loads(release.journal_path.read_text())['changed'] == list(names)
        stopped.append(name)
    def inspect_many(waiting):
        assert stopped == list(names)
        return [{'Name': name, 'State': {'Running': False}} for name in waiting]
    def run(args):
        nonlocal started
        assert stopped == list(names)
        assert release.env['COMPOSE_PARALLEL_LIMIT'] == '2'
        calls.append(args)
        started = True
    release.inspect = inspect
    release.stop_background = stop
    release.inspect_many = inspect_many
    release.run = run
    release.background()
    assert len(calls) == 1 and calls[0][-2:] == list(names)


def test_http_only_change_preserves_verified_background_image(tmp_path, monkeypatch):
    deploy = module('deploy-release.py')
    manifest = {'commit': 'a'*40, 'compose_sha256': 'b'*64, 'background_inputs': {'api': 'c'*64}}
    (tmp_path/'.release-state.json').write_text(json.dumps({'manifest': manifest, 'background': {'image-ocr': {'image': 'old'}}}))
    release = deploy.Deployment(tmp_path, manifest)
    monkeypatch.setattr(deploy.staged, 'BACKGROUND_SERVICES', ('image-ocr',))
    release.service = lambda _: 'image-ocr'
    release.inspect = lambda name: {'Id': 'new'} if name.startswith('monica-next-') else {'Image': 'old', 'State': {'Running': True}}
    release.run = lambda _: pytest.fail('Unchanged background was restarted')
    release.background()
    assert release.background_versions['image-ocr']['image'] == 'old'


def test_http_only_modules_are_never_imported_by_background_code():
    import ast
    common = module('release_common.py')
    root = Path(__file__).parents[3]
    excluded = {Path(p).stem for p in common.HTTP_ONLY_PATHS}
    for path in (root/'services').rglob('*.py'):
        if '.venv' in path.parts or 'tests' in path.parts or str(path.relative_to(root)) in common.HTTP_ONLY_PATHS:
            continue
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Import):
                assert not any(a.name in {'app.'+name for name in excluded} for a in node.names), path
            elif isinstance(node, ast.ImportFrom):
                assert node.module not in excluded | {'app.'+name for name in excluded}, path
                if node.module in ('app', None):
                    assert not any(a.name in excluded for a in node.names), path


def test_inventory_batches_compose_discovery_and_excludes_candidates(tmp_path):
    deploy = module('deploy-release.py')
    value = deploy.Deployment(tmp_path, {'commit': 'a'*40, 'compose_sha256': 'b'*64})
    items = [
        {'Id': 'old', 'Name': '/worker', 'Config': {'Labels': {'com.docker.compose.service': 'worker'}}},
        {'Id': 'api', 'Name': '/api', 'Config': {'Labels': {'com.docker.compose.service': 'api'}}},
        {'Id': 'candidate', 'Name': '/candidate', 'Config': {'Labels': {'com.docker.compose.service': 'api', 'com.docker.compose.oneoff': 'True'}}},
    ]
    calls = []
    def run(args):
        calls.append(args)
        return 'old\napi\ncandidate' if args[0] == value.compose else json.dumps(items)
    value.run = run
    value.refresh_inventory()
    assert set(value.service_inventory) == {'worker', 'api'}
    assert value.service('api') == 'api'
    assert len(calls) == 2
    items[2]['Config']['Labels']['com.docker.compose.oneoff'] = 'false'
    with pytest.raises(RuntimeError, match='Multiple containers'):
        value.refresh_inventory()
