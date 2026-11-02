"""Import local contact images with exact source ownership and bounded transactions."""
import hashlib
import json
from collections import defaultdict
from pathlib import Path

from app.archive_media import register_file
from app.db import SessionLocal
from app.platform_store import object_id, observe_many, platform_engine
from PIL import Image
from sqlalchemy import text

ROOT=Path('/data/staging/wechat-media-backfill-20260920')
CACHE=Path('/data/sns-cache')
report={'references':0,'cached_references':0,'imported_references':0,'missing_cache':0,'unknown_accounts':0,
        'invalid_images':0,'encrypted_attachments':0,'updated_avatars':0,'state':'running','issues':[]}
refs=json.loads((ROOT/'contact-media.json').read_text())['references']
checkpoint=ROOT/'contact-import-checkpoint.json'
done=set(json.loads(checkpoint.read_text())) if checkpoint.exists() else set()
available={p.name for p in CACHE.iterdir() if p.name.endswith('.bin')}
with SessionLocal() as db:
    rows=db.execute(text("SELECT id,external_id FROM source_accounts WHERE provider='wechat'")).all()
    accounts={r.external_id:str(r.id) for r in rows}
    aliases=defaultdict(set)
    for row in db.execute(text("SELECT external_id,source_account_id FROM identities WHERE provider IN ('wechat','wechat_group') AND source_account_id IS NOT NULL AND status NOT IN ('invalid','merged')")):
        aliases[row.external_id].add(str(row.source_account_id))
    for key,ids in aliases.items():
        if len(ids)==1:accounts.setdefault(key,next(iter(ids)))
    db.commit()
    selected=[]
    for ref in refs:
        report['references']+=1
        if ref.get('encrypted'):
            report['encrypted_attachments']+=1;continue
        if ref['cache_name'] not in available:
            report['missing_cache']+=1;continue
        report['cached_references']+=1
        aid=accounts.get(ref['username'])
        if not aid:
            report['unknown_accounts']+=1;report['issues'].append({'username':ref['username'],'kind':ref['kind'],'reason':'unknown_account'});continue
        selected.append((aid,ref))
    for offset in range(0,len(selected),50):
        assets=[];observations=[];touched=set();keys=[]
        for aid,ref in selected[offset:offset+50]:
            event='contact-media:'+hashlib.sha256(json.dumps(ref,sort_keys=True).encode()).hexdigest()
            if event in done:
                report['imported_references']+=1;continue
            path=CACHE/ref['cache_name']
            try:
                with Image.open(path) as im:
                    width,height=im.size;im.verify()
            except (OSError,ValueError):
                report['invalid_images']+=1;report['issues'].append({'file':ref['cache_name'],'reason':'invalid_image'});continue
            asset=register_file(db,path,Path('/data/media'),'wechat')
            if not asset:
                report['invalid_images']+=1;continue
            meta=dict(db.execute(text('SELECT sha256,media_type,byte_length,object_path FROM media_assets WHERE id=:id'),{'id':asset}).mappings().one())
            role={'avatar_big':'wechat_avatar_big','avatar_small':'wechat_avatar_small','moments_cover':'cover','brand_icon':'wechat_brand_icon'}.get(ref['kind'])
            if not role:continue
            value={'url':ref['url'],'sha256':meta['sha256'],'width':width,'height':height,'source_table':ref.get('source_table')}
            observations.append({'account':aid,'kind':'profile_media','external_id':role,'value':value,'source_event':event})
            assets.append({**meta,'aid':aid,'asset':asset,'role':role,'url':ref['url'],'width':width,'height':height})
            keys.append(event);touched.add(aid)
        if assets:
            with platform_engine('wechat').begin() as origin:
                observe_many(origin,observations)
                origin.execute(text('''INSERT INTO media(sha256,media_type,byte_length,object_path)
                    VALUES(:sha256,:media_type,:byte_length,:object_path) ON CONFLICT DO NOTHING'''),assets)
                origin.execute(text('INSERT INTO media_links(object_id,media_hash,role) VALUES(:object,:hash,:role) ON CONFLICT DO NOTHING'),
                    [{'object':object_id(a['aid'],'profile_media',a['role']),'hash':a['sha256'],'role':a['role']} for a in assets])
            for a in assets:
                db.execute(text('DELETE FROM source_account_media WHERE account_id=:aid AND role=:role AND media_id<>:asset'),a)
                db.execute(text('INSERT INTO source_account_media(account_id,media_id,role) VALUES(:aid,:asset,:role) ON CONFLICT DO NOTHING'),a)
                db.execute(text('INSERT INTO remote_media_sources(source_url,media_id) VALUES(:url,:asset) ON CONFLICT(source_url) DO UPDATE SET media_id=EXCLUDED.media_id'),a)
                db.execute(text('''INSERT INTO media_links(media_id,entity_type,entity_id,role,source_account_id)
                    SELECT :asset,'profile',profile_id,:role,:aid FROM source_account_links WHERE account_id=:aid ON CONFLICT DO NOTHING'''),a)
            for aid in touched:
                # Prefer the larger of the cached big/small images; never replace a manual selected avatar.
                options=db.execute(text("SELECT m.media_id,a.object_path FROM source_account_media m JOIN media_assets a ON a.id=m.media_id WHERE m.account_id=:aid AND m.role IN ('wechat_avatar_big','wechat_avatar_small')"),{'aid':aid}).all()
                sizes=[]
                for mid,path in options:
                    try:
                        with Image.open(path) as im:sizes.append((im.width*im.height,str(mid)))
                    except OSError:pass
                if not sizes:continue
                chosen=max(sizes)[1]
                db.execute(text("DELETE FROM source_account_media WHERE account_id=:aid AND role='wechat_avatar' AND media_id<>CAST(:asset AS uuid)"),{'aid':aid,'asset':chosen})
                db.execute(text("INSERT INTO source_account_media(account_id,media_id,role) VALUES(:aid,:asset,'wechat_avatar') ON CONFLICT DO NOTHING"),{'aid':aid,'asset':chosen})
                db.execute(text("""INSERT INTO media_links(media_id,entity_type,entity_id,role,source_account_id)
                    SELECT CAST(:asset AS uuid),'profile',profile_id,'wechat_avatar',CAST(:aid AS uuid) FROM source_account_links WHERE account_id=:aid ON CONFLICT DO NOTHING"""),{'aid':aid,'asset':chosen})
                report['updated_avatars']+=db.execute(text("""UPDATE profiles p SET avatar_media_id=:asset FROM source_account_links l WHERE l.account_id=:aid AND l.profile_id=p.id
                    AND p.avatar_media_id IS DISTINCT FROM CAST(:asset AS uuid)
                    AND NOT EXISTS(SELECT 1 FROM media_links m WHERE m.entity_type='profile' AND m.entity_id=p.id AND m.role='selected_avatar')"""),{'aid':aid,'asset':chosen}).rowcount
            db.commit();done.update(keys);report['imported_references']+=len(keys)
            tmp=checkpoint.with_suffix('.tmp');tmp.write_text(json.dumps(sorted(done)));tmp.replace(checkpoint)
        else:db.commit()
        (ROOT/'contact-import.json').write_text(json.dumps(report))
        print(json.dumps({k:v for k,v in report.items() if k!='issues'}),flush=True)
report['state']='complete';(ROOT/'contact-import.json').write_text(json.dumps(report))
