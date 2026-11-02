#!/usr/bin/env python3
"""Validate candidate containers before atomically reloading the NAS proxy.

Run on the NAS from infra, after loading immutable Actions artifacts. Previous
containers and the previous proxy configuration are retained for rollback.
"""
import argparse
import fcntl
import json
import os
import re
import subprocess
import time
from pathlib import Path


RELEASE_NAME = re.compile(r'monica-(api|web)-[0-9a-f]{12}-\d{8}')


class Release:
    def __init__(self, root, tag):
        self.root = Path(root)
        self.tag = tag
        self.docker = os.environ.get('DOCKER_BINARY', '/var/packages/ContainerManager/target/usr/bin/docker')
        self.compose = os.environ.get('COMPOSE_BINARY', '/var/packages/ContainerManager/target/usr/bin/docker-compose')
        self.env = dict(os.environ, IMAGE_TAG=tag)
        self.state_path = self.root / '.release-state.json'
        self.created = []
        self.before_switch = None
        self.attempt_id = str(time.time_ns())
        self.timings = []
        self.cleanup_deferred = []

    def run(self, args):
        started = time.monotonic()
        try:
            return subprocess.check_output(args, cwd=self.root, env=self.env, stdin=subprocess.DEVNULL, text=True, timeout=1200,
                                           stderr=subprocess.STDOUT if args[:2] == [self.docker, 'pull'] else None).strip()
        except subprocess.TimeoutExpired:
            # The daemon can finish after its client times out. Never resubmit.
            name = None
            running = True
            if args[0] == self.compose and '--name' in args:
                name = args[args.index('--name') + 1]
            elif args[0] == self.compose and 'up' in args:
                name = self.service(args[-1])
            elif args[0] == self.docker and args[1] in ('start', 'stop'):
                name = args[-1]
                running = args[1] == 'start'
            if name:
                actual = self.inspect(name)
                if actual['State']['Running'] == running:
                    if args[0] == self.compose:
                        role = 'web' if args[-1] == 'web' else 'worker' if args[-1].startswith('worker') or args[-1] == 'scheduler' else 'api'
                        expected = self.inspect('monica-next-' + role + ':' + self.tag)['Id']
                        if actual['Image'] != expected:
                            raise RuntimeError('Timed-out lifecycle operation has an unexpected image') from None
                    return name
            raise RuntimeError('Docker operation timed out; exact runtime requires recovery; operation was not repeated') from None
        finally:
            self.timings.append({'command': Path(args[0]).name, 'operation': args[1] if len(args)>1 else '',
                                 'seconds': round(time.monotonic()-started, 3)})

    def inspect(self, name):
        return json.loads(self.run([self.docker, 'inspect', name]))[0]

    def service(self, name):
        for cid in self.run([self.compose, 'ps', '-a', '-q', name]).splitlines():
            item = self.inspect(cid)
            if item['Config'].get('Labels', {}).get('com.docker.compose.oneoff', '').lower() != 'true':
                return item['Name'].lstrip('/')
        raise RuntimeError('No existing ' + name + ' service')

    def candidate(self, service, previous, api=None):
        image = 'monica-next-' + service + ':' + self.tag
        target = self.inspect(image)['Id']
        current = self.inspect(previous)
        api_matches = not api or ('API_INTERNAL_URL=http://' + api + ':8000') in current.get('Config', {}).get('Env', [])
        if current['Image'] == target and current['State']['Running'] and api_matches and not getattr(self, 'force_config_reload', False):
            return previous
        name = 'monica-' + service + '-' + self.tag[:12] + '-' + str(time.time_ns())[-8:]
        args = [self.compose, 'run', '-d', '--no-deps', '--name', name]
        if api:
            args += ['-e', 'API_INTERNAL_URL=http://' + api + ':8000']
        args += [service]
        self.run(args)
        self.created.append(name)
        assert self.inspect(name)['Image'] == target, 'Candidate image differs from Actions artifact'
        self.run([self.docker, 'update', '--restart', 'unless-stopped', name])
        return name

    def ready(self, name, service):
        if service == 'api':
            probe = ['/app/.venv/bin/python', '-c', 'import urllib.request;urllib.request.urlopen("http://127.0.0.1:8000/api/v1/ready",timeout=5).read()']
        else:
            # Existing standalone Next containers bind to Docker's HOSTNAME,
            # which can be their container IP rather than loopback.
            probe = ['node', '-e', 'fetch("http://"+require("os").hostname()+":3000/login",{signal:AbortSignal.timeout(5000)}).then(r=>{if(!r.ok)process.exit(1)}).catch(()=>process.exit(1))']
        # Cold imports on the NAS can exceed a fixed number of fast refusals.
        # Use elapsed time so startup gets a bounded, predictable budget.
        budget = 600
        deadline = time.monotonic() + budget
        while True:
            try:
                self.run([self.docker, 'exec', name, *probe])
                return
            except subprocess.CalledProcessError:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise RuntimeError(f'{service} candidate did not become ready within {budget}s') from None
                time.sleep(min(2, remaining))

    def runtime_config(self, proxy):
        return json.loads(self.run([self.docker, 'exec', proxy, 'wget', '-qO-',
                                    'http://127.0.0.1:2019/config/']))

    @staticmethod
    def upstreams(value):
        found = set()
        if isinstance(value, dict):
            if value.get('handler') == 'reverse_proxy':
                found.update(item['dial'] for item in value.get('upstreams', []) if 'dial' in item)
            for child in value.values():
                found.update(Release.upstreams(child))
        elif isinstance(value, list):
            for child in value:
                found.update(Release.upstreams(child))
        return found

    @staticmethod
    def stale_releases(names, keep):
        """Release candidates other than the live and immediate rollback pair."""
        return sorted(n for n in names if RELEASE_NAME.fullmatch(n) and n not in keep)

    def prune(self, keep):
        names = self.run([self.docker, 'ps', '-a', '--format', '{{.Names}}']).splitlines()
        stale = self.stale_releases(names, keep)
        self.cleanup_deferred = list(stale)
        removed = []
        for name in stale:
            if self.inspect(name)['State']['Running']:
                print(json.dumps({'cleanup_deferred': name, 'reason': 'container still running'}), flush=True)
                continue
            # Without --force Docker also protects a container started after
            # inspection, including an operator's independent manual task.
            self.run([self.docker, 'rm', name])
            self.cleanup_deferred.remove(name)
            removed.append(name)
        return removed

    def migrate(self):
        self.run([self.compose, 'run', '--rm', '--no-deps', '-T', 'migrate'])

    def deploy(self):
        self.run([self.compose, 'config', '--quiet'])
        for role in ('api', 'web'):
            self.inspect('monica-next-' + role + ':' + self.tag)
        state = json.loads(self.state_path.read_text()) if self.state_path.exists() else {}
        previous = {role: state.get(role) or self.service(role) for role in ('api', 'web')}
        proxy = self.service('reverse-proxy')
        mounted = [m['Source'] for m in self.inspect(proxy)['Mounts'] if m['Destination'] == '/etc/caddy/Caddyfile']
        if len(mounted) != 1:
            raise RuntimeError('Expected one persistent Caddyfile mount')
        config = Path(mounted[0])
        original = config.read_text()
        original_runtime = self.runtime_config(proxy)
        # These migrations must remain compatible with the previous live API.
        self.migrate()
        switched = False
        try:
            api = self.candidate('api', previous['api'])
            self.ready(api, 'api')
            web = self.candidate('web', previous['web'], api)
            self.ready(web, 'web')
            for url in ('http://' + api + ':8000/api/v1/ready', 'http://' + web + ':3000/login'):
                self.run([self.docker, 'exec', proxy, 'wget', '-qO-', url])
            changed, api_count = re.subn(r'(reverse_proxy\s+)[\w.-]+:8000\b', lambda m: m[1] + api + ':8000', original)
            changed, web_count = re.subn(r'(reverse_proxy\s+)[\w.-]+:3000\b', lambda m: m[1] + web + ':3000', changed)
            if api_count < 1 or web_count != 1:
                raise RuntimeError('Unexpected proxy configuration; original routing retained')
            if self.before_switch:
                self.before_switch()
            history = self.root / '.release-history'
            history.mkdir(exist_ok=True)
            stamp = self.tag + '-' + str(time.time_ns())
            backup = history / (stamp + '.previous.caddy')
            backup.write_text(original)
            runtime_backup = history / (stamp + '.previous.json')
            runtime_backup.write_text(json.dumps(original_runtime))
            candidate = history / (stamp + '.candidate.caddy')
            candidate.write_text(changed)
            remote = '/tmp/monica-candidate-' + stamp + '.caddy'
            self.run([self.docker, 'cp', str(candidate), proxy + ':' + remote])
            self.run([self.docker, 'exec', proxy, 'caddy', 'validate', '--config', remote, '--adapter', 'caddyfile'])
            # Persist the startup configuration. An earlier atomic replacement
            # may already have detached the container's bind-mounted inode, so
            # reload the explicit validated copy, never the mount's old view.
            with config.open('w') as stream:
                stream.write(changed)
                stream.flush()
                os.fsync(stream.fileno())
            switched = True
            self.run([self.docker, 'exec', proxy, 'caddy', 'reload', '--config', remote, '--adapter', 'caddyfile'])
            if self.upstreams(self.runtime_config(proxy)) != {api + ':8000', web + ':3000'}:
                raise RuntimeError('Effective proxy routing does not match candidate containers')
            self.ready(api, 'api')
            self.ready(web, 'web')
            result = {'release': self.tag, 'attempt_id': self.attempt_id, 'timings': self.timings, 'api': api, 'web': web, 'previous': previous,
                      'previous_config': str(backup), 'previous_runtime_config': str(runtime_backup),
                      'effective_upstreams': sorted(self.upstreams(self.runtime_config(proxy))), 'proxy': proxy}
            result['background'] = getattr(self, 'background_versions', {})
            if hasattr(self, 'manifest'):
                result['manifest'] = self.manifest
            # Keep the live pair and every rollback pair already recorded; a
            # repeated deploy of the live release must not discard its rollback.
            recorded = state.get('previous') or {}
            reused_pair = {api, web} == set(previous.values())
            result['previous'] = (recorded or previous) if reused_pair else previous
            if reused_pair and recorded:
                for key in ('previous_config', 'previous_runtime_config'):
                    if state.get(key):
                        result[key] = state[key]
            temporary = self.state_path.with_suffix('.tmp')
            temporary.write_text(json.dumps(result, indent=2) + '\n')
            temporary.replace(self.state_path)
            # Historical container deletion belongs to the separate maintenance job.
            result['pruned'] = []
            deferred = state.get('cleanup_deferred', [])
            if deferred:
                result['cleanup_deferred'] = deferred
            temporary.write_text(json.dumps(result, indent=2) + '\n')
            temporary.replace(self.state_path)
            print(json.dumps(result), flush=True)
            return result
        except BaseException:
            if switched:
                with config.open('w') as stream:
                    stream.write(original)
                    stream.flush()
                    os.fsync(stream.fileno())
                # If rollback reload fails, retain both sets of running services
                # and fail loudly; never stop a potentially active upstream.
                rollback = '/tmp/monica-rollback-' + stamp + '.json'
                self.run([self.docker, 'cp', str(runtime_backup), proxy + ':' + rollback])
                self.run([self.docker, 'exec', proxy, 'caddy', 'reload', '--config', rollback])
                if self.runtime_config(proxy) != original_runtime:
                    raise RuntimeError('Effective proxy rollback could not be verified')
                if state:
                    temporary = self.state_path.with_suffix('.tmp')
                    temporary.write_text(json.dumps(state, indent=2) + '\n')
                    temporary.replace(self.state_path)
                else:
                    self.state_path.unlink(missing_ok=True)
            for name in self.created:
                self.run([self.docker, 'stop', '--time', '30', name])
            raise


