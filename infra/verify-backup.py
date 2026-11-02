#!/usr/bin/env python3
"""Verify backup bytes and optionally all restored CRM media references."""
import argparse
import csv
import hashlib
import json
import tarfile
from pathlib import Path


def digest(stream):
    value = hashlib.sha256()
    for chunk in iter(lambda: stream.read(1024*1024), b''):
        value.update(chunk)
    return value.hexdigest()


def verify(root, media_csv=None):
    document = json.loads((root/'manifest.json').read_text())
    if document.get('version') != 1 or not (root/'COMPLETE').is_file():
        raise ValueError('Incomplete backup set')
    for name, expected in document['files'].items():
        if Path(name).name != name:
            raise ValueError('Unsafe backup filename')
        path = root/name
        with path.open('rb') as stream:
            if path.stat().st_size != expected['bytes'] or digest(stream) != expected['sha256']:
                raise ValueError('Backup checksum mismatch: '+name)
    if media_csv is None:
        return {'checksums': len(document['files'])}
    expected = {}
    with media_csv.open() as stream:
        for sha, path in csv.reader(stream):
            if not path.startswith('/data/media/'):
                raise ValueError('Unmapped media root: '+path)
            expected['media/'+path.removeprefix('/data/media/')] = sha
    if not (root/'media.tar').is_file():
        raise ValueError('External media snapshot needs a separate isolated restore; database restore alone is insufficient')
    verified = set()
    with tarfile.open(root/'media.tar', mode='r|') as archive:
        for entry in archive:
            if entry.name in expected:
                if not entry.isfile() or digest(archive.extractfile(entry)) != expected[entry.name]:
                    raise ValueError('Restored media checksum mismatch: '+entry.name)
                verified.add(entry.name)
    if verified != set(expected):
        raise ValueError(f'Missing {len(set(expected)-verified)} referenced media files')
    return {'checksums': len(document['files']), 'media_references': len(verified)}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('backup', type=Path)
    parser.add_argument('--media-csv', type=Path)
    args = parser.parse_args()
    print(json.dumps(verify(args.backup, args.media_csv)))
