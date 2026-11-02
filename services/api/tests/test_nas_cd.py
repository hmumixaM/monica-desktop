"""Exercise the unattended boundary: trust, archive safety, locking and recovery."""
import importlib.util
import io
import json
import sys
import tarfile
from pathlib import Path

import pytest

INFRA = Path(__file__).resolve().parents[3] / 'infra'
sys.path.insert(0, str(INFRA))
from release_common import (
    AUDIT_PATHS,
    REPOSITORY,
    WORKFLOW,
    TransportError,
    digest,
    read_json,
    write_json,
)
from release_installer import unpack


def module(name):
    spec = importlib.util.spec_from_file_location(name.replace('-', '_'), INFRA / name)
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


def artifact(extra=None):
    files = {'infra/deploy-release.py': b'print(1)', 'infra/docker-compose.yml': b'services: {}',
             'migrations/001.sql': b'SELECT 1;'}
    files.update(extra or {})
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode='w') as archive:
        for name, content in files.items():
            entry = tarfile.TarInfo(name)
            entry.size = len(content)
            archive.addfile(entry, io.BytesIO(content))
    data = output.getvalue()
    manifest = {'version': 2, 'repository': REPOSITORY, 'workflow': WORKFLOW, 'commit': 'b' * 40,
                'ci_run': '123', 'images': {r: 'ghcr.io/hmumixam/monica-next-' + r + '@sha256:' + 'd' * 64 for r in ('api', 'worker', 'web')},
                'compose_sha256': digest(files['infra/docker-compose.yml']),
                'bundle': {'name': 'deployment.tar', 'sha256': digest(data)},
                'files': {p: digest(v) for p, v in files.items()},
                'migrations': {'001.sql': digest(files['migrations/001.sql'])},
                'audit_inputs': {p: 'a' * 64 for p in AUDIT_PATHS},
                'build_inputs': {r: 'a' * 64 for r in ('api', 'worker', 'web')}}
    return data, manifest


def test_archive_validates_all_files_before_installation(tmp_path):
    data, manifest = artifact()
    unpack(data, manifest, tmp_path)
    assert (tmp_path / 'migrations/001.sql').read_bytes() == b'SELECT 1;'
    assert read_json(tmp_path / 'release.json') == manifest


@pytest.mark.parametrize('bad', ['infra/../.env', 'infra/.env', 'infra/Caddyfile.live', '/etc/passwd'])
def test_protected_or_traversal_paths_are_rejected(tmp_path, bad):
    data, manifest = artifact({bad: b'bad'})
    with pytest.raises(ValueError):
        unpack(data, manifest, tmp_path)
    assert not list(tmp_path.iterdir())


def test_archive_hash_or_file_mismatch_is_rejected(tmp_path):
    data, manifest = artifact()
    with pytest.raises(ValueError, match='archive checksum'):
        unpack(data + b'bad', manifest, tmp_path)
    manifest['files']['infra/deploy-release.py'] = 'f' * 64
    with pytest.raises(ValueError, match='file checksum'):
        unpack(data, manifest, tmp_path)


class GitHub:
    def __init__(self, manifest, **run_changes):
        self.manifest = manifest
        self.run_changes = run_changes
        self.releases = []
        self.calls = []
        self.relations = {('a' * 40, 'b' * 40), ('b' * 40, 'c' * 40), ('a' * 40, 'c' * 40)}

    def ancestor(self, older, newer):
        return older == newer or (older, newer) in self.relations

    def request(self, path, data=None, binary=False):
        self.calls.append((path, data))
        if path == '/branches/main':
            return {'commit': {'sha': 'c' * 40}}
        if path.startswith('/actions/runs/'):
            return dict(head_sha=self.manifest['commit'], head_branch='main', event='push', path=WORKFLOW,
                        status='completed', conclusion='success', repository={'full_name': REPOSITORY}, **self.run_changes)
        if path == 'manifest':
            return json.dumps(self.manifest).encode()
        if path == '/deployments':
            return {'id': 99}
        return {}

    def pages(self, path):
        return iter(self.releases)


@pytest.mark.parametrize('field,value', [('event', 'pull_request'), ('conclusion', 'failure'),
    ('path', '.github/workflows/untrusted.yml'), ('head_branch', 'other'), ('head_sha', 'f' * 40)])
