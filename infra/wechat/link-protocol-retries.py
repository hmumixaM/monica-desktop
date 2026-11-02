"""Run inside the API image; link only verified successful protocol retry files."""
import hashlib
import json
import os
import sqlite3
from pathlib import Path

from app.archive_media import fix_url, link, register_file
from app.db import SessionLocal
from app.moments_xml import parse_moments_xml
from app.remote_media import retain_post_media_batch
from sqlalchemy import text

ROOT = Path(os.environ.get('MEDIA_RETRY_STAGE', '/data/staging/protocol-retry-20260920'))
report = json.loads((ROOT / 'downloads.json').read_text())
files = {r['cache'] for r in report['results'] if r['status'] == 'cached'}
if ROOT.name == 'protocol-retry-20260920':
    files.add('c953f89ba251641a34aa3c842688bd7c.bin')
tids = {str(r['tid']) for r in report['results'] if r['status'] == 'cached'}
if ROOT.name == 'protocol-retry-20260920':
    tids.add('-3433628171320389112')
summary = {'download_state': report['state'], 'successful_cache_files': len(files),
           'linked_slots': 0, 'added_links': 0, 'unmapped_posts': 0, 'unsupported': 0,
           'matched_files': [], 'assets': []}
with sqlite3.connect('file:/data/staging/media-snapshot-20260919/sns.db?mode=ro', uri=True) as source, SessionLocal() as db:
    db.execute(text("SET lock_timeout='10s'"))
    pending=[]
    for tid, xml in source.execute('SELECT tid,content FROM SnsTimeLine'):
        if str(tid) not in tids:
            continue
        root, _ = parse_moments_xml(xml)
        obj = root.find('TimelineObject')
        external = obj.findtext('id') or str(tid)
        post = db.execute(text("SELECT id FROM social_posts WHERE provider='wechat' AND external_id=:id"), {'id':external}).scalar_one_or_none()
        if post is None:
            summary['unmapped_posts'] += 1
            continue
        slots=[]
        cover_count = len(obj.findall('ContentObject/finderFeed/mediaList/media'))
        for pos, media in enumerate(obj.findall('ContentObject/mediaList/media'),1 + cover_count):
            slots.append((f'media-{pos}',media,False))
            live=media.find('LivePhoto')
            if live is not None:
                nested=live.find('liveMedia')
                slots.append((f'live-media-{pos}',nested if nested is not None else live,True))
        for role, media, live_video in slots:
            url, thumb = media.find('url'), media.find('thumb')
            attrs = {**(thumb.attrib if thumb is not None else {}), **(url.attrib if url is not None else {})}
            key, token = attrs.get('key',''), attrs.get('token','')
            if not key or key == '0':
                enc=media.find('.//enc')
                if enc is None: enc=root.find('.//enc')
                key=enc.get('key','') if enc is not None else key
            raw = media.findtext('url') or media.findtext('thumb')
            if not raw:
                continue
            normalized = fix_url(raw, token, live_video or media.findtext('type') in ('6','15'))
            preserved = normalized.replace('https://','http://',1) if raw.startswith('http://') else normalized
            names = [hashlib.md5((u+'|'+key).encode()).hexdigest()+'.bin' for u in (preserved, raw, normalized)]
            name = next((n for n in names if n in files), None)
            if not name:
                continue
            path=Path('/data/sns-cache')/name
            if not path.is_file() or not path.stat().st_size:
                raise RuntimeError('Reported successful cache is missing: '+name)
            asset=register_file(db,path,Path('/data/media'),'wechat')
            if not asset:
                summary['unsupported'] += 1
                continue
            summary['added_links'] += link(db,asset,post,role,raw,retain=False) or 0
            pending.append({'post':str(post),'media':str(asset),'role':role})
            verified=db.execute(text("SELECT a.media_type,a.byte_length,a.object_path FROM media_links l JOIN media_assets a ON a.id=l.media_id WHERE l.entity_type='social_post' AND l.entity_id=:post AND l.media_id=:asset AND l.role=:role"),{'post':post,'asset':asset,'role':role}).mappings().one()
            if not Path(verified['object_path']).is_file():
                raise RuntimeError('Registered media file missing')
            summary['linked_slots'] += 1
            summary['matched_files'].append(name)
            summary['assets'].append({'post':str(post),'media':str(asset),'bytes':verified['byte_length'],'type':verified['media_type']})
            if len(pending)>=25:
                retain_post_media_batch(db,pending);db.commit();pending=[]
                print(json.dumps({'linked_slots':summary['linked_slots']}),flush=True)
    if pending:retain_post_media_batch(db,pending)
    db.commit()
summary['unmatched_files']=sorted(files-set(summary['matched_files']))
target=ROOT/'links.json'; temporary=target.with_suffix('.tmp')
temporary.write_text(json.dumps(summary)); temporary.replace(target)
print(json.dumps({k:v for k,v in summary.items() if k not in ('assets','matched_files')}),flush=True)
