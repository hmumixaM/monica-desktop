#!/usr/bin/env python3
"""Build Node without libuv's private macOS LaunchServices process-title APIs.

The implementation is replaced in source with libuv's public thread-name API.
No binary strings are hidden or renamed. Run with Python 3.12 on the build Mac.
"""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import tarfile
import urllib.request

VERSION = '22.21.1'
ROOT = Path(__file__).resolve().parents[3]


def digest(path):
    with path.open('rb') as handle:
        return hashlib.file_digest(handle, 'sha256').hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--jobs', type=int, default=6)
    args = parser.parse_args()
    cache = ROOT / 'apps/desktop/.build/appstore-node'
    cache.mkdir(parents=True, exist_ok=True)
    name = f'node-v{VERSION}.tar.xz'
    archive = cache / name
    base = f'https://nodejs.org/dist/v{VERSION}/'
    checksums = urllib.request.urlopen(base + 'SHASUMS256.txt', timeout=60).read().decode()
    expected = next(line.split()[0] for line in checksums.splitlines() if line.split()[-1] == name)
    if not archive.exists():
        with urllib.request.urlopen(base + name, timeout=120) as response, archive.open('wb') as out:
            shutil.copyfileobj(response, out)
    if digest(archive) != expected:
        raise ValueError('Official Node source checksum mismatch')
    source = cache / f'node-v{VERSION}'
    if not source.exists():
        with tarfile.open(archive) as package:
            package.extractall(cache, filter='data')
    title = source / 'deps/uv/src/unix/darwin-proctitle.c'
    original = title.read_text()
    marker = '/* Monica App Store: public thread naming only. */'
    if marker not in original:
        if '_LSSetApplicationLaunchServicesServerConnectionStatus' not in original:
            raise ValueError('Unexpected libuv source layout')
        license_text = original[:original.index('#include "uv.h"')]
        title.write_text(license_text + '#include "uv.h"\n#include "internal.h"\n\n' + marker + '\n'
                         'int uv__set_process_title(const char* title) {\n'
                         '  return uv__thread_setname(title);\n}\n')
    subprocess.run(['./configure', '--without-npm', '--without-corepack', '--without-inspector',
                    '--without-node-snapshot'], cwd=source, check=True)
    subprocess.run(['make', f'-j{args.jobs}'], cwd=source, check=True)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source / 'out/Release/node', args.output)
    subprocess.run(['strip', '-S', str(args.output)], check=True)
    subprocess.run(['codesign', '--force', '--sign', '-', str(args.output)], check=True)
    report = {'version': VERSION, 'source': base + name, 'source_sha256': expected,
              'process_title_source_sha256': digest(title), 'node_sha256': digest(args.output),
              'private_launchservices_process_title_removed': True}
    args.output.with_suffix('.json').write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report), flush=True)


if __name__ == '__main__':
    main()
