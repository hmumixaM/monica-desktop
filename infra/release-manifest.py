#!/usr/bin/env python3
"""Publish a complete immutable deployment bundle after all required CI jobs pass."""
import io
import json
import os
import subprocess
import tarfile
from pathlib import Path

from release_common import tree_digest, HTTP_ONLY_PATHS, INPUT_PATHS, AUDIT_PATHS, REPOSITORY, ROLES, WORKFLOW, digest, validate_v2


def run(*args):
    return subprocess.check_output(args, timeout=120).decode().strip()


def bundle(sha):
    archive = subprocess.check_output(['git', 'archive', '--format=tar', sha, 'infra', 'migrations'])
    output = io.BytesIO()
    files = {}
    with tarfile.open(fileobj=io.BytesIO(archive)) as source, tarfile.open(fileobj=output, mode='w') as target:
        for member in source.getmembers():
            if member.isdir() or any(p.startswith(('.', 'Caddyfile')) for p in member.name.split('/')):
                continue
            if not member.isfile():
                raise ValueError('Only regular deployment files are supported')
            content = source.extractfile(member).read()
            files[member.name] = digest(content)
            target.addfile(member, io.BytesIO(content))
    archive = output.getvalue()
    Path('deployment.tar').write_bytes(archive)
    return archive, files


def publish(result):
    tag = 'verified-' + result['commit']
    lookup = subprocess.run(['gh', 'release', 'view', tag, '--json', 'isDraft'], capture_output=True, text=True, check=False)
    if lookup.returncode == 0 and not json.loads(lookup.stdout)['isDraft']:
        # Re-running CI must not replace published bytes or their original CI provenance.
        existing = json.loads(run('gh', 'api', 'repos/' + REPOSITORY + '/releases/tags/' + tag))
        asset = next(a for a in existing['assets'] if a['name'] == 'release.json')
        old = json.loads(run('gh', 'api', asset['url'], '-H', 'Accept: application/octet-stream'))
        if {k: v for k, v in old.items() if k != 'ci_run'} != {k: v for k, v in result.items() if k != 'ci_run'}:
            raise RuntimeError('Published release differs; refusing overwrite')
        return
    if lookup.returncode:
        run('gh', 'release', 'create', tag, '--draft', '--target', result['commit'], '--title', tag,
            '--notes', 'Verified NAS deployment bundle. Images are pinned by immutable digest.')
    Path('release.json').write_text(json.dumps(result, indent=2, sort_keys=True) + '\n')
    run('gh', 'release', 'upload', tag, 'deployment.tar', 'release.json', '--clobber')
    run('gh', 'release', 'edit', tag, '--draft=false', '--latest=false')


def main():
    sha = os.environ['RELEASE_SHA']
    plan = json.loads(os.environ['BUILD_PLAN'])
    images = {}
    for role in ROLES:
        if not plan[role]['build']:
            images[role] = plan[role]['image']
        else:
            name = 'ghcr.io/hmumixam/monica-next-' + role
            manifest = json.loads(run('docker', 'buildx', 'imagetools', 'inspect', name + ':' + sha,
                                      '--format', '{{json .Manifest}}'))
            images[role] = name + '@' + manifest['digest']
    archive, files = bundle(sha)
    result = {'version': 2, 'repository': REPOSITORY, 'workflow': WORKFLOW, 'commit': sha,
              'ci_run': os.environ['CI_RUN_ID'], 'images': images,
              'background_inputs': {role: tree_digest(sha, INPUT_PATHS[role], HTTP_ONLY_PATHS) for role in ('api', 'worker')},
              'migration_input': tree_digest(sha, ('services/api/app/__init__.py', 'services/api/app/__main__.py',
                  'services/api/app/db.py', 'services/api/app/config.py', 'services/api/Dockerfile',
                  'services/api/uv.lock', 'services/api/pyproject.toml', 'infra/docker-compose.yml')),
              'build_inputs': {role: plan[role]['input'] for role in ROLES},
              'compose_sha256': files['infra/docker-compose.yml'],
              'bundle': {'name': 'deployment.tar', 'sha256': digest(archive)}, 'files': files,
              'migrations': {Path(p).name: value for p, value in files.items() if p.startswith('migrations/') and p.endswith('.sql')},
              'audit_inputs': {p: digest(subprocess.check_output(['git', 'show', sha + ':' + p])) for p in AUDIT_PATHS}}
    validate_v2(result)
    publish(result)


if __name__ == '__main__':
    main()
