#!/usr/bin/env python3
"""Refuse publication into an existing public GHCR package."""
import argparse
import json
import os
import urllib.error
import urllib.request


def check(role, allow_missing=False):
    owner = os.environ['GITHUB_REPOSITORY_OWNER']
    request = urllib.request.Request(
        f'https://api.github.com/users/{owner}/packages/container/monica-next-{role}',
        headers={'Authorization': 'Bearer '+os.environ['GH_TOKEN'], 'Accept': 'application/vnd.github+json',
                 'X-GitHub-Api-Version': '2022-11-28'})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            package = json.load(response)
    except urllib.error.HTTPError as error:
        if error.code == 404 and allow_missing:
            return  # First publication defaults to private visibility.
        raise
    if package.get('visibility') != 'private':
        raise RuntimeError('Expected a private GHCR package; publication refused')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('role', choices=['api', 'worker', 'web'])
    parser.add_argument('--allow-missing', action='store_true')
    args = parser.parse_args()
    check(args.role, args.allow_missing)
