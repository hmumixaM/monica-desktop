"""Archive the collector's hash-verified local bytes and explicit owner links."""
import re
from pathlib import Path

from sqlalchemy import text

from .archive_media import link, register_file
from .jobs import heavy_io
from .runtime_status import progress


def archive(db, snapshot, root, media_root, *, stop_requested=None):
    def checkpoint(flush=None):
        if stop_requested is not None and stop_requested():
            if flush is not None:
                flush()
            db.commit()
            raise RuntimeError('LinkedIn archive stopped at committed checkpoint')

    assets={};result={'asset_urls':0,'registered_files':0,'added_links':0,'missing_files':0,'unmapped_accounts':0,'unmapped_posts':0}
    rows = snapshot.get('local_assets') or []
    paths = [str((root/'assets'/row['local_path']).resolve()) for row in rows]
    cached = {row['source_path']: row for row in db.execute(text("""SELECT f.source_path,
        f.byte_length,f.modified_ns,f.media_id,a.object_path,a.sha256
        FROM archive_media_files f JOIN media_assets a ON a.id=f.media_id
        WHERE f.provider='linkedin' AND f.source_path=ANY(:paths)"""), {'paths': paths}).mappings()}
    known_urls = dict(db.execute(text('SELECT source_url,media_id FROM remote_media_sources WHERE source_url=ANY(:urls)'),
                                {'urls': [row['source_url'] for row in rows]}).all())
    db.commit()
    pending_urls = []

    def flush_urls():
        if pending_urls:
            db.execute(text('''INSERT INTO remote_media_sources(source_url,media_id) VALUES(:url,:id)
                ON CONFLICT(source_url) DO UPDATE SET media_id=EXCLUDED.media_id
                WHERE remote_media_sources.media_id IS DISTINCT FROM EXCLUDED.media_id'''), sorted(pending_urls, key=lambda row: row['url']))
            db.commit()
            pending_urls.clear()

    for index, row in enumerate(rows, start=1):
        checkpoint(flush_urls)
        name=row['local_path'];sha=row['content_hash'];path=root/'assets'/name
        if not re.fullmatch(r'[a-f0-9]{64}\.[a-z0-9]+',name) or not name.startswith(sha+'.') or path.is_symlink():
            raise ValueError('Unsafe LinkedIn asset path')
        if not path.is_file():result['missing_files']+=1;continue
        stamp=path.stat()
        if stamp.st_size!=row['byte_count']:raise ValueError('LinkedIn asset size mismatch')
        resolved=str(path.resolve())
        previous=cached.get(resolved)
        if (previous and previous['byte_length']==stamp.st_size and previous['modified_ns']==stamp.st_mtime_ns
                and Path(previous['object_path']).is_file()):
            asset=previous
            media=previous['media_id']
        else:
            # Only changed bytes acquire the disk permit. Cached manifests must
            # not perform tens of thousands of individual WAL flushes on NAS.
            flush_urls()
            db.commit()
            with heavy_io('LinkedIn asset'):
                checkpoint()
                media=register_file(db,path,media_root,'linkedin')
                db.commit()
            asset=dict(db.execute(text('SELECT sha256,object_path FROM media_assets WHERE id=:id'),{'id':media}).mappings().one())
            cached[resolved]={**asset,'media_id':media,'byte_length':stamp.st_size,'modified_ns':stamp.st_mtime_ns}
            db.commit()
        if asset['sha256']!=sha:raise ValueError('LinkedIn asset hash mismatch')
        assets[row['source_url']]=media;result['asset_urls']+=1
        if known_urls.get(row['source_url'])!=media:
            pending_urls.append({'url':row['source_url'],'id':media})
            known_urls[row['source_url']]=media
        if index % 100 == 0:
            flush_urls()
            progress(stage='local_assets', processed=index, total=len(rows))
    flush_urls()
    db.commit()
    checkpoint()
    result['registered_files']=len(set(assets.values()))
    pending=[]
    def flush_posts():
        if pending:
            from .remote_media import retain_post_media_batch
            retain_post_media_batch(db,pending)
            db.commit()
            pending.clear()
    def attach(url,entity,kind,role):
        media=assets.get(url)
        if not media:return
        # Avoid rebuilding profiles and touching source observation history on replay.
        exists=db.execute(text('SELECT 1 FROM media_links WHERE entity_type=:kind AND entity_id=:id AND role=:role AND media_id=:media'),{'kind':kind,'id':entity,'role':role,'media':media}).first()
        if exists:return
        if kind=='social_post':
            result['added_links']+=link(db,media,entity,role,url,entity_type=kind,retain=False) or 0
            pending.append({'post':str(entity),'media':str(media),'role':role})
            if len(pending)>=100:flush_posts()
        else:
            flush_posts()
            result['added_links']+=link(db,media,entity,role,url,entity_type=kind) or 0
            db.commit()
    for index, item in enumerate(snapshot.get('profiles') or [], start=1):
        checkpoint(flush_posts)
        progress(stage='asset_links', processed=index, total=len(snapshot.get('profiles') or []))
        external=str(item.get('provider_user_id') or item.get('profile_url') or '')
        account=db.execute(text("SELECT id FROM source_accounts WHERE provider='linkedin' AND external_id=:id"),{'id':external}).scalar_one_or_none()
        if not account:result['unmapped_accounts']+=1;continue
        profile=item.get('profile') or {}
        for key,role in [('avatar_url','linkedin_avatar'),('background_url','linkedin_cover')]:
            attach(profile.get(key),account,'source_account',role)
        for post in item.get('posts') or []:
            checkpoint(flush_posts)
            external_post=next((str(post[k]) for k in ('provider_post_id','urn','external_id','id','source_key') if post.get(k)),None)
            target=db.execute(text("SELECT id FROM social_posts WHERE provider='linkedin' AND external_id=:id AND source_account_id=:account"),{'id':external_post,'account':account}).scalar_one_or_none()
            if not target:result['unmapped_posts']+=1;continue
            from .linkedin_import import _linkedin_media
            for media in _linkedin_media(post):
                attach(media.get('source_url'),target,'social_post',media['role'])
                if media.get('thumbnail_url')!=media.get('source_url'):
                    attach(media.get('thumbnail_url'),target,'social_post','thumbnail-'+media['role'])
    flush_posts()
    db.commit()
    checkpoint()
    return result
