#!/usr/bin/env python3
"""Resolve complete build inputs before spending time building unchanged images."""
import json
import os
import subprocess
import sys

from release_common import (
    INPUT_PATHS,
    HTTP_ONLY_PATHS,
    ROLES,
    WORKFLOW,
    tree_digest,
    valid_image,
    validate_v2,
)


def run(*args):
    return subprocess.check_output(args, timeout=120).decode().strip()


def inputs(sha):
    return {role: tree_digest(sha, paths, HTTP_ONLY_PATHS if role == 'worker' else ())
            for role, paths in INPUT_PATHS.items()}


def previous_manifest(sha):
    previous = {}
    # Only ancestor releases can supply a build cache; publication order is irrelevant.
    for entry in json.loads(run('gh', 'release', 'list', '--limit', '100', '--json', 'tagName,isDraft')):
        tag = entry['tagName']
        if not tag.startswith('verified-') or entry['isDraft'] or tag == 'verified-' + sha:
            continue
        if subprocess.run(['git', 'merge-base', '--is-ancestor', tag[9:], sha], capture_output=True, check=False).returncode:
            continue
        candidate = json.loads(run('gh', 'api', 'repos/' + os.environ['GITHUB_REPOSITORY'] + '/releases/tags/' + tag))
        assets = [a for a in candidate['assets'] if a['name'] == 'release.json']
        if not assets:
            continue
        manifest = json.loads(run('gh', 'api', assets[0]['url'], '-H', 'Accept: application/octet-stream'))
        if manifest.get('version') != 2 or manifest.get('commit') != tag[9:]:
            continue
        validate_v2(manifest)
        ci = json.loads(run('gh', 'api', 'repos/' + os.environ['GITHUB_REPOSITORY'] + '/actions/runs/' + str(manifest['ci_run'])))
        if (ci['head_sha'] != tag[9:] or ci['path'] != WORKFLOW or ci['event'] != 'push'
                or ci['head_branch'] != 'main' or ci['conclusion'] != 'success'):
            continue
        previous = manifest
        break
    return previous


def plan(sha, is_pr=False):
    hashes = inputs(sha)
    previous = {}
    if not is_pr:
        try:
            previous = previous_manifest(sha)
        except (subprocess.SubprocessError, ValueError, KeyError):
            print('Verified build cache unavailable; rebuilding all roles.', file=sys.stderr)
    return {role: {'build': is_pr or previous.get('build_inputs', {}).get(role) != hashes[role]
                          or not valid_image(role, previous.get('images', {}).get(role)),
                   'input': hashes[role], 'image': previous.get('images', {}).get(role)} for role in ROLES}


if __name__ == '__main__':
    result = plan(os.environ['GITHUB_SHA'], os.environ.get('GITHUB_EVENT_NAME') == 'pull_request')
    encoded = json.dumps(result, separators=(',', ':'))
    print(encoded)
    with open(os.environ['GITHUB_OUTPUT'], 'a') as output:
        output.write('plan=' + encoded + '\n')
