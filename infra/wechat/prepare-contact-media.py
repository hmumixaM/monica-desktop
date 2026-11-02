"""Build a frozen contact-media manifest from readonly Chatlog contact data."""
import importlib.util
import json
import sqlite3
import sys
from pathlib import Path

root=Path('/volume1/docker/monica-next/data/staging/wechat-media-backfill-20260920')
spec=importlib.util.spec_from_file_location('contact_prefetch',root/'prefetch-sns-images.py')
m=importlib.util.module_from_spec(spec);sys.modules[spec.name]=m;spec.loader.exec_module(m)
p=root/'contact.db'
if not p.exists():
    with sqlite3.connect('file:/path/to/nas/home/docker/chatlog/work/db_storage/contact/contact.db?mode=ro',uri=True) as src, sqlite3.connect(p) as out:
        src.backup(out)
resources,refs=m.read_avatars(p)
extra,more=m.read_supplemental(p)
(root/'contact-media.json').write_text(json.dumps({'references':refs+more},ensure_ascii=False))
print(json.dumps({'references':len(refs+more),'supplemental':len(more)}),flush=True)
