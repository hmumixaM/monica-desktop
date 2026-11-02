"""Publish the collector's complete local asset catalog before its CRM export."""
import argparse
import fcntl
import hashlib
import json
import re
import shlex
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from snapshot_publish import publish


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--payload',type=Path,required=True)
    parser.add_argument('--db',type=Path,required=True)
    parser.add_argument('--assets',type=Path,required=True)
    parser.add_argument('--host',default='linkedin-nas')
    parser.add_argument('--destination',default='/volume1/docker/monica-next/data/raw/linkedin')
    args=parser.parse_args()
    with args.payload.with_suffix('.publish.lock').open('w') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        with sqlite3.connect(str(args.db)) as db:
            db.row_factory=sqlite3.Row
            rows=[dict(r) for r in db.execute('SELECT source_url,content_hash,media_type,byte_count,local_path,captured_at FROM assets')]
        catalog=[];files={};missing=0
        for row in rows:
            name=row['local_path'];sha=row['content_hash'];path=args.assets/name
            if not re.fullmatch(r'[a-f0-9]{64}\.[a-z0-9]+',name) or not name.startswith(sha+'.'):
                raise ValueError('Unsafe local asset path')
            if path.is_symlink() or not path.is_file():missing+=1;continue
            if name not in files:
                with path.open('rb') as f:
                    digest=hashlib.sha256()
                    for chunk in iter(lambda:f.read(1024*1024),b''):digest.update(chunk)
                if digest.hexdigest()!=sha or path.stat().st_size!=row['byte_count']:
                    raise ValueError('Asset checksum mismatch')
                files[name]=path
            catalog.append(row)
        document=json.loads(args.payload.read_text());document['local_assets']=catalog
        document['local_asset_missing_files']=missing
        dest=shlex.quote(args.destination)
        # Only immutable, verified hashes are reused remotely. Missing bytes are resent.
        inventory=subprocess.check_output(['ssh',args.host,f'if test -L {dest}/CURRENT; then ls {dest}/CURRENT/assets; else mkdir -p {dest}/assets; ls {dest}/assets; fi'],text=True).splitlines()
        selected={name:path for name,path in files.items() if name not in inventory}
        with tempfile.TemporaryDirectory() as temp:
            payload=Path(temp)/'linkedin_enrichment.incoming.json';payload.write_text(json.dumps(document,ensure_ascii=False))
            publish(args.host, args.destination,
                    [(path, 'assets/'+name) for name,path in selected.items()]+[(payload,payload.name)])
        print(json.dumps({'asset_urls':len(catalog),'unique_files':len(files),'transferred_files':len(selected),'missing_files':missing,'profiles':len(document.get('profiles',[]))}),flush=True)


if __name__=='__main__':main()
