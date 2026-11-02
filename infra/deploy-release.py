#!/usr/bin/env python3
"""Deploy a verified private GHCR manifest on NAS. Run from versioned infra.

Download release.json from the successful CI's verified-<SHA> draft release.
Authenticate Docker to GHCR with a read-only token before invoking this command.
"""
import argparse
import fcntl
import hashlib
import importlib.util
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from release_common import (
    ConfigurationError,
    TransportError,
    read_json,
    valid_image,
    validate_v2,
    write_json,
)

spec = importlib.util.spec_from_file_location('staged', Path(__file__).with_name('deploy-staged-images.py'))
staged = importlib.util.module_from_spec(spec)
spec.loader.exec_module(staged)
spec = importlib.util.spec_from_file_location('legacy_drain', Path(__file__).with_name('legacy-worker-drain.py'))
legacy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(legacy)


def validate_manifest(value, root):
    if value.get('version') == 2:
        validate_v2(value)
    if value.get('version') not in (1, 2) or not re.fullmatch('[0-9a-f]{40}', value.get('commit', '')):
        raise ValueError('Expected a v1 manifest with a full commit SHA')
    if not str(value.get('ci_run', '')).isdigit():
        raise ValueError('Missing verified CI run')
    if set(value.get('images', {})) != {'api', 'worker', 'web'}:
        raise ValueError('Incomplete image set')
    for role, image in value['images'].items():
        if not re.fullmatch(r'ghcr\.io/hmumixam/monica-next-'+role+r'@sha256:[0-9a-f]{64}', image):
            raise ValueError('Expected the private repository image pinned by digest')
    actual = hashlib.sha256((root/'docker-compose.yml').read_bytes()).hexdigest()
    if value.get('compose_sha256') != actual:
        raise ValueError('Compose differs from the verified release; install its versioned infra first')
    return value


