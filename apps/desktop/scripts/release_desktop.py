#!/usr/bin/env python3
"""Package a verified macOS app and publish immutable experimental releases."""
import argparse
import hashlib
import json
import os
import plistlib
import re
import subprocess
from pathlib import Path

LIMIT = 2 * 1024 ** 3


def digest(path):
    with path.open('rb') as source:
        return hashlib.file_digest(source, 'sha256').hexdigest()


def verify(folder, sha):
    if not re.fullmatch(r'[0-9a-f]{40}', sha):
        raise ValueError('Expected a complete source commit')
    manifest = json.loads((folder / 'release.json').read_text())
    if manifest['source_commit'] != sha or manifest['schema'] != 1:
        raise ValueError('Artifact source commit does not match this run')
    required = {f'Monica-{manifest["version"]}-macos-arm64-{sha[:12]}.zip',
                'runtime-verification.json', 'smoke-verification.json'}
    if {entry['name'] for entry in manifest['assets']} != required:
        raise ValueError('Incomplete application artifact set')
    expected = {'release.json', 'SHA256SUMS'}
    for entry in manifest['assets']:
        name = entry['name']
        if not name or Path(name).name != name or name in expected:
            raise ValueError('Unsafe or duplicate release asset')
        expected.add(name)
        path = folder / name
        if path.is_symlink() or not path.is_file() or path.stat().st_size != entry['bytes']:
            raise ValueError('Release asset missing or wrong size')
        if path.stat().st_size >= LIMIT or digest(path) != entry['sha256']:
            raise ValueError('Release asset checksum or size limit failed')
    if {p.name for p in folder.iterdir()} != expected:
        raise ValueError('Unexpected files in release directory')
    checksums = ''.join(f'{digest(folder / name)}  {name}\n' for name in sorted(expected - {'SHA256SUMS'}))
    if (folder / 'SHA256SUMS').read_text() != checksums:
        raise ValueError('Release checksum manifest failed')
    return manifest


def package(app, output, reports, sha):
    subprocess.run(['codesign', '--verify', '--deep', '--strict', str(app)], check=True)
    plist = plistlib.loads((app / 'Contents/Info.plist').read_bytes())
    if plist['CFBundleIdentifier'] != 'org.monica.desktop':
        raise ValueError('Unexpected application identity')
    version = plist['CFBundleShortVersionString']
    if not re.fullmatch(r'\d+\.\d+\.\d+', version):
        raise ValueError('Expected a numeric application version')
    output.mkdir(parents=True, exist_ok=False)
    archive = output / f'Monica-{version}-macos-arm64-{sha[:12]}.zip'
    subprocess.run(['ditto', '-c', '-k', '--sequesterRsrc', '--keepParent', str(app), str(archive)], check=True)
    for name, source in [('runtime-verification.json', reports / 'runtime.json'), ('smoke-verification.json', reports / 'smoke.json')]:
        # Copy only the two public reports, never runtime settings or logs.
        value = json.loads(source.read_text())
        if name.startswith('runtime') and value.get('face_models') != 'external':
            raise ValueError('Public release must use external face weights')
        if name.startswith('smoke') and (len(value.get('passed', [])) < 8 or value.get('provider_requests') is not False):
            raise ValueError('Complete isolated runtime verification required')
        (output / name).write_text(json.dumps(value, indent=2) + '\n')
    assets = [{'name': p.name, 'bytes': p.stat().st_size, 'sha256': digest(p)} for p in sorted(output.iterdir())]
    metadata = {'schema': 1, 'product': 'Monica', 'version': version, 'architecture': 'arm64',
                'minimum_macos': '26.0', 'source_commit': sha, 'repository': os.environ.get('GITHUB_REPOSITORY'),
                'run_id': os.environ.get('GITHUB_RUN_ID'), 'signing': 'ad-hoc', 'notarized': False,
                'face_models': 'external', 'assets': assets}
    (output / 'release.json').write_text(json.dumps(metadata, indent=2) + '\n')
    (output / 'SHA256SUMS').write_text(''.join(f'{digest(p)}  {p.name}\n' for p in sorted(output.iterdir())))
    verify(output, sha)


def gh(*args, **kwargs):
    return subprocess.run(['gh', *args], check=True, capture_output=True, text=True, **kwargs).stdout


def publish(folder, sha, repository, ref):
    manifest = verify(folder, sha)
    if ref == 'refs/heads/main':
        head = gh('api', f'repos/{repository}/git/ref/heads/main', '--jq', '.object.sha').strip()
        if head != sha:
            print('A newer main commit exists; skipping stale preview')
            return
        tag = 'preview-' + sha[:12]
    elif ref == 'refs/tags/v' + manifest['version']:
        tag = ref.removeprefix('refs/tags/')
    else:
        raise ValueError('Only main or an exact version tag may publish')
    existing = subprocess.run(['gh', 'release', 'view', tag, '--repo', repository, '--json', 'isDraft'], check=False, capture_output=True, text=True)
    if existing.returncode == 0:
        if not json.loads(existing.stdout)['isDraft']:
            previous = json.loads(gh('release', 'download', tag, '--repo', repository, '--pattern', 'release.json', '--output', '-'))
            if previous['source_commit'] != sha:
                raise ValueError('Published release belongs to another source commit')
            # Never replace already published binaries on a retry.
            print('Release is already published: ' + tag)
            return
    else:
        notes = (f'Experimental Monica Desktop {manifest["version"]}, Apple Silicon, macOS 26+.\n\n'
                 f'Source: https://github.com/{repository}/commit/{sha}\n\n'
                 'Download the ZIP, extract Monica.app, and move it to Applications. '
                 'This build uses local ad-hoc signing and has not been notarized by Apple.\n\n'
                 'WeChat, Instagram and LinkedIn collectors are included. Face model weights are supplied locally by the user. '
                 f'See https://github.com/{repository}/blob/{sha}/apps/desktop/README.md for setup.\n\n'
                 'Runtime verification and isolated startup/recovery tests passed. SHA256SUMS covers all assets.\n')
        gh('release', 'create', tag, '--repo', repository, '--target', sha, '--draft', '--prerelease',
           '--title', f'Monica {manifest["version"]} · {tag}', '--notes-file', '-', input=notes)
    gh('release', 'upload', tag, '--repo', repository, '--clobber', *[str(p) for p in sorted(folder.iterdir())])
    # Uploading a partial set leaves a draft, never a public broken release.
    assets = json.loads(gh('release', 'view', tag, '--repo', repository, '--json', 'assets'))['assets']
    if {a['name']: a['size'] for a in assets} != {p.name: p.stat().st_size for p in folder.iterdir()}:
        raise ValueError('Remote release assets are incomplete')
    gh('release', 'edit', tag, '--repo', repository, '--draft=false', '--prerelease', '--latest=false')
    print(f'https://github.com/{repository}/releases/tag/{tag}')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('package', 'verify', 'publish'))
    parser.add_argument('--sha', required=True)
    parser.add_argument('--directory', required=True, type=Path)
    parser.add_argument('--app', type=Path)
    parser.add_argument('--reports', type=Path)
    args = parser.parse_args()
    if args.command == 'package':
        package(args.app, args.directory, args.reports, args.sha)
    elif args.command == 'verify':
        print(json.dumps(verify(args.directory, args.sha)))
    else:
        publish(args.directory, args.sha, os.environ['GITHUB_REPOSITORY'], os.environ['GITHUB_REF'])


if __name__ == '__main__':
    main()