def test_only_successful_main_workflow_is_eligible(field, value):
    cd = module('nas-cd.py')
    _, manifest = artifact()
    github = GitHub(manifest)
    original = github.request
    def request(path, **kwargs):
        result = original(path, **kwargs)
        if path.startswith('/actions/runs/'):
            result[field] = value
        return result
    github.request = request
    with pytest.raises(ValueError, match='CI'):
        cd.eligible(github, manifest, 'c' * 40, 'a' * 40)


def test_release_cannot_roll_back_or_leave_main():
    cd = module('nas-cd.py')
    _, manifest = artifact()
    github = GitHub(manifest)
    with pytest.raises(ValueError, match='ancestor'):
        cd.eligible(github, manifest, 'c' * 40, 'c' * 40)
    github.relations.clear()
    with pytest.raises(ValueError, match='ancestor'):
        cd.eligible(github, manifest, 'c' * 40, None)


def test_discovery_ignores_v1_drafts_and_incomplete_releases():
    cd = module('nas-cd.py')
    _, manifest = artifact()
    github = GitHub(manifest)
    good = {'tag_name': 'verified-' + 'b' * 40, 'draft': False,
            'assets': [{'name': 'release.json', 'url': 'manifest'}, {'name': 'deployment.tar', 'url': 'archive'}]}
    github.releases = [dict(good, draft=True), dict(good, assets=[]), good]
    assert cd.discover(github, 'a' * 40)['manifest'] == manifest


def runner(tmp_path, monkeypatch):
    cd = module('nas-cd.py')
    token = tmp_path / 'token'
    token.write_text('not-a-real-token')
    token.chmod(0o600)
    root = tmp_path / 'infra'
    root.mkdir()
    home = tmp_path / 'cd'
    value = cd.Runner({'infra': str(root), 'state_dir': str(home), 'token_file': str(token)})
    _, manifest = artifact()
    value.github = GitHub(manifest)
    return cd, value


def test_published_bundle_waits_for_ci_success_without_pausing(tmp_path, monkeypatch):
    cd, value = runner(tmp_path, monkeypatch)
    manifest = value.github.manifest
    value.github.releases = [{'tag_name': 'verified-' + manifest['commit'], 'draft': False,
        'assets': [{'name': 'release.json', 'url': 'manifest'}, {'name': 'deployment.tar', 'url': 'archive'}]}]
    original = value.github.request
    completed = False
    def request(path, **kwargs):
        result = original(path, **kwargs)
        if path.startswith('/actions/runs/') and not completed:
            result.update(status='in_progress', conclusion=None)
        return result
    value.github.request = request
    waiting = value.run_once()
    assert waiting['phase'] == 'idle'
    assert not waiting['paused']
    assert not value.state.get('active')
    assert not value.state.get('failed_sha')
    completed = True
    assert cd.discover(value.github, None)['manifest'] == manifest


def test_existing_manual_journal_blocks_install(tmp_path, monkeypatch):
    _cd, value = runner(tmp_path, monkeypatch)
    (value.root / '.deploy-journal.json').write_text('{}')
    assert value.run_once() == {'waiting': 'existing_deployment_recovery'}
    assert not value.github.calls


def test_deployment_and_runner_locks_do_not_fail_or_mutate(tmp_path, monkeypatch):
    cd, value = runner(tmp_path, monkeypatch)
    for path, reason in [(value.home / 'runner.lock', 'runner_lock'), (value.root / '.deploy.lock', 'deployment_lock')]:
        with cd.locked(path) as acquired:
            assert acquired
            assert value.run_once() == {'waiting': reason}
    assert not value.state.get('paused')


def test_network_backoff_is_bounded_and_does_not_pause(tmp_path, monkeypatch):
    cd, value = runner(tmp_path, monkeypatch)
    monkeypatch.setattr(cd, 'discover', lambda *_: (_ for _ in ()).throw(TransportError('offline')))
    now = [1000]
    monkeypatch.setattr(cd.time, 'time', lambda: now[0])
    for delay in (120, 300, 900, 900):
        result = value.run_once()
        assert result['retry_at'] == now[0] + delay
        assert not result['paused']
        now[0] = result['retry_at']


