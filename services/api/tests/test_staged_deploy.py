"""Release failures must leave the previous site available."""
import importlib.util
import json
import re
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

path = Path(__file__).resolve().parents[3] / 'infra' / 'deploy-staged-images.py'
spec = importlib.util.spec_from_file_location('staged_deploy', path)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


@pytest.mark.parametrize('service', ['api', 'web'])
def test_cold_candidate_can_start_after_sixty_refusals(tmp_path, monkeypatch, service):
    clock = [0.0]
    monkeypatch.setattr(module, 'time', SimpleNamespace(
        time_ns=lambda: 0,
        monotonic=lambda: clock[0],
        sleep=lambda seconds: clock.__setitem__(0, clock[0] + seconds)))
    release = module.Release(tmp_path, 'a' * 40)
    calls = []

    def probe(args):
        calls.append(args)
        clock[0] += 4
        if len(calls) <= 80:
            raise subprocess.CalledProcessError(1, args)
        return ''

    release.run = probe
    release.ready('cold-candidate', service)
    assert len(calls) == 81
    assert 120 < clock[0] < 600
    assert all(args[:3] == [release.docker, 'exec', 'cold-candidate'] for args in calls)


def test_unhealthy_candidate_exhausts_elapsed_startup_budget(tmp_path, monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(module, 'time', SimpleNamespace(
        time_ns=lambda: 0,
        monotonic=lambda: clock[0],
        sleep=lambda seconds: clock.__setitem__(0, clock[0] + seconds)))
    release = module.Release(tmp_path, 'a' * 40)

    def refused(args):
        clock[0] += 5
        raise subprocess.CalledProcessError(1, args)

    release.run = refused
    with pytest.raises(RuntimeError, match='within 600s'):
        release.ready('unhealthy-candidate', 'api')
    assert 600 <= clock[0] <= 605
    assert not release.state_path.exists()


class SimulatedRelease(module.Release):
    def __init__(self, root, failure=None, identical_web=True):
        super().__init__(root, 'a' * 40)
        self.failure = failure
        self.events = []
        self.config = root / 'Caddyfile'
        self.original = 'handle /api/* {\n reverse_proxy api:8000\n}\nhandle {\n reverse_proxy web:3000\n}\n'
        self.config.write_text(self.original)
        self.containers = {
            'api': {'Image': 'old-api', 'State': {'Running': True}},
            'web': {'Image': 'new-web' if identical_web else 'old-web', 'State': {'Running': True}, 'Config': {'Env': ['API_INTERNAL_URL=http://api:8000']}},
        }
        self.probes = 0
        self.remote_files = {'/etc/caddy/Caddyfile': self.original}
        self.active = self.parse_caddy(self.original)

    @staticmethod
    def parse_caddy(value):
        return {'handler': 'reverse_proxy', 'upstreams': [
            {'dial': target} for target in re.findall(r'reverse_proxy\s+(\S+)', value)]}

    def runtime_config(self, proxy):
        return self.active

    def service(self, name):
        return name

    def inspect(self, name):
        if name.startswith('monica-next-'):
            return {'Id': 'new-' + name.split(':')[0].removeprefix('monica-next-')}
        if name == 'reverse-proxy':
            return {'Mounts': [{'Source': str(self.config), 'Destination': '/etc/caddy/Caddyfile'}]}
        return self.containers[name]

    def run(self, args):
        self.events.append(args)
        if args[0] == self.compose and '--name' in args:
            self.containers[args[args.index('--name') + 1]] = {'Image': 'new-' + args[-1], 'State': {'Running': True}}
        if 'validate' in args and self.failure == 'validate':
            raise subprocess.CalledProcessError(1, args)
        if 'wget' in args and self.failure == 'network':
            raise subprocess.CalledProcessError(1, args)
        if 'cp' in args:
            self.remote_files[args[-1].split(':', 1)[1]] = Path(args[-2]).read_text()
        if 'reload' in args:
            name = args[args.index('--config') + 1]
            if self.failure != 'routing' or name.endswith('.json'):
                value = self.remote_files[name]
                self.active = json.loads(value) if name.endswith('.json') else self.parse_caddy(value)
        return ''

    def ready(self, name, service):
        self.events.append(['ready', name, service])
        self.probes += 1
        if self.probes <= 2:
            assert self.config.read_text() == self.original
        if (self.failure == 'ready' and self.probes == 1) or (self.failure == 'after-switch' and self.probes == 3):
            raise RuntimeError('Candidate unavailable')


def test_candidates_ready_before_switch_and_web_api_dependency_updated(tmp_path):
    release = SimulatedRelease(tmp_path)
    result = release.deploy()
    assert result['web'] != 'web'
    assert result['previous'] == {'api': 'api', 'web': 'web'}
    events = release.events
    reload_index = next(i for i, a in enumerate(events) if 'reload' in a)
    assert len([a for a in events[:reload_index] if a[0] == 'ready']) == 2
    assert not any('stop' in a for a in events)
    assert Path(result['previous_config']).read_text() == release.original
    assert result['api'] + ':8000' in release.config.read_text()
    # The old read-only bind mount can retain a different inode indefinitely.
    assert release.remote_files['/etc/caddy/Caddyfile'] == release.original
    assert result['effective_upstreams'] == sorted([result['api'] + ':8000', result['web'] + ':3000'])
    assert release.state_path.exists()


@pytest.mark.parametrize('failure', ['ready', 'network', 'validate', 'after-switch', 'routing'])
def test_release_failure_preserves_routes_and_previous_containers(tmp_path, failure):
    release = SimulatedRelease(tmp_path, failure=failure, identical_web=False)
    with pytest.raises((RuntimeError, subprocess.CalledProcessError)):
        release.deploy()
    assert release.config.read_text() == release.original
    assert not release.state_path.exists()
    stopped = [a[-1] for a in release.events if 'stop' in a]
    assert stopped == release.created
    assert 'api' not in stopped and 'web' not in stopped
    assert release.active == release.parse_caddy(release.original)
    if failure in ('after-switch', 'routing'):
        assert len([a for a in release.events if 'reload' in a]) == 2


def test_prune_keeps_live_and_rollback_releases_only():
    names = ['monica-api-0ecd62cdd928-68871331', 'monica-web-0ecd62cdd928-83721064',
             'monica-api-d3868b7b729c-23894869', 'monica-web-d3868b7b729c-79286765',
             'monica-api-e195194a6e1e-16567188', 'monica-web-8c1eda1f2fb5-89158321',
             'infra-postgres-1', 'infra-api-1']
    keep = set(names[:4])
    assert module.Release.stale_releases(names, keep) == [
        'monica-api-e195194a6e1e-16567188', 'monica-web-8c1eda1f2fb5-89158321']


@pytest.mark.parametrize('started_during_cleanup', [False, True])
def test_cleanup_preserves_an_independent_running_task(tmp_path, started_during_cleanup):
    release = module.Release(tmp_path, 'a' * 40)
    name = 'monica-api-bbbbbbbbbbbb-12345678'
    containers = {name: not started_during_cleanup}

    def docker(args):
        if args[1] == 'ps':
            return '\n'.join(containers)
        if args[1] == 'inspect':
            return json.dumps([{'State': {'Running': containers[args[-1]]}}])
        if args[1] == 'rm':
            if started_during_cleanup:
                containers[name] = True
            if containers[name] and '-f' not in args:
                raise subprocess.CalledProcessError(1, args, 'Container is running')
            containers.pop(name)
            return name
        raise AssertionError(args)

    release.run = docker
    if started_during_cleanup:
        with pytest.raises(subprocess.CalledProcessError):
            release.prune(set())
    else:
        assert release.prune(set()) == []
    assert containers == {name: True}
    assert release.cleanup_deferred == [name]


def test_reusing_live_containers_retains_deferred_cleanup(tmp_path):
    release = SimulatedRelease(tmp_path)
    release.containers['api']['Image'] = 'new-api'
    pending = ['monica-api-bbbbbbbbbbbb-12345678']
    release.state_path.write_text(json.dumps({'api': 'api', 'web': 'web', 'cleanup_deferred': pending}))
    result = release.deploy()
    assert result['cleanup_deferred'] == pending
    assert json.loads(release.state_path.read_text())['cleanup_deferred'] == pending


def test_redeploying_the_live_release_keeps_its_recorded_rollback(tmp_path):
    release = SimulatedRelease(tmp_path)
    release.containers['api']['Image'] = 'new-api'
    rollback_config = tmp_path / 'original-rollback.caddy'
    rollback_runtime = tmp_path / 'original-rollback.json'
    rollback_config.write_text('original rollback routes')
    rollback_runtime.write_text('{"rollback": true}')
    release.state_path.write_text(json.dumps({'api': 'api', 'web': 'web',
                                              'previous': {'api': 'old-api-container', 'web': 'old-web-container'},
                                              'previous_config': str(rollback_config),
                                              'previous_runtime_config': str(rollback_runtime)}))
    result = release.deploy()
    assert result['pruned'] == []
    assert result['previous'] == {'api': 'old-api-container', 'web': 'old-web-container'}
    assert result['previous_config'] == str(rollback_config)
    assert result['previous_runtime_config'] == str(rollback_runtime)
    assert json.loads(release.state_path.read_text())['previous'] == result['previous']
    assert not any('rm' in a for a in release.events)


def test_cleanup_is_not_on_the_release_path(tmp_path):
    release = SimulatedRelease(tmp_path, identical_web=False)
    def fail(_keep):
        raise RuntimeError('cleanup unavailable')
    release.prune = fail
    result = release.deploy()
    assert result['pruned'] == []
    state = json.loads(release.state_path.read_text())
    assert state['api'] + ':8000' in release.upstreams(release.active)
    assert not any('stop' in event for event in release.events)
