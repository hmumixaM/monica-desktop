"""Keep independent media tasks in a dedicated container, away from API releases."""
import os
import subprocess
import time
from pathlib import Path

root=Path('/data/staging/wechat-media-backfill-20260920')
os.environ['PYTHONPATH']='/app'
scripts=['backfill-channels.py','import-contact-media.py','import-channels-covers.py']
processes={}
for name in scripts:
    log=(root/(name+'.log')).open('ab')
    processes[name]=(subprocess.Popen(['/app/.venv/bin/python',str(root/name)],stdout=log,stderr=log),log)
while processes:
    for name,(process,log) in list(processes.items()):
        code=process.poll()
        if code is not None:
            (root/(name+'.exit')).write_text(str(code));log.close();del processes[name]
    time.sleep(10)