BACKGROUND_SERVICES = ('linkedin-sync', 'archive-sync', 'instagram-story-sync', 'image-ocr', 'worker', 'worker-ai', 'worker-media', 'worker-media-2', 'scheduler')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('release')
    parser.add_argument('--background', action='store_true',
                        help='also recreate importers and the worker from the release images; '
                             'they resume from their committed checkpoints')
    args = parser.parse_args()
    if not re.fullmatch('[0-9a-f]{40}', args.release):
        parser.error('Expected the full Git commit SHA')
    release = Release(Path(__file__).resolve().parent, args.release)
    with (release.root / '.deploy.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if args.background:
            release.inspect('monica-next-worker:' + args.release)
        result = release.deploy()
        if args.background:
            # Record partial rollout explicitly; never report all services as
            # upgraded when compose failed after the interactive cutover.
            try:
                release.run([release.compose, 'up', '-d', '--no-deps', *BACKGROUND_SERVICES])
                result['background'] = {name: release.inspect(release.service(name))['Image']
                                        for name in BACKGROUND_SERVICES}
            except Exception as exc:
                result['background_error'] = str(exc)[:500]
                raise
            finally:
                temporary = release.state_path.with_suffix('.tmp')
                temporary.write_text(json.dumps(result, indent=2) + '\n')
                temporary.replace(release.state_path)


if __name__ == '__main__':
    main()
