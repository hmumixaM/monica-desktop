#!/usr/bin/env python3
"""NAS-pulled CD. No git, build daemon or model session is needed by this process."""
import argparse
import contextlib
import datetime
import fcntl
import json
import os
import re
import time
from pathlib import Path

from release_common import (
    REPOSITORY,
    WORKFLOW,
    ConfigurationError,
    GitHub,
    TransportError,
    read_json,
    validate_v2,
    write_json,
)
from release_installer import install, load_deployer, unpack, upgrade_runtime


@contextlib.contextmanager
def locked(path):
    with Path(path).open('a') as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
        else:
            try:
                yield True
            finally:
                fcntl.flock(stream, fcntl.LOCK_UN)


class ReleaseNotReady(ValueError):
    """Publication precedes the publishing workflow's terminal success briefly."""


def eligible(github, manifest, main, live):
    validate_v2(manifest)
    sha = manifest['commit']
    run = github.request('/actions/runs/' + str(manifest['ci_run']))
    if (run['head_sha'] != sha or run['head_branch'] != 'main' or run['event'] != 'push'
            or run['path'] != WORKFLOW
            or run['repository']['full_name'].lower() != REPOSITORY.lower()):
        raise ValueError('Release CI is not a successful main push')
    if run['status'] != 'completed':
        raise ReleaseNotReady('Release CI has not completed yet')
    if run['conclusion'] != 'success':
        raise ValueError('Release CI is not a successful main push')
    if not github.ancestor(sha, main) or (live and not github.ancestor(live, sha)):
        raise ValueError('Release is not a forward main ancestor')


def discover(github, live, requested=None):
    main = github.request('/branches/main')['commit']['sha']
    if live == main and not requested:
        return None
    selected = None
    for release in github.pages('/releases'):
        tag = release['tag_name']
        if release['draft'] or not re.fullmatch('verified-[0-9a-f]{40}', tag) or tag[9:] == live:
            continue
        if requested and tag[9:] != requested:
            continue
        assets = {a['name']: a for a in release['assets']}
        if not {'release.json', 'deployment.tar'} <= set(assets):
            continue  # Historical v1 or incomplete publication.
        manifest = json.loads(github.request(assets['release.json']['url'], binary=True))
        if manifest.get('version') != 2:
            continue
        if manifest.get('commit') != tag[9:]:
            raise ValueError('Release tag/manifest mismatch')
        # Skip old releases, but validate a candidate completely before accepting it.
        if live and not github.ancestor(live, manifest['commit']):
            continue
        try:
            eligible(github, manifest, main, live)
        except ReleaseNotReady:
            continue  # The next scheduled discovery can accept terminal success.
        if selected is None or github.ancestor(selected['manifest']['commit'], manifest['commit']):
            selected = {'manifest': manifest, 'asset': assets['deployment.tar']['url']}
        if manifest['commit'] == main:
            break
    return selected


