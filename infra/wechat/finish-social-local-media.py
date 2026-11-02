"""Fill exact missing LinkedIn owners after the current local import completes."""
import json
import subprocess
import time
from pathlib import Path

from app.archive_linkedin import archive
from app.db import SessionLocal
from app.full_migration import load_linkedin
from sqlalchemy import text

ROOT = Path('/data/staging/wechat-media-backfill-20260920')
while True:
    path = ROOT / 'social-local-import.json'
    try:
        done = json.loads(path.read_text()).get('state') == 'complete'
    except (FileNotFoundError, json.JSONDecodeError):
        done = False
    if done:
        break
    time.sleep(30)

snapshot = json.loads((ROOT / 'linkedin-media-snapshot.json').read_text())
with SessionLocal() as db:
    accounts = set(db.execute(text("SELECT external_id FROM source_accounts WHERE provider='linkedin'")).scalars())
    missing = [row for row in snapshot['profiles']
               if str(row.get('provider_user_id') or row.get('profile_url') or '') not in accounts]
    db.commit()
    if missing:
        load_linkedin(db, Path('/data/raw/linkedin/linkedin_enrichment.incoming.json'), {},
                      export_snapshot={'profiles': missing}, incremental=True)
        db.commit()
subprocess.run(['/app/.venv/bin/python', str(ROOT / 'create-archive-profiles.py'),
                '--input', str(ROOT / 'linkedin-media-snapshot.json'),
                '--report', str(ROOT / 'linkedin-new-owner-profiles.json'), '--apply'], check=True)
with SessionLocal() as db:
    result = archive(db, snapshot, Path('/data/raw/linkedin'), Path('/data/media'))
    result['new_owner_records'] = len(missing)
    (ROOT / 'linkedin-final-media.json').write_text(json.dumps(result))
    print(json.dumps(result), flush=True)
    if result['unmapped_accounts'] or result['unmapped_posts'] or result['missing_files']:
        raise RuntimeError('LinkedIn local media still has unresolved ownership or file gaps')