class Deployment(staged.Release):
    def __init__(self, root, manifest):
        super().__init__(root, manifest['commit'])
        self.manifest = manifest
        self.background_before = {}
        self.background_changed = []
        self.background_versions = {}
        self.event = lambda phase, **details: None
        self.journal_path = self.root / '.deploy-journal.json'
        previous = json.loads(self.state_path.read_text()) if self.state_path.exists() else {}
        self.force_config_reload = previous.get('manifest', {}).get('compose_sha256') != manifest['compose_sha256']

    def refresh_inventory(self):
        """Two daemon calls for all Compose services, excluding candidate one-offs."""
        ids = self.run([self.compose, 'ps', '-a', '-q']).splitlines()
        inventory = {}
        for item in self.inspect_many(ids):
            labels = item.get('Config', {}).get('Labels', {})
            name = labels.get('com.docker.compose.service')
            if name and labels.get('com.docker.compose.oneoff', '').lower() != 'true':
                if name in inventory:
                    raise RuntimeError('Multiple containers for singleton service: ' + name)
                inventory[name] = item
        self.service_inventory = inventory

    def service(self, name):
        item = getattr(self, 'service_inventory', {}).get(name)
        return item['Name'].lstrip('/') if item else super().service(name)

    def ledger(self):
        sql = "BEGIN READ ONLY;SET LOCAL statement_timeout='15s';SELECT coalesce(json_agg(version),'[]'::json) FROM schema_migrations;COMMIT;"
        raw = self.run([self.docker, 'exec', self.service('postgres'), 'psql', '-U',
                        os.environ.get('POSTGRES_USER', 'crm'), '-d', os.environ.get('POSTGRES_DB', 'crm'),
                        '-X', '-qAt', '-c', sql])
        return set(json.loads(next(line for line in raw.splitlines() if line.startswith('['))))

    def migrate(self):
        self.event('migrate')
        if self.manifest.get('version') != 2:
            return super().migrate()
        expected = self.manifest['migrations']
        proof_path = self.root / '.migration-verification.json'
        previous = read_json(proof_path, {})
        same_api = previous.get('api_image') == self.manifest['images']['api']
        if (previous.get('migrations') == expected and not same_api
                and valid_image('api', previous.get('api_image'))):
            # Registry index digests can change with provenance attestations while
            # the actual application image is identical. Retain the old verified
            # receipt only when both immutable references resolve to the same ID.
            try:
                same_api = self.inspect(previous['api_image'])['Id'] == self.inspect(self.manifest['images']['api'])['Id']
            except subprocess.CalledProcessError:
                same_api = False
        same_runner = (self.manifest.get('migration_input') is not None
                       and previous.get('migration_input') == self.manifest['migration_input'])
        complete = previous.get('migrations') == expected and (same_api or same_runner)
        if not (complete and set(expected) <= self.ledger()):
            super().migrate()
            if not set(expected) <= self.ledger():
                raise RuntimeError('Migration ledger is incomplete after migrator')
        write_json(proof_path, {'migrations': expected, 'api_image': self.manifest['images']['api'],
                                'verified_at': time.time(), 'release': self.tag,
                                'migration_input': self.manifest.get('migration_input')})

    def audit_messages(self):
        self.event('audit')
        state = read_json(self.state_path, {})
        api = state.get('api') or self.service('api')
        paths = self.manifest['audit_inputs']
        mapped = {name: '/app/' + name[len('services/api/'):] for name in paths}
        probe = "import hashlib,json,pathlib;paths=" + repr(mapped) + ";print(json.dumps({k:hashlib.sha256(pathlib.Path(v).read_bytes()).hexdigest() for k,v in paths.items()}))"
        actual = json.loads(self.run([self.docker, 'exec', api, '/app/.venv/bin/python', '-c', probe]))
        api_id = self.inspect(api)['Id']
        proof_path = self.root / '.release-history' / ('audit-' + self.tag + '.json')
        receipt = read_json(proof_path, {})
        if (receipt.get('audited_release') == state.get('release') and receipt.get('container_id') == api_id
                and receipt.get('audit_inputs') == paths and time.time() - receipt.get('observed_at', 0) < 3600
                and receipt.get('report', {}).get('ambiguous') == 0):
            return
        if actual == paths:
            raw = self.run([self.docker, 'exec', '-e', 'PGOPTIONS=-c timezone=UTC', api, '/app/.venv/bin/python', '-m', 'app.message_key_audit'])
        else:
            # New normalization/dependencies require the verified target code. Old workers still run.
            raw = self.run([self.compose, 'run', '--rm', '--no-deps', '-T', '-e', 'PGOPTIONS=-c timezone=UTC', '--entrypoint',
                            '/app/.venv/bin/python', 'api', '-m', 'app.message_key_audit'])
        report = json.loads(raw)
        if report['ambiguous'] != 0 or report['timezone'] not in ('UTC', 'Etc/UTC'):
            raise RuntimeError('Message-key audit failed; no worker was drained')
        # Store only counts; audit examples can contain personal identifiers.
        report.pop('examples', None)
        write_json(proof_path, {'release': self.tag, 'audited_release': state.get('release'),
                   'container_id': api_id, 'audit_inputs': paths, 'read_only': True,
                   'observed_at': time.time(), 'report': report})

    def verify_runtime(self, result):
        self.event('verify')
        if self.upstreams(self.runtime_config(result['proxy'])) != set(result['effective_upstreams']):
            raise RuntimeError('Live Caddy routes changed during observation')
        names = {role: result[role] for role in ('api', 'web')}
        names.update({name: self.service(name) for name in staged.BACKGROUND_SERVICES})
        observed = self.inspect_many(list(names.values()))
        containers = dict(zip(names, observed))
        if len(containers) != len(names):
            raise RuntimeError('Incomplete runtime container inventory')
        targets = {}
        proof = {}
        for name, item in containers.items():
            role = name if name in ('api', 'web') else 'worker' if name.startswith('worker') or name == 'scheduler' else 'api'
            target = (result.get('background', {}).get(name, {}).get('image')
                      if name in staged.BACKGROUND_SERVICES else None)
            if not target:
                if role not in targets:
                    targets[role] = self.inspect(self.manifest['images'][role])['Id']
                target = targets[role]
            if item['Image'] != target or not item['State']['Running'] or item['State'].get('OOMKilled') or item.get('RestartCount', 0):
                raise RuntimeError('Runtime image/health mismatch: ' + name)
            proof[name] = {'id': item['Id'], 'image': item['Image'], 'started_at': item['State']['StartedAt']}
        if 'API_INTERNAL_URL=http://' + result['api'] + ':8000' not in containers['web']['Config']['Env']:
            raise RuntimeError('Web points to another API')
        worker_hosts = [item['Config']['Hostname'] + ':%' for name, item in containers.items()
                        if name.startswith('worker') or name == 'scheduler']
        code = """import json
from sqlalchemy import text
from app.db import engine
hosts=HOSTS
with engine.connect() as db:
 db.execute(text('SET TRANSACTION READ ONLY'))
 db.execute(text("SET LOCAL statement_timeout='30s'"))
 db.execute(text("SET LOCAL lock_timeout='3s'"))
 missing=sum(not bool(db.execute(text("SELECT 1 FROM worker_heartbeats WHERE worker_id LIKE :host AND heartbeat_at>now()-interval '2 minutes' AND stopped_at IS NULL AND NOT draining"),{'host':host}).first()) for host in hosts)
 expired=db.execute(text("SELECT count(*) FROM jobs WHERE status='running' AND lease_expires_at<now()" )).scalar_one()
 unclassified=db.execute(text("SELECT count(*) FROM jobs WHERE status IN ('queued','running') AND resource_class IS NULL")).scalar_one()
 print(json.dumps({'missing_heartbeats':missing,'expired_leases':expired,'unclassified_jobs':unclassified}))
""".replace('HOSTS', repr(worker_hosts))
        health = json.loads(self.run([self.docker, 'exec', result['api'], '/app/.venv/bin/python', '-c', code]))
        if any(health.values()):
            raise RuntimeError('Worker/lease verification failed: ' + json.dumps(health))
        return {'containers': proof, 'jobs': health, 'verified_at': time.time()}

    def save_journal(self, value):
        temporary = self.journal_path.with_suffix('.tmp')
        with temporary.open('w') as output:
            os.chmod(temporary, 0o600)
            json.dump(value, output)
            output.flush()
            os.fsync(output.fileno())
        temporary.replace(self.journal_path)
        directory = os.open(self.root, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)

    def begin_journal(self):
        proxy = self.service('reverse-proxy')
        config = next(m['Source'] for m in self.inspect(proxy)['Mounts'] if m['Destination'] == '/etc/caddy/Caddyfile')
        self.journal = {'release': self.tag, 'attempt_id': self.attempt_id, 'proxy': proxy, 'config_path': config,
                        'config': Path(config).read_text(), 'runtime': self.runtime_config(proxy),
                        'state': json.loads(self.state_path.read_text()) if self.state_path.exists() else None,
                        'background': {}, 'changed': []}
        self.save_journal(self.journal)

    def recover(self, force=False):
        if not self.journal_path.exists():
            return None
        journal = json.loads(self.journal_path.read_text())
        current = json.loads(self.state_path.read_text()) if self.state_path.exists() else {}
        if not force and journal.get('attempt_id') and current.get('attempt_id') == journal['attempt_id'] and self.upstreams(self.runtime_config(journal['proxy'])) == set(current.get('effective_upstreams', [])):
            return current  # Commit succeeded; resume observation/retirement.
        rollback = self.root / '.recover-runtime.json'
        rollback.write_text(json.dumps(journal['runtime']))
        remote = '/tmp/monica-recover-' + journal['release'] + '.json'
        try:
            self.run([self.docker, 'cp', str(rollback), journal['proxy'] + ':' + remote])
            self.run([self.docker, 'exec', journal['proxy'], 'caddy', 'reload', '--config', remote])
            if self.upstreams(self.runtime_config(journal['proxy'])) != self.upstreams(journal['runtime']):
                raise RuntimeError('Recovery routing verification failed; journal retained')
            with Path(journal['config_path']).open('w') as output:
                output.write(journal['config']); output.flush(); os.fsync(output.fileno())
            self.background_before, self.background_changed = journal['background'], journal['changed']
            self.restore_background()
            if journal['state'] is None:
                self.state_path.unlink(missing_ok=True)
            else:
                temporary = self.state_path.with_suffix('.tmp')
                temporary.write_text(json.dumps(journal['state']))
                temporary.replace(self.state_path)
            self.journal_path.unlink()
        finally:
            rollback.unlink(missing_ok=True)
        return None

    def manual_tasks(self, container):
        probe = """import pathlib,json
found=[]
for p in pathlib.Path('/proc').glob('[0-9]*/cmdline'):
 try:
  args=[a.decode(errors='replace') for a in p.read_bytes().split(b'\\0') if a]
  scripts=[a for a in args if a.startswith(('/tmp/','/data/staging/')) and a.endswith('.py')]
  if scripts: found.append({'pid':int(p.parent.name),'script':pathlib.Path(scripts[0]).name})
 except (FileNotFoundError,PermissionError): pass
print(json.dumps(found))
"""
        return json.loads(self.run([self.docker, 'exec', container, '/app/.venv/bin/python', '-c', probe]))

    def assert_no_manual_tasks(self):
        state = json.loads(self.state_path.read_text()) if self.state_path.exists() else {}
        api = state.get('api') or self.service('api')
        tasks = self.manual_tasks(api)
        if tasks:
            raise RuntimeError('Manual API tasks still active; migration/switch deferred: '+json.dumps(tasks))

    def pull(self):
        for role, image in self.manifest['images'].items():
            try:
                installed = self.inspect(image)
            except subprocess.CalledProcessError:
                installed = {}
            # A successful prior pull records the immutable registry digest.
            # Rechecking mutable tags is unnecessary for a pinned local digest,
            # and registry retries can otherwise stall an entirely cached release.
            if image in (installed.get('RepoDigests') or []):
                print(json.dumps({'cached_digest': role, 'image': image}), flush=True)
            else:
                try:
                    self.run([self.docker, 'pull', image])
                except subprocess.CalledProcessError as error:
                    reason = (error.output or '').lower()
                    if any(word in reason for word in ('unauthorized', 'denied', 'authentication', 'manifest unknown', 'not found')):
                        raise ConfigurationError('GHCR rejected image access; inspect package access and credential expiry') from None
                    raise TransportError('GHCR pull temporarily failed before service changes') from None
            self.run([self.docker, 'tag', image, 'monica-next-'+role+':'+self.tag])

    def prepare_legacy_worker(self):
        try:
            worker = self.service('worker')
        except RuntimeError:
            return
        previous = self.inspect(worker)
        if not previous['State']['Running']:
            return
        compatible = self.run([self.docker, 'exec', worker, '/app/.venv/bin/python', '-c',
                               "import importlib.util; print(int(importlib.util.find_spec('app.job_queue') is not None))"])
        if compatible.strip() == '1':
            return
        self.background_before['worker'] = previous
        self.journal['background']['worker'] = previous
        self.journal['changed'] = ['worker']
        self.save_journal(self.journal)
        state = json.loads(self.state_path.read_text()) if self.state_path.exists() else {}
        api = state.get('api') or self.service('api')
        legacy.drain(self, api, worker)
        self.background_changed.append('worker')

    def inspect_many(self, names):
        if len(names) == 1:
            return [self.inspect(names[0])]
        return json.loads(self.run([self.docker, 'inspect', *names])) if names else []

    def background(self):
        self.pause_background()
        self.start_background()

    def pause_background(self):
        # Journal the entire replacement set before sending any stop signal.
        # All roles yield concurrently, and Compose bounds starts to two at once.
        self.event('background')
        targets = {}
        pending = []
        previous = read_json(self.state_path, {})
        for name in staged.BACKGROUND_SERVICES:
            if hasattr(self, 'service_inventory'):
                existing = self.service_inventory.get(name)
            else:
                try:
                    existing = self.inspect(self.service(name))
                except (RuntimeError, subprocess.CalledProcessError):
                    existing = None
            self.background_before.setdefault(name, existing)
            role = 'worker' if name.startswith('worker') or name == 'scheduler' else 'api'
            if role not in targets:
                targets[role] = self.inspect('monica-next-'+role+':'+self.tag)['Id']
            target = targets[role]
            same_inputs = (self.manifest.get('background_inputs', {}).get(role) is not None
                and self.manifest['background_inputs'][role] == previous.get('manifest', {}).get('background_inputs', {}).get(role)
                and existing is not None
                and previous.get('background', {}).get(name, {}).get('image') == existing['Image'])
            if existing and existing['State']['Running'] and not self.force_config_reload and (existing['Image'] == target or same_inputs):
                self.background_versions[name] = {'image': existing['Image'], 'running': True}
                continue
            self.background_versions[name] = {'image': target, 'running': True}
            if name not in self.background_changed:
                self.background_changed.append(name)
            self.journal['background'][name] = self.background_before[name]
            pending.append((name, existing, role, target))
        self.pending_background = pending
        if not pending:
            return
        self.journal['changed'] = list(self.background_changed)
        self.save_journal(self.journal)
        waiting = []
        for name, existing, _, _ in pending:
            if existing and existing['State']['Running']:
                container = existing['Name'].lstrip('/')
                self.event('pause:' + name)
                self.stop_background(name, container)
                waiting.append(container)
        # The first upgrade may still contain legacy workers; never kill them
        # or release their leases while their result transaction can still run.
        deadline = time.monotonic() + 960
        while waiting:
            running = self.inspect_many(waiting)
            waiting = [item['Name'].lstrip('/') for item in running if item['State']['Running']]
            if not waiting:
                break
            if time.monotonic() >= deadline:
                raise RuntimeError('Drain deadline exceeded; containers still running: ' + ', '.join(waiting))
            time.sleep(5)

    def start_background(self):
        pending = getattr(self, 'pending_background', [])
        if not pending:
            return
        self.event('containers')
        self.env['COMPOSE_PARALLEL_LIMIT'] = '2'
        self.run([self.compose, 'up', '-d', '--force-recreate', '--no-deps', *[p[0] for p in pending]])
        if hasattr(self, 'service_inventory'):
            self.refresh_inventory()
        for name, _, _, target in pending:
            current = (self.service_inventory.get(name) if hasattr(self, 'service_inventory')
                       else self.inspect(self.service(name)))
            if current is None:
                raise RuntimeError('Background service missing after start: '+name)
            if current['Image'] != target or not current['State']['Running']:
                raise RuntimeError('Background image/start mismatch: '+name)
        # One readiness/lease audit verifies every worker after the switch;
        # per-worker docker exec polling amplified NAS startup I/O dramatically.

    def stop_background(self, name, container):
        modules = {'archive-sync': 'app.sync_archives', 'linkedin-sync': 'app.sync_linkedin',
                   'instagram-story-sync': 'app.sync_instagram_stories'}
        if name in modules:
            legacy_source = self.run([self.docker, 'exec', container, '/app/.venv/bin/python', '-c',
                "import importlib.util;print('legacy' if importlib.util.find_spec('app.runtime_status') is None else 'native')"])
            if legacy_source.strip() == 'legacy':
                # Older uv PID 1 does not forward TERM; direct Python SIGINT
                # unwinds sessions/advisory locks and keeps committed checkpoints.
                # No SIGKILL or synthetic cursor advancement is used.
                interrupt = """import os,pathlib,signal,sys,json
target=sys.argv[1];pids=[]
for p in pathlib.Path('/proc').glob('[0-9]*/cmdline'):
 try:
  args=p.read_bytes().split(b'\\0')
  if b'-m' in args and args[args.index(b'-m')+1].decode()==target and 'python' in pathlib.Path(args[0].decode()).name:
   pids.append(int(p.parent.name))
 except (FileNotFoundError,PermissionError,IndexError):pass
assert len(pids)==1, pids
os.kill(pids[0],signal.SIGINT)
print(json.dumps({'legacy_source_interrupted':target,'pid':pids[0]}))
"""
                # An in-container exit otherwise triggers unless-stopped and
                # immediately launches the old importer again during the drain.
                self.run([self.docker, 'update', '--restart=no', container])
                self.run([self.docker, 'exec', container, '/app/.venv/bin/python', '-c', interrupt, modules[name]])
                return
        self.run([self.docker, 'kill', '--signal', 'TERM', container])

    def restore_restart_policy(self, previous, current):
        policy = previous.get('HostConfig', {}).get('RestartPolicy')
        if policy and current.get('HostConfig', {}).get('RestartPolicy') != policy:
            value = policy['Name']
            if value == 'on-failure' and policy.get('MaximumRetryCount'):
                value += ':'+str(policy['MaximumRetryCount'])
            self.run([self.docker, 'update', '--restart='+value, previous['Name']])

    def restore_background(self):
        if not self.background_changed:
            return
        override = self.root/'.background-rollback.json'
        services = {}
        retained = set()
        restarted = set()
        restart_original = {}
        recovered = set()
        for name in self.background_changed:
            previous = self.background_before[name]
            if not previous:
                continue
            value = {'image': previous['Image']}
            if 'Config' in previous:
                value.update(environment=previous['Config'].get('Env', []),
                             command=previous['Config'].get('Cmd'), entrypoint=previous['Config'].get('Entrypoint'))
            if previous.get('State', {}).get('Running') and previous.get('Name'):
                current = self.inspect(previous['Name'])
                if current['State']['Running'] and current['Image'] == previous['Image']:
                    self.restore_restart_policy(previous, current)
                    if (previous['State'].get('StartedAt') and current['State'].get('StartedAt')
                            and previous['State']['StartedAt'] != current['State']['StartedAt']):
                        # A previous recovery already started a fresh process;
                        # it cannot carry the original TERM handler's stop flag.
                        recovered.add(name)
                        continue
                    # A failed drain has not replaced this service. Preserve it
                    # while restoring other roles instead of stranding rollback.
                    retained.add(name)
                    continue
                if (not current['State']['Running'] and previous.get('Id')
                        and current.get('Id') == previous['Id'] and current['Image'] == previous['Image']
                        and not previous.get('State', {}).get('Paused')):
                    restarted.add(name)
                    restart_original[name] = (previous, current)
                    continue
            if previous.get('HostConfig', {}).get('Binds'):
                value['volumes'] = previous['HostConfig']['Binds']
            services[name] = value
        # Drain every replacement before restarting legacy code which does not
        # understand resource lanes or lease fencing. Stop the scheduler first.
        for name in reversed(self.background_changed):
            if name in retained or name in restarted or name in recovered:
                continue
            previous = self.background_before[name]
            if previous and 'Name' not in previous:
                continue
            try:
                current = self.inspect(self.service(name))
            except (RuntimeError, subprocess.CalledProcessError):
                continue
            if current['State']['Running']:
                self.run([self.docker, 'stop', '--time', '960', current['Name']])
        with override.open('w') as output:
            os.chmod(override, 0o600)
            json.dump({'services': services}, output)
        try:
            for name in reversed(self.background_changed):
                if name in retained or name in recovered:
                    continue
                if name in restarted:
                    previous, current = restart_original[name]
                    self.restore_restart_policy(previous, current)
                    self.run([self.docker, 'start', previous['Name']])
                    if not self.inspect(previous['Name'])['State']['Running']:
                        raise RuntimeError('Original background did not restart: '+name)
                    continue
                if self.background_before[name]:
                    self.run([self.compose, '-f', 'docker-compose.yml', '-f', str(override),
                              'up', '-d', '--force-recreate', '--no-deps', name])
                    if self.background_before[name].get('State', {}).get('Running') is False:
                        self.run([self.compose, 'stop', name])
                else:
                    self.run([self.compose, 'stop', name])
        finally:
            override.unlink(missing_ok=True)
        if retained:
            # TERM may already have set a stop flag in these original
            # processes. They can exit later, so retaining them is not a
            # completed rollback. Other roles are recovered; keep only these
            # pending roles in the journal until they exit and can be restarted.
            journal = json.loads(self.journal_path.read_text()) if self.journal_path.exists() else {}
            journal['changed'] = [name for name in self.background_changed if name in retained]
            self.save_journal(journal)
            raise RuntimeError('Other roles recovered; original roles still draining; journal retained: '+', '.join(sorted(retained)))


