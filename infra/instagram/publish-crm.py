"""Copy changed, already-collected Instagram files to NAS over the existing SSH key.

No provider requests, deletes, credential files, or collector scheduling changes.
Only publish after a successful local archive cycle; unchanged files are skipped.
"""
import argparse
import json
import sys
import fcntl
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from snapshot_publish import publish

parser=argparse.ArgumentParser(description=__doc__)
parser.add_argument('--root',type=Path,required=True)
parser.add_argument('--host',default='linkedin-nas')
parser.add_argument('--destination',default='/volume1/docker/monica-next/data/staging/instagram-current')
parser.add_argument('--dry-run',action='store_true')
args=parser.parse_args()
root=args.root/'data/profiles';state=args.root/'state/crm-published-files.json'
state.parent.mkdir(parents=True,exist_ok=True)
lock=state.with_suffix('.lock').open('a')
fcntl.flock(lock,fcntl.LOCK_EX | fcntl.LOCK_NB)
before=json.loads(state.read_text()) if state.exists() else {}
after={};changed=[]
for path in sorted(root.glob('*/*')):
    if path.is_symlink() or not path.is_file() or path.suffix.lower() not in {'.json','.jpg','.jpeg','.png','.webp','.mp4'}:continue
    key=path.relative_to(root).as_posix();info=path.stat();value=[info.st_size,info.st_mtime_ns];after[key]=value
    if before.get(key)!=value:changed.append((path,key))
if changed and not args.dry_run:
    publish(args.host,args.destination,[(path,'profiles/'+key) for path,key in changed])
    temporary=state.with_suffix('.tmp');temporary.write_text(json.dumps(after,sort_keys=True));temporary.chmod(0o600);temporary.replace(state)
print(json.dumps({'changed_files':len(changed),'source_files':len(after),'published':not args.dry_run}))
