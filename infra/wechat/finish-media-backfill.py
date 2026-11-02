"""Finish social archive imports even if the core backfill container exits first."""
import json
import os
import subprocess
import time
from pathlib import Path

ROOT = Path('/volume1/docker/monica-next/data/staging/wechat-media-backfill-20260920')
INFRA = Path('/path/to/nas/home/docker/monica-next/infra')
DOCKER = '/var/packages/ContainerManager/target/usr/bin/docker'
COMPOSE = '/var/packages/ContainerManager/target/usr/bin/docker-compose'
while True:
    result = subprocess.run([DOCKER, 'inspect', '--format', '{{.State.Running}}',
                             'monica-wechat-media-backfill-20260920'],
                            capture_output=True, text=True, check=False)
    if result.returncode:
        raise RuntimeError('Cannot inspect the original backfill container')
    if result.stdout.strip() == 'false':
        break
    time.sleep(30)

def read(name):
    path = ROOT / name
    return json.loads(path.read_text()) if path.exists() else {}

scripts = []
if read('social-local-import.json').get('state') != 'complete':
    scripts.append('import-social-local-media.py')
final = read('linkedin-final-media.json')
if not final or any(final.get(key) for key in ('unmapped_accounts', 'unmapped_posts', 'missing_files')):
    scripts.append('finish-social-local-media.py')
for script in scripts:
    release = json.loads((INFRA / '.release-state.json').read_text())['release']
    result = subprocess.run([COMPOSE, 'run', '--rm', '--no-deps', '-T', '-e', 'PYTHONPATH=/app',
                             'api', '/app/.venv/bin/python',
                             '/data/staging/wechat-media-backfill-20260920/' + script],
                            cwd=INFRA, env=dict(os.environ, IMAGE_TAG=release), check=False)
    if result.returncode:
        (ROOT / 'finalizer-error.json').write_text(json.dumps({'script': script, 'exit': result.returncode}))
        raise RuntimeError('Social import finalizer failed; inspect its log')
(ROOT / 'finalizer-complete.json').write_text(json.dumps({'social_imports_finished': True}))
print('Social imports finished; per-job reports retain unresolved gaps.', flush=True)