def test_report_failure_is_retried_without_redeploy(tmp_path, monkeypatch):
    cd, value = runner(tmp_path, monkeypatch)
    value.state.update(target='b' * 40, ci_run='123', attempt='attempt', active=False)
    calls = []
    def failing(path, data=None):
        calls.append(path)
        raise TransportError('offline')
    value.github.request = failing
    value.queue_status('success', 'verified')
    assert value.state['outbox'] and not value.state.get('active')
    value.github = GitHub({})
    monkeypatch.setattr(cd, 'discover', lambda *_: None)
    value.run_once()
    assert not value.state['outbox']
    create = next(data for path, data in value.github.calls if path == '/deployments')
    assert create['auto_merge'] is False and create['ref'] == 'b' * 40


def test_pause_does_not_prevent_interrupted_attempt_recovery(tmp_path, monkeypatch):
    _cd, value = runner(tmp_path, monkeypatch)
    (value.home / 'paused').touch()
    value.state.update(active=True, target='b' * 40)
    value.save()
    seen = []
    monkeypatch.setattr(value, '_run', lambda pending: seen.append(pending))
    value.run_once()
    assert seen == [True]


def test_failure_pauses_following_releases_and_requires_explicit_retry(tmp_path, monkeypatch):
    _cd, value = runner(tmp_path, monkeypatch)
    value.state.update(active=True, target='b' * 40, ci_run='123', attempt='attempt')
    value.save()
    monkeypatch.setattr(value, '_run', lambda _: (_ for _ in ()).throw(RuntimeError('bad health')))
    value.run_once()
    assert value.state['paused'] and value.state['failed_sha'] == 'b' * 40
    assert not value.state['active']


def test_migration_skip_requires_image_hashes_and_every_ledger_file(tmp_path):
    deploy = module('deploy-release.py')
    _, manifest = artifact()
    value = deploy.Deployment(tmp_path, manifest)
    calls = []
    value.run = lambda args: calls.append(args)
    value.ledger = lambda: {'001.sql'}
    value.migrate()
    assert len(calls) == 1
    value.migrate()
    assert len(calls) == 1
    manifest['migrations']['001.sql'] = 'c' * 64
    value.migrate()
    assert len(calls) == 2
    value.ledger = lambda: set()
    with pytest.raises(RuntimeError, match='incomplete'):
        value.migrate()


def test_migration_receipt_reuses_same_application_with_new_registry_attestation(tmp_path):
    deploy = module('deploy-release.py')
    _, manifest = artifact()
    value = deploy.Deployment(tmp_path, manifest)
    calls = []
    value.run = lambda args: calls.append(args)
    value.ledger = lambda: {'001.sql'}
    value.migrate()
    old = manifest['images']['api']
    manifest['images']['api'] = old.replace('d' * 64, 'e' * 64)
    value.inspect = lambda image: {'Id': 'same-application'}
    value.migrate()
    assert len(calls) == 1
    manifest['images']['api'] = old.replace('d' * 64, 'f' * 64)
    value.inspect = lambda image: {'Id': image}
    value.migrate()
    assert len(calls) == 2
    manifest['images']['api'] = old.replace('d' * 64, 'a' * 64)
    def unavailable(image):
        raise deploy.subprocess.CalledProcessError(1, ['docker', 'inspect', image])
    value.inspect = unavailable
    value.migrate()
    assert len(calls) == 3


def test_status_keeps_phase_proof_without_expanding_command_timings(tmp_path, monkeypatch):
    _cd, value = runner(tmp_path, monkeypatch)
    timings = [{'command': 'docker', 'operation': 'inspect', 'seconds': 2.1}] * 500
    value.state.update(attempt='attempt', phase='success', phase_seconds={'observe': 300},
                       result={'timings': timings, 'observed_seconds': 300, 'runtime': {'jobs': {'expired_leases': 0}}})
    value.save()
    status = value.status()
    assert len(json.dumps(status)) < len(json.dumps(value.state)) / 10
    assert status['phase_seconds'] == {'observe': 300}
    assert status['result']['observed_seconds'] == 300
    assert status['result']['runtime']['jobs']['expired_leases'] == 0
    assert status['attempt_record'].endswith('/attempts/attempt.json')
    assert read_json(value.path)['result']['timings'] == timings
    assert value.state['result']['timings'] == timings


