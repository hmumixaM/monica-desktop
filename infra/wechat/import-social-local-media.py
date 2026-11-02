"""Reconcile already collected social archives, then attach locally downloaded files."""
import json
from pathlib import Path

from app.archive_linkedin import archive
from app.archive_media import instagram_archive
from app.db import SessionLocal
from app.reconcile_sources import instagram

ROOT=Path('/data/staging/wechat-media-backfill-20260920')
report={}
with SessionLocal() as db:
    path=Path('/data/raw/linkedin/linkedin_enrichment.incoming.json')
    # Snapshot only the manifest; asset names are immutable content hashes.
    saved=ROOT/'linkedin-media-snapshot.json'
    snapshot=json.loads((saved if saved.exists() else path).read_text())
    if not saved.exists():saved.write_text(json.dumps(snapshot))
    report['linkedin']=archive(db,snapshot,path.parent,Path('/data/media'))
    (ROOT/'social-local-import.json').write_text(json.dumps(report));print(json.dumps(report),flush=True)
    root=Path('/data/staging/instagram-current/profiles')
    report['instagram_posts']=instagram(db,root,apply=True)
    (ROOT/'social-local-import.json').write_text(json.dumps(report));print(json.dumps(report),flush=True)
    report['instagram_media']=instagram_archive(db,root,Path('/data/media'))
    report['state']='complete';(ROOT/'social-local-import.json').write_text(json.dumps(report));print(json.dumps(report),flush=True)
