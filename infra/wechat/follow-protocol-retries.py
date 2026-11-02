"""Host-side one-shot follower: link current successes, then final completed retry."""
import json
import subprocess
import time
from pathlib import Path

ROOT=Path('/path/to/nas/home/docker/chatlog-alpha')
STAGE=Path('/volume1/docker/monica-next/data/staging/protocol-retry-20260920')
DOCKER='/var/packages/ContainerManager/target/usr/bin/docker'
STAGE.mkdir(parents=True,exist_ok=True)
first=True
while True:
    report=json.loads((ROOT/'protocol-retry-20260920.json').read_text())
    if first or report['state']=='complete':
        (STAGE/'downloads.json').write_text(json.dumps(report))
        names=subprocess.check_output([DOCKER,'ps','--format','{{.Names}}'],text=True).splitlines()
        apis=[n for n in names if n == 'monica-wechat-media-backfill-20260920']
        if len(apis)!=1:
            time.sleep(30); continue
        result=subprocess.run([DOCKER,'exec','-i',apis[0],'/app/.venv/bin/python','-'],
                              input=(ROOT/'link-protocol-retries.py').read_text(),text=True,check=False)
        if result.returncode:
            print('Import failed; retrying idempotently in 60 seconds',flush=True)
            time.sleep(60); continue
        first=False
        if report['state']=='complete':
            print('Downloads finished; Monica linking finished. See downloads.json and links.json.',flush=True)
            break
    time.sleep(30)
