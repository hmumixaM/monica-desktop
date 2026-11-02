"""Import successful locally cached Channels posters, checkpointed by post/slot/file."""
import json
import time
from pathlib import Path

from app.archive_media import link, register_file
from app.db import SessionLocal
from app.remote_media import retain_post_media_batch
from sqlalchemy import text

ROOT=Path('/data/staging/wechat-media-backfill-20260920')
checkpoint=ROOT/'channels-import-checkpoint.json'
done=set(json.loads(checkpoint.read_text())) if checkpoint.exists() else set()
while not (ROOT/'channels-cover-manifest.json').exists():time.sleep(10)
refs=json.loads((ROOT/'channels-cover-manifest.json').read_text())['references']
reference_keys={r['post']+'|'+r['role']+'|'+r['file'] for r in refs}
report={'total_references':len(refs),'unique_references':len(reference_keys),
        'duplicate_references':len(refs)-len(reference_keys),'linked':len(done),'added_links':0,
        'missing_posts':[],'invalid_files':[],'state':'running'}
with SessionLocal() as db:
    posts=dict(db.execute(text("SELECT external_id,id FROM social_posts WHERE provider='wechat' AND external_id=ANY(:ids)"),{'ids':list({r['post'] for r in refs})}).all());db.commit()
    while True:
        pending=[];new=[]
        for ref in refs:
            key=ref['post']+'|'+ref['role']+'|'+ref['file']
            if key in done:continue
            path=ROOT/'channel-covers'/ref['file']
            if not path.is_file():continue
            post=posts.get(ref['post'])
            if not post:report['missing_posts'].append(ref['post']);continue
            asset=register_file(db,path,Path('/data/media'),'wechat')
            if not asset:report['invalid_files'].append(ref['file']);continue
            report['added_links']+=link(db,asset,post,ref['role'],ref['url'],retain=False) or 0
            pending.append({'post':str(post),'media':str(asset),'role':ref['role']});new.append(key)
            if len(pending)>=25:
                retain_post_media_batch(db,pending);db.commit();done.update(new);pending=[];new=[]
                checkpoint.write_text(json.dumps(sorted(done)));report['linked']=len(done)
                (ROOT/'channels-import.json').write_text(json.dumps(report));print(json.dumps(report),flush=True)
        if pending:
            retain_post_media_batch(db,pending);db.commit();done.update(new);checkpoint.write_text(json.dumps(sorted(done)))
        report['linked']=len(done)
        downloads=ROOT/'channels-downloads.json'
        if downloads.exists() and json.loads(downloads.read_text()).get('state')=='complete':
            report['state']='complete';report['not_linked']=len(reference_keys-done)
            (ROOT/'channels-import.json').write_text(json.dumps(report));print(json.dumps(report),flush=True);break
        (ROOT/'channels-import.json').write_text(json.dumps(report));time.sleep(30)