def observe_and_retire(release, result):
    release.event('observe')
    deadline = time.monotonic()+300
    while time.monotonic() < deadline:
        release.ready(result['api'], 'api')
        release.ready(result['web'], 'web')
        time.sleep(5)
    if release.manifest.get('version') == 2:
        result['verification'] = release.verify_runtime(result)
    release.event('retire')
    previous = set(result['previous'].values())
    for role in ('api', 'web'):
        try:
            previous.add(release.service(role))
        except RuntimeError:
            pass
    # Check both proxy routes and the live Web's server-side API dependency.
    referenced = {value.split(':')[0] for value in release.upstreams(release.runtime_config(result['proxy']))}
    env = release.inspect(result['web']).get('Config', {}).get('Env', [])
    for value in env:
        if value.startswith('API_INTERNAL_URL='):
            from urllib.parse import urlsplit
            referenced.add(urlsplit(value.split('=', 1)[1]).hostname)
    cleanup_errors = []
    for container in previous - {result['api'], result['web']} - referenced:
        try:
            if release.inspect(container)['State']['Running'] and container.startswith(('monica-api-', 'infra-api-')) and release.manual_tasks(container):
                cleanup_errors.append('Manual task still active in '+container+'; retained')
                continue
            release.run([release.docker, 'stop', '--time', '30', container])
        except Exception as error:  # noqa: BLE001 - retirement cannot undo a healthy release
            cleanup_errors.append(str(error)[:500])
    if cleanup_errors:
        result['cleanup_errors'] = cleanup_errors
    result['observed_seconds'] = 300
    result['timings'] = release.timings
    temporary = release.state_path.with_suffix('.tmp')
    temporary.write_text(json.dumps(result, indent=2)+'\n')
    temporary.replace(release.state_path)
    release.journal_path.unlink(missing_ok=True)