def test_background_same_image_keeps_all_roles_running(tmp_path, monkeypatch):
    deploy = module('deploy-release.py')
    _, manifest = artifact()
    write_json(tmp_path / '.release-state.json', {'manifest': manifest})
    value = deploy.Deployment(tmp_path, manifest)
    value.service = lambda name: name
    value.inspect = lambda name: {'Image': 'image', 'Id': 'image', 'State': {'Running': True}}
    value.run = lambda _: pytest.fail('unchanged service was recreated')
    value.background()
    assert value.background_changed == []


def test_safe_redirect_drops_github_auth_on_asset_cdn():
    from urllib.request import Request

    from release_common import SafeRedirect
    request = Request('https://api.github.com/repos/example/assets/1', headers={'Authorization': 'Bearer secret'})
    redirect = SafeRedirect().redirect_request(request, None, 302, '', {}, 'https://release-assets.githubusercontent.com/asset')
    assert not redirect.has_header('Authorization')


def test_missing_cache_builds_all_roles(monkeypatch):
    build = module('build-plan.py')
    monkeypatch.setattr(build, 'inputs', lambda _: {r: 'i' for r in ('api', 'worker', 'web')})
    monkeypatch.setattr(build, 'previous_manifest', lambda _: (_ for _ in ()).throw(ValueError('missing')))
    assert all(p['build'] for p in build.plan('a' * 40).values())


def test_matching_inputs_reuse_only_verified_fixed_digests(monkeypatch):
    build = module('build-plan.py')
    _, manifest = artifact()
    manifest['build_inputs'] = {r: 'i' for r in ('api', 'worker', 'web')}
    monkeypatch.setattr(build, 'inputs', lambda _: manifest['build_inputs'])
    monkeypatch.setattr(build, 'previous_manifest', lambda _: manifest)
    assert all(not p['build'] for p in build.plan('a' * 40).values())
    manifest['images']['api'] = 'api:latest'
    assert build.plan('a' * 40)['api']['build']
    assert all(p['build'] for p in build.plan('a' * 40, is_pr=True).values())


def test_completed_install_is_not_reexecuted_after_reporting_crash(tmp_path, monkeypatch):
    import release_installer as installer
    _, manifest = artifact()
    stage, root = tmp_path / 'stage', tmp_path / 'infra'
    stage.mkdir()
    root.mkdir()
    write_json(stage / 'release.json', manifest)
    write_json(root / '.release-state.json', {'release': manifest['commit'], 'manifest': manifest,
                                             'observed_seconds': 300})
    monkeypatch.setattr(installer, 'load_deployer', lambda _: SimpleDeployer())
    result = installer.install(root, stage, lambda *_: pytest.fail('deployment repeated'))
    assert result['already_verified']


class SimpleDeployer:
    def validate_manifest(self, manifest, root):
        return manifest


def test_partial_infra_install_restores_before_any_deployment(tmp_path, monkeypatch):
    import release_installer as installer
    data, manifest = artifact()
    stage, root, backup = tmp_path / 'stage', tmp_path / 'infra', tmp_path / 'backup'
    unpack(data, manifest, stage)
    root.mkdir()
    backup.mkdir()
    (backup / 'deploy-release.py').write_text('old')
    (root / 'deploy-release.py').write_text('partial-new')
    write_json(root / '.cd-install.json', {'release': manifest['commit'], 'backup': str(backup),
        'history': str(tmp_path / 'history'), 'installed': ['deploy-release.py'], 'status': 'installing'})
    class Deployer(SimpleDeployer):
        def Deployment(self, *args):
            raise RuntimeError('stop after reinstall')
    monkeypatch.setattr(installer, 'load_deployer', lambda _: Deployer())
    with pytest.raises(RuntimeError, match='stop after reinstall'):
        installer.install(root, stage, lambda *_: None)
    # The new backup must contain the recovered old version, not the partial install.
    record = read_json(root / '.cd-install.json')
    assert (Path(record['backup']) / 'deploy-release.py').read_text() == 'old'