class Runner:
    def __init__(self, config):
        self.config = config
        self.root = Path(config['infra'])
        self.home = Path(config['state_dir'])
        self.home.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = self.home / 'state.json'
        self.state = read_json(self.path, {'paused': False, 'outbox': [], 'phase': 'idle'})
        token_file = Path(config['token_file'])
        if not token_file.exists():
            self.github = None
            return
        if token_file.stat().st_mode & 0o077:
            raise ConfigurationError('GitHub token file must be private to root')
        self.github = GitHub(token_file.read_text().strip())

    def save(self):
        write_json(self.path, self.state)

    def status(self):
        live = read_json(self.root / '.release-state.json', {})
        expiry = datetime.date.fromisoformat(self.config.get('ghcr_expires', '2027-01-14'))
        today = datetime.datetime.now(datetime.timezone.utc).date()
        days = (expiry - today).days
        github_days = (datetime.date.fromisoformat(self.config.get('github_expires', '2027-01-16')) - today).days
        result = {k: v for k, v in dict(self.state, github_credential_days_remaining=github_days, github_rotation_required=github_days <= 30, credential_ready=self.github is not None, paused=self.state.get('paused', False) or (self.home / 'paused').exists(), live_release=live.get('release'),
                    journal_present=(self.root / '.deploy-journal.json').exists(),
                    ghcr_credential_days_remaining=days, ghcr_rotation_required=days <= 30).items()
                if k not in ('outbox',)}
        if self.state.get('attempt'):
            result['attempt_record'] = str(self.home / 'attempts' / (self.state['attempt'] + '.json'))
        if isinstance(result.get('result'), dict):
            # Keep routine status bounded; full command timings remain durable
            # in state and the attempt record for targeted diagnosis.
            result['result'] = {k: v for k, v in result['result'].items() if k != 'timings'}
        return result

    def queue_status(self, state, description):
        payload = {'environment': 'production-nas', 'state': state, 'description': description[:140],
                   'environment_url': 'https://nas.example.invalid:9443',
                   'log_url': 'https://github.com/' + REPOSITORY + '/actions/runs/' + str(self.state['ci_run'])}
        self.state['outbox'].append(payload)
        self.save()
        if not self.state.get('report_error'):
            self.flush()

    def flush(self):
        if self.github is None or not self.state.get('target') or not self.state.get('outbox'):
            return
        try:
            if not self.state.get('deployment_id'):
                value = self.github.request('/deployments', {'ref': self.state['target'], 'auto_merge': False,
                    'required_contexts': [], 'environment': 'production-nas', 'production_environment': True,
                    'description': 'NAS pull deployment', 'payload': {'attempt': self.state['attempt']}})
                self.state['deployment_id'] = value['id']
                self.save()
            while self.state['outbox']:
                self.github.request('/deployments/' + str(self.state['deployment_id']) + '/statuses', self.state['outbox'][0])
                self.state['outbox'].pop(0)
                self.save()
            self.state.pop('report_error', None)
            self.save()
        except (TransportError, ConfigurationError) as error:
            self.state['report_error'] = str(error)
            self.save()

    def event(self, phase, **details):
        if self.state.get('phase') == phase:
            return
        now = time.time()
        previous = self.state.get('phase')
        durations = self.state.setdefault('phase_seconds', {})
        durations[previous] = round(durations.get(previous, 0) + now - self.state.get('updated_at', now), 3)
        self.state['phase'] = phase
        self.state['updated_at'] = now
        event = dict(details, phase=phase, time=now)
        with (self.home / 'attempts' / (self.state['attempt'] + '.jsonl')).open('a') as stream:
            stream.write(json.dumps(event, sort_keys=True) + '\n')
        self.save()
        self.queue_status('in_progress', phase + ' ' + self.state['target'][:12])

    def run_once(self):
        with locked(self.home / 'runner.lock') as acquired:
            if not acquired:
                return {'waiting': 'runner_lock'}
            # CLI pause/retry shares this lock, so refresh after acquiring it.
            self.state = read_json(self.path, self.state)
            if self.github is None:
                return {'waiting': 'github_credential', **self.status()}
            self.flush()
            pending = self.state.get('active', False)
            if self.state.get('outbox') and not pending:
                return {'waiting': 'github_status_delivery', **self.status()}
            if not pending and (self.state.get('paused') or (self.home / 'paused').exists()):
                return self.status()
            if time.time() < self.state.get('retry_at', 0):
                return self.status()
            with locked(self.root / '.deploy.lock') as deploying:
                if not deploying:
                    return {'waiting': 'deployment_lock'}
                try:
                    return self._run(pending)
                except TransportError as error:
                    failures = self.state.get('network_failures', 0) + 1
                    self.state.update(network_failures=failures, retry_at=time.time() + (120, 300, 900)[min(failures - 1, 2)], error=str(error))
                    self.save()
                    return self.status()
                except Exception as error:  # noqa: BLE001 - persist unexpected failures and stop CD
                    # Deployment handles coordinated recovery. Stop accepting later commits.
                    attempted = self.state.get('active', False)
                    self.state.update(paused=True, active=False, failed_sha=self.state.get('target') if attempted else None, phase='failed', error=type(error).__name__ + ': ' + str(error)[:300])
                    self.save()
                    if attempted and self.state.get('target'):
                        self.queue_status('failure', self.state['error'])
                    return self.status()

    def _run(self, pending):
        if not pending:
            if (self.root / '.deploy-journal.json').exists() or (self.root / '.cd-install.json').exists():
                return {'waiting': 'existing_deployment_recovery'}
            live = read_json(self.root / '.release-state.json', {}).get('release')
            selected = discover(self.github, live, self.state.get('retry_sha'))
            if not selected:
                return self.status()
            manifest = selected['manifest']
            requested = self.state.get('retry_sha')
            if requested and manifest['commit'] != requested:
                raise ValueError('Retry SHA is not the latest eligible forward release')
            if self.state.get('failed_sha') == manifest['commit'] and not requested:
                self.state['paused'] = True
                self.save()
                return self.status()
            stage = self.home / 'releases' / manifest['commit']
            unpack(self.github.request(selected['asset'], binary=True), manifest, stage)
            attempt = manifest['commit'][:12] + '-' + str(time.time_ns())
            self.state.update(target=manifest['commit'], ci_run=manifest['ci_run'], attempt=attempt,
                              stage=str(stage), active=True, phase='prepared', deployment_id=None,
                              outbox=[], started_at=time.time(), updated_at=time.time(), phase_seconds={}, network_failures=0, retry_at=0)
            self.state.pop('retry_sha', None)
            self.state.pop('error', None)
            (self.home / 'attempts').mkdir(exist_ok=True, mode=0o700)
            self.save()
            self.queue_status('queued', 'Verified release prepared')
        stage = Path(self.state['stage'])
        manifest = read_json(stage / 'release.json')
        live = read_json(self.root / '.release-state.json', {}).get('release')
        installation = read_json(self.root / '.cd-install.json')
        if installation:
            # Recovery must work while GitHub is offline. Its locked local journal
            # binds both the original live release and the pinned target.
            forward = installation['release'] == manifest['commit'] and live in (manifest['commit'], installation.get('previous_release'))
        else:
            forward = not live or self.github.ancestor(live, manifest['commit'])
        if not forward:
            raise ValueError('A newer manual release is live; refusing an older interrupted attempt')
        self.event('install')
        result = install(self.root, stage, self.event)
        self.event('record')
        manifest = read_json(stage / 'release.json')
        result.update(images=manifest['images'], ci_url='https://github.com/' + REPOSITORY + '/actions/runs/' + str(manifest['ci_run']),
                      phase_seconds=self.state.get('phase_seconds', {}), started_at=self.state['started_at'], completed_at=time.time())
        write_json(self.home / 'attempts' / (self.state['attempt'] + '.json'), result)
        upgrade_runtime(stage, self.home)
        self.state.update(active=False, phase='success', completed_at=time.time(), result=result, network_failures=0, retry_at=0)
        self.state.pop('failed_sha', None)
        self.state.pop('error', None)
        self.save()
        self.queue_status('success', 'Verified images, routing, workers and 300-second observation')
        return self.status()

    def cleanup(self):
        if self.github is None or self.state.get('paused') or (self.home / 'paused').exists():
            return {'maintenance': 'paused'}
        # Called every few minutes, but maintenance runs once at/after 03:30 Chicago.
        # NAS Python 3.8 uses libc zoneinfo; do not change the host timezone.
        old_tz = os.environ.get('TZ')
        os.environ['TZ'] = 'America/Chicago'
        time.tzset()
        local = time.localtime()
        day = time.strftime('%Y-%m-%d', local)
        if old_tz is None:
            os.environ.pop('TZ', None)
        else:
            os.environ['TZ'] = old_tz
        time.tzset()
        if local.tm_hour != 3 or local.tm_min < 30 or self.state.get('cleanup_day') == day:
            return {'maintenance': 'not_due'}
        with locked(self.home / 'runner.lock') as acquired:
            if not acquired:
                return {'waiting': 'runner_lock'}
            with locked(self.root / '.deploy.lock') as deploying:
                if not deploying or (self.root / '.deploy-journal.json').exists() or (self.root / '.cd-install.json').exists():
                    return {'waiting': 'deployment_lock_or_journal'}
                state = read_json(self.root / '.release-state.json', {})
                if not state.get('manifest'):
                    return {'maintenance': 'no_verified_state'}
                # Load the currently installed version, not the next downloaded release.
                deploy = load_deployer(self.root.parent)
                release = deploy.Deployment(self.root, state['manifest'])
                keep = {state['api'], state['web'], *state.get('previous', {}).values()}
                result = {'removed': release.prune(keep), 'time': time.time()}
                self.state = read_json(self.path, self.state)
                self.state.update(cleanup_day=day, cleanup=result)
                self.save()
                return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True, type=Path)
    parser.add_argument('command', choices=('check', 'run-once', 'status', 'pause', 'resume', 'retry', 'cleanup'))
    parser.add_argument('sha', nargs='?')
    parser.add_argument('--json', action='store_true')
    parser.add_argument('--quiet', action='store_true')
    args = parser.parse_args()
    config = read_json(args.config)
    runner = Runner(config)
    if args.command == 'status':
        result = runner.status()
    elif args.command == 'check':
        if runner.github is None:
            raise SystemExit('Install the dedicated GitHub credential before check')
        selected = discover(runner.github, runner.status()['live_release'])
        result = {'candidate': selected['manifest'] if selected else None, 'status': runner.status()}
    elif args.command == 'run-once':
        result = runner.run_once()
    elif args.command == 'cleanup':
        result = runner.cleanup()
    elif args.command == 'pause':
        (runner.home / 'paused').touch(mode=0o600)
        result = runner.status()
    else:
        with locked(runner.home / 'runner.lock') as acquired:
            if not acquired:
                raise SystemExit('Active deployment owns control lock; pause flag can be set after completion')
            runner.state = read_json(runner.path, runner.state)
            if args.command == 'retry':
                if not args.sha or not re.fullmatch('[0-9a-f]{40}', args.sha) or args.sha != runner.state.get('target'):
                    raise SystemExit('retry requires the full SHA of the failed attempt')
                live = read_json(runner.root / '.release-state.json', {}).get('release')
                if (runner.root / '.cd-install.json').exists() or live == args.sha:
                    runner.state['active'] = True
                else:
                    runner.state['retry_sha'] = args.sha
                runner.state.update(paused=False, retry_at=0)
            else:
                runner.state['paused'] = args.command == 'pause'
            (runner.home / 'paused').unlink(missing_ok=True)
            runner.save()
            result = runner.status()
    if not args.quiet:
        print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == '__main__':
    main()