def execute_release(release):
    """Caller must hold the canonical deployment lock, including infra installation."""
    manifest = release.manifest
    pending = release.recover()
    if pending:
        try:
            observe_and_retire(release, pending)
        except BaseException:
            release.recover(force=True)
            raise
        if pending['release'] == release.tag:
            print(json.dumps({'resumed_release': release.tag}), flush=True)
            return
    release.assert_no_manual_tasks()
    release.event('images')
    release.pull()
    if manifest.get('version') == 2:
        release.audit_messages()
    release.assert_no_manual_tasks()
    release.begin_journal()
    release.before_switch = release.start_background
    try:
        release.assert_no_manual_tasks()
        release.prepare_legacy_worker()
        # Free NAS I/O before migration and candidate startup. Unchanged roles
        # remain live; replacement roles resume just before the route switch.
        release.refresh_inventory()
        release.pause_background()
        release.event('candidates')
        result = release.deploy()
        observe_and_retire(release, result)
    except BaseException:
        release.recover(force=True)
        raise
    print(json.dumps({'release': manifest['commit'], 'observed_seconds': 300,
                      'background_changed': release.background_changed}), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('manifest', type=Path)
    args = parser.parse_args()
    root = Path(__file__).resolve().parent
    manifest = validate_manifest(json.loads(args.manifest.read_text()), root)
    release = Deployment(root, manifest)
    with (root/'.deploy.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        execute_release(release)


if __name__ == '__main__':
    main()