def test_observation_failure_forces_coordinated_rollback(tmp_path, monkeypatch):
    deploy = module('deploy-release.py')
    value = deploy.Deployment(tmp_path, {'commit': 'a' * 40, 'compose_sha256': 'b' * 64})
    calls = []
    value.recover = lambda force=False: calls.append(force)
    value.assert_no_manual_tasks = lambda: None
    value.pull = lambda: None
    value.begin_journal = lambda: None
    value.prepare_legacy_worker = lambda: None
    value.pause_background = lambda: None
    value.refresh_inventory = lambda: None
    value.deploy = dict
    monkeypatch.setattr(deploy, 'observe_and_retire', lambda *_: (_ for _ in ()).throw(RuntimeError('unhealthy')))
    with pytest.raises(RuntimeError, match='unhealthy'):
        deploy.execute_release(value)
    assert calls == [False, True]


def test_lifecycle_timeout_checks_exact_container_without_repeating(tmp_path, monkeypatch):
    import subprocess
    staged = module('deploy-staged-images.py')
    value = staged.Release(tmp_path, 'a' * 40)
    calls = []
    def timeout(args, **kwargs):
        calls.append(args)
        raise subprocess.TimeoutExpired(args, 1200)
    monkeypatch.setattr(staged.subprocess, 'check_output', timeout)
    value.inspect = lambda name: {'State': {'Running': True}, 'Image': 'expected', 'Id': 'expected'}
    args = [value.compose, 'run', '-d', '--name', 'exact-candidate', 'api']
    assert value.run(args) == 'exact-candidate'
    assert calls == [args]
    value.inspect = lambda name: {'State': {'Running': False}}
    with pytest.raises(RuntimeError, match='not repeated'):
        value.run(args)
    assert calls == [args, args]


@pytest.mark.parametrize('failure', [None, 'route', 'image', 'lease'])
def test_runtime_verification_checks_actual_roles_and_worker_leases(tmp_path, failure):
    deploy = module('deploy-release.py')
    _, manifest = artifact()
    value = deploy.Deployment(tmp_path, manifest)
    names = ['api', 'web', *deploy.staged.BACKGROUND_SERVICES]
    items = {name: {'Id': name, 'Image': 'image', 'State': {'Running': True, 'StartedAt': 'now'},
                    'Config': {'Hostname': name, 'Env': ['API_INTERNAL_URL=http://api:8000']}}
             for name in names}
    value.service = lambda name: name
    value.inspect = lambda name: {'Id': 'image'} if name.startswith('ghcr.io/') else items[name]
    value.runtime_config = lambda _: {'handler': 'reverse_proxy', 'upstreams': [{'dial': 'wrong:8000' if failure == 'route' else 'api:8000'}, {'dial': 'web:3000'}]}
    calls = []
    def run(args):
        calls.append(args)
        if args[1] == 'inspect':
            return json.dumps([items[name] for name in args[2:]])
        return json.dumps({'missing_heartbeats': 0, 'expired_leases': int(failure == 'lease'), 'unclassified_jobs': 0})
    value.run = run
    if failure == 'image':
        items['image-ocr']['Image'] = 'old-image'
    result = {'api': 'api', 'web': 'web', 'proxy': 'proxy', 'effective_upstreams': ['api:8000', 'web:3000']}
    if failure:
        with pytest.raises(RuntimeError):
            value.verify_runtime(result)
    else:
        proof = value.verify_runtime(result)
        assert len(proof['containers']) == 11 and not any(proof['jobs'].values())
        assert len([call for call in calls if call[1] == 'inspect']) == 1


def test_published_release_cannot_be_clobbered(monkeypatch):
    from types import SimpleNamespace
    publish = module('release-manifest.py')
    _, manifest = artifact()
    monkeypatch.setattr(publish.subprocess, 'run', lambda *a, **k: SimpleNamespace(returncode=0, stdout='{"isDraft":false}'))
    calls = []
    def run(*args):
        calls.append(args)
        return json.dumps({'assets': [{'name': 'release.json', 'url': 'manifest'}]}) if 'manifest' not in args else json.dumps(manifest)
    monkeypatch.setattr(publish, 'run', run)
    publish.publish(manifest)
    assert not any('upload' in call for call in calls)
    with pytest.raises(RuntimeError, match='overwrite'):
        publish.publish(dict(manifest, compose_sha256='f' * 64))


