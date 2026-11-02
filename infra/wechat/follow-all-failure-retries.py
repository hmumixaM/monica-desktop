"""Import this retry's successful local files after its downloader completes."""
import json
import os
import subprocess
import time
from pathlib import Path

HOST = Path('/path/to/nas/home/docker/chatlog-alpha')
INFRA = Path('/path/to/nas/home/docker/monica-next/infra')
STAGE = Path('/volume1/docker/monica-next/data/staging/all-failure-retry-20260920')
CONTAINER_STAGE = '/data/staging/all-failure-retry-20260920'
COMPOSE = '/var/packages/ContainerManager/target/usr/bin/docker-compose'
STAGE.mkdir(parents=True, exist_ok=True)
while True:
    source = HOST / 'all-failure-retry-20260920.json'
    report = json.loads(source.read_text()) if source.exists() else {}
    if report.get('state') != 'complete':
        time.sleep(30)
        continue
    (STAGE / 'downloads.json').write_text(json.dumps(report))
    (STAGE / 'link.py').write_text((HOST / 'link-protocol-retries.py').read_text())
    release = json.loads((INFRA / '.release-state.json').read_text())['release']
    result = subprocess.run(
        [COMPOSE, 'run', '--rm', '--no-deps', '-T',
         '-e', 'PYTHONPATH=/app', '-e', 'MEDIA_RETRY_STAGE=' + CONTAINER_STAGE,
         'api', '/app/.venv/bin/python', CONTAINER_STAGE + '/link.py'],
        cwd=INFRA, env=dict(os.environ, IMAGE_TAG=release), check=False,
    )
    if result.returncode == 0:
        print('Retry download and Monica import finished; inspect links.json for gaps.', flush=True)
        break
    print('Import failed; retrying committed-safe work in 60 seconds.', flush=True)
    time.sleep(60)
