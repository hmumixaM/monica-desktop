#!/usr/bin/env python3
"""Load reviewed GitHub Actions artifacts on NAS, reusing identical image bytes.

This does not build images, change running containers, or read NAS credentials
from disk. Run only after the corresponding Actions run has succeeded.
"""
import argparse
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import tarfile
from pathlib import Path

DOCKER = '/var/packages/ContainerManager/target/usr/bin/docker'


def artifact_image(path, expected_tag):
    with tarfile.open(path) as archive:
        manifest = json.load(archive.extractfile('manifest.json'))
        if len(manifest) != 1 or expected_tag not in manifest[0].get('RepoTags', []):
            raise ValueError(f'{path.name}: artifact does not contain {expected_tag}')
        config = archive.extractfile(manifest[0]['Config']).read()
        image_id = 'sha256:' + hashlib.sha256(config).hexdigest()
        if Path(manifest[0]['Config']).name.removesuffix('.json') != image_id[7:]:
            raise ValueError(f'{path.name}: image configuration hash is invalid')
        return image_id


def remote(host, args, password, *, capture=False):
    return subprocess.run(['ssh', host, shlex.join(['sudo', '-S', '-p', '', DOCKER, *args])],
                          input=password, stdout=subprocess.PIPE if capture else None,
                          stderr=subprocess.PIPE if capture else None, check=False)


def load(host, path, password):
    process = subprocess.Popen(['ssh', host, shlex.join(['sudo', '-S', '-p', '', DOCKER, 'load'])],
                               stdin=subprocess.PIPE)
    try:
        process.stdin.write(password)
        with path.open('rb') as source:
            shutil.copyfileobj(source, process.stdin, length=1024 * 1024)
    finally:
        process.stdin.close()
    if process.wait():
        raise RuntimeError(f'{path.name}: NAS image load failed')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('release', help='Full commit SHA from the successful Actions run')
    parser.add_argument('artifacts', type=Path, help='Directory containing image-api, image-web, image-worker')
    parser.add_argument('--host', default='nas')
    args = parser.parse_args()
    if not re.fullmatch(r'[0-9a-f]{40}', args.release):
        parser.error('Expected a full commit SHA')
    secret = os.environ.get('NAS_PASSWORD')
    if not secret:
        parser.error('NAS_PASSWORD must be provided in the environment')
    password = (secret + '\n').encode()
    # Validate all three local artifacts before making any NAS changes.
    images = []
    for service in ('api', 'web', 'worker'):
        path = args.artifacts / f'image-{service}' / f'monica-next-{service}.tar'
        tag = f'monica-next-{service}:{args.release}'
        images.append((path, tag, artifact_image(path, tag)))
    for path, tag, image_id in images:
        existing = remote(args.host, ['image', 'inspect', image_id, '--format', '{{.Id}}'], password, capture=True)
        if existing.returncode == 0 and existing.stdout.decode().strip() == image_id:
            if remote(args.host, ['image', 'tag', image_id, tag], password).returncode:
                raise RuntimeError(f'{tag}: could not tag the existing image')
            print(f'{tag}: identical image already present; skipped artifact transfer', flush=True)
        else:
            load(args.host, path, password)
        verified = remote(args.host, ['image', 'inspect', tag, '--format', '{{.Id}}'], password, capture=True)
        if verified.returncode or verified.stdout.decode().strip() != image_id:
            raise RuntimeError(f'{tag}: NAS image differs from the Actions artifact')
        print(f'{tag}: verified {image_id}', flush=True)


if __name__ == '__main__':
    main()