def test_missing_credential_status_does_not_require_a_network_session(tmp_path):
    cd = module('nas-cd.py')
    value = cd.Runner({'infra': str(tmp_path), 'state_dir': str(tmp_path / 'cd'), 'token_file': str(tmp_path / 'missing')})
    assert not value.status()['credential_ready']
    assert value.run_once()['waiting'] == 'github_credential'
    assert value.cleanup() == {'maintenance': 'paused'}


@pytest.mark.parametrize('message,expected', [('i/o timeout', 'TransportError'), ('unauthorized: authentication required', 'ConfigurationError')])
def test_registry_network_errors_retry_but_access_errors_pause(tmp_path, message, expected):
    import subprocess

    import release_common
    deploy = module('deploy-release.py')
    _, manifest = artifact()
    value = deploy.Deployment(tmp_path, manifest)
    value.inspect = lambda _: {'RepoDigests': []}
    value.run = lambda args: (_ for _ in ()).throw(subprocess.CalledProcessError(1, args, output=message))
    with pytest.raises(getattr(release_common, expected)):
        value.pull()


def test_full_runner_keeps_lock_and_only_retries_failed_reporting(tmp_path, monkeypatch):
    cd, value = runner(tmp_path, monkeypatch)
    data, manifest = artifact()
    write_json(value.root / '.release-state.json', {'release': 'a' * 40})
    monkeypatch.setattr(cd, 'discover', lambda github, live, requested=None: None if live == manifest['commit'] else {'manifest': manifest, 'asset': 'archive'})
    installs = []
    def install(root, stage, event):
        # The same lock must still be held when installation and cutover run.
        with cd.locked(root / '.deploy.lock') as acquired:
            assert not acquired
        installs.append(read_json(stage / 'release.json')['commit'])
        event('verify')
        write_json(root / '.release-state.json', {'release': manifest['commit']})
        return {'observed_seconds': 300}
    monkeypatch.setattr(cd, 'install', install)
    monkeypatch.setattr(cd, 'upgrade_runtime', lambda *_: None)
    original = value.github.request
    offline = [True]
    def request(path, body=None, binary=False):
        if path == 'archive':
            return data
        if path.endswith('/statuses') and offline[0]:
            raise TransportError('reporting offline')
        return original(path, body, binary)
    value.github.request = request
    result = value.run_once()
    assert result['phase'] == 'success' and not result['active']
    assert value.state['outbox'] and installs == [manifest['commit']]
    offline[0] = False
    value.run_once()
    assert not value.state['outbox'] and installs == [manifest['commit']]


def test_newer_manual_release_blocks_resuming_an_older_attempt(tmp_path, monkeypatch):
    cd, value = runner(tmp_path, monkeypatch)
    data, manifest = artifact()
    stage = value.home / 'stage'
    unpack(data, manifest, stage)
    write_json(value.root / '.release-state.json', {'release': 'c' * 40})
    value.state.update(active=True, target=manifest['commit'], stage=str(stage), ci_run='123', attempt='attempt')
    value.save()
    monkeypatch.setattr(cd, 'install', lambda *_: pytest.fail('older version must not install'))
    result = value.run_once()
    assert result['paused'] and 'newer manual release' in result['error']


def test_github_outage_cannot_block_an_interrupted_install_recovery(tmp_path, monkeypatch):
    cd, value = runner(tmp_path, monkeypatch)
    data, manifest = artifact()
    stage = value.home / 'stage'
    unpack(data, manifest, stage)
    (value.home / 'attempts').mkdir()
    write_json(value.root / '.release-state.json', {'release': 'a' * 40})
    write_json(value.root / '.cd-install.json', {'release': manifest['commit'], 'previous_release': 'a' * 40})
    value.state.update(active=True, target=manifest['commit'], stage=str(stage), ci_run='123', attempt='attempt',
                       started_at=0, outbox=[{'state': 'in_progress'}])
    value.save()
    value.github.request = lambda *a, **k: (_ for _ in ()).throw(TransportError('offline'))
    value.github.ancestor = lambda *a: pytest.fail('recovery must not need GitHub ancestry')
    recovered = []
    monkeypatch.setattr(cd, 'install', lambda *a: recovered.append(True) or {'observed_seconds': 300})
    monkeypatch.setattr(cd, 'upgrade_runtime', lambda *a: None)
    result = value.run_once()
    assert recovered and result['phase'] == 'success'
    assert value.state['outbox'] and not value.state['active']
