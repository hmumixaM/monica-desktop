"""Queued, content-addressed LinkedIn images. Also shared with the worker."""

import hashlib
import json
import os
import tempfile
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

from sqlalchemy import text

from .account_links import rebuild_media
from .ai.image_ocr import enqueue_media as enqueue_image_ocr
from .ai.queue import enqueue_story_media
from .media_store import record_asset

WECHAT_IMAGE_HOSTS = frozenset({"wxapp.tc.qq.com", "finder.video.qq.com"})


def provider_image_url(url):
    if not isinstance(url,str): return None
    parsed=urlsplit(url); host=(parsed.hostname or '').lower()
    if (parsed.scheme=='https' and not parsed.username and not parsed.password and parsed.port in (None,443)
            and (host in WECHAT_IMAGE_HOSTS or any(host==domain or host.endswith('.'+domain) for domain in ('licdn.com','cdninstagram.com','fbcdn.net')))):
        return url
    return None


def linkedin_image_url(url):
    if not isinstance(url, str):
        return None
    parsed = urlsplit(url)
    host = (parsed.hostname or "").lower()
    if (parsed.scheme == "https" and not parsed.username and not parsed.password
            and parsed.port in (None, 443) and (host == "licdn.com" or host.endswith(".licdn.com"))):
        return url
    return None


class _ProviderRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not provider_image_url(newurl):
            raise ValueError("Media redirect is outside approved provider CDNs")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def download_image(url, media_root, max_bytes=25 * 1024 * 1024):
    """Stream to a temporary file, reject non-images, then atomically deduplicate."""
    if not provider_image_url(url):
        raise ValueError("Expected an approved HTTPS provider image")
    staging = Path(media_root) / ".staging"
    staging.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        opener = urllib.request.build_opener(_ProviderRedirect())
        request = urllib.request.Request(url, headers={"User-Agent": "MonicaNext/1.0"})
        with opener.open(request, timeout=20) as response:
            mime = response.headers.get_content_type()
            if mime == "image/jpg":
                mime = "image/jpeg"
            if mime not in {"image/jpeg", "image/png", "image/webp", "image/gif", "image/avif"}:
                raise ValueError("Provider returned non-image content")
            digest, size = hashlib.sha256(), 0
            with tempfile.NamedTemporaryFile(dir=staging, delete=False) as output:
                temporary = Path(output.name)
                while chunk := response.read(128 * 1024):
                    size += len(chunk)
                    if size > max_bytes:
                        raise ValueError("Provider image exceeds download limit")
                    digest.update(chunk)
                    output.write(chunk)
        if not size:
            raise ValueError("Provider returned an empty image")
        sha = digest.hexdigest()
        target = Path(media_root) / "sha256" / sha[:2] / sha[2:4] / sha
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            os.replace(temporary, target)
        return sha, str(target), size, mime
    finally:
        if temporary:
            temporary.unlink(missing_ok=True)


def enqueue_media(db, *, url, entity_type, entity_id, role):
    if not provider_image_url(url):
        return False
    payload = {"url": url, "entity_type": entity_type, "entity_id": str(entity_id), "role": role}
    # Bounded daily observation keys allow a stable URL to be checked again.
    # Content and asset deduplication still use the downloaded bytes, never URL.
    payload["check_day"] = datetime.now(UTC).date().isoformat()
    payload["revalidate"] = True
    payload["download_key"] = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
    db.execute(text("""INSERT INTO media_manifest(source_url,entity_type,entity_id,role)
        VALUES (:url,:entity_type,:entity_id,:role) ON CONFLICT DO NOTHING"""), payload)
    result = db.execute(text("""
        INSERT INTO jobs(job_type, payload)
        VALUES ('media.download', CAST(:payload AS jsonb)) ON CONFLICT DO NOTHING
    """), {"payload": json.dumps(payload)})
    return result.rowcount > 0


def media_state(db, payload, state, error=None):
    db.execute(text("""UPDATE media_manifest SET state=:state,error=:error,checked_at=now()
        WHERE source_url=:url AND entity_type=:entity_type AND entity_id=:entity_id AND role=:role"""),
        {**payload, 'state':state, 'error':error})


def needs_download(db, payload):
    checked = db.execute(text("SELECT checked_at FROM remote_media_sources WHERE source_url=:url"), payload).scalar_one_or_none()
    return not checked or (payload.get('revalidate') and checked.astimezone(UTC).date().isoformat() < payload.get('check_day',datetime.now(UTC).date().isoformat()))


def enqueue_post_media(db, *, media, post_id, index):
    """Image refreshes may update a poster, never a locally archived video slot."""
    role=media.get('role') or f'media-{index + 1}'
    url=media.get('source_url') or media.get('url')
    if str(media.get('media_type') or '').startswith('video') or media.get('is_video'):
        url=media.get('thumbnail_url')
        role+='-poster'
    return enqueue_media(db,url=url,entity_type='social_post',entity_id=post_id,role=role)


def enqueue_due_media(db):
    rows=db.execute(text("""SELECT source_url,entity_type,entity_id,role FROM media_manifest
        WHERE state='ready' AND checked_at < now() - CASE WHEN role LIKE '%avatar' THEN interval '1 day' ELSE interval '7 days' END
          AND NOT EXISTS (SELECT 1 FROM media_assets a WHERE a.id=media_manifest.media_id AND a.media_type LIKE 'video/%')
        ORDER BY checked_at LIMIT 100""")).mappings().all()
    return sum(enqueue_media(db,url=r['source_url'],entity_type=r['entity_type'],entity_id=r['entity_id'],role=r['role']) for r in rows)


def retain_post_media_batch(db, attachments):
    """Retain bounded archive attachments in one transaction per source DB."""
    from .platform_store import object_id, platform_engine

    rows=db.execute(text('''SELECT p.source_account_id,p.provider,p.external_id,
        m.sha256,m.media_type,m.byte_length,m.object_path,i.role
        FROM jsonb_to_recordset(CAST(:items AS jsonb)) AS i(post uuid,media uuid,role text)
        JOIN social_posts p ON p.id=i.post JOIN media_assets m ON m.id=i.media
        WHERE p.source_account_id IS NOT NULL'''),{'items':json.dumps(attachments)}).mappings().all()
    for provider in {r['provider'] for r in rows}:
        media=[dict(r) for r in rows if r['provider']==provider]
        with platform_engine(provider).begin() as source:
            source.execute(text('''INSERT INTO media(sha256,media_type,byte_length,object_path)
                VALUES(:sha256,:media_type,:byte_length,:object_path) ON CONFLICT DO NOTHING'''),media)
            source.execute(text('''INSERT INTO media_links(object_id,media_hash,role)
                VALUES(:object,:hash,:role) ON CONFLICT DO NOTHING'''),
                [{'object':object_id(r['source_account_id'],'post',r['external_id']),
                  'hash':r['sha256'],'role':r['role']} for r in media])


def retain_source_media(db, payload, media_id):
    """Keep downloaded and archive media attached to the independent account."""
    from uuid import uuid4

    if payload['entity_type']=='source_account':
        from .platform_store import observe, platform_engine
        db.execute(text('SELECT pg_advisory_xact_lock_shared(11803295)'))
        account=db.execute(text('SELECT * FROM source_accounts WHERE id=:id FOR UPDATE'),{'id':payload['entity_id']}).mappings().one()
        media=db.execute(text('SELECT * FROM media_assets WHERE id=:id'),{'id':media_id}).mappings().one()
        with platform_engine(account['provider']).begin() as source:
            result=observe(source,account['id'],'profile_media',payload['role'],{'sha256':media['sha256']},
                           source_event=payload.get('download_key') or 'media:'+str(uuid4()))
            source.execute(text('''INSERT INTO media(sha256,media_type,byte_length,object_path)
                VALUES(:sha256,:media_type,:byte_length,:object_path) ON CONFLICT DO NOTHING'''),media)
            source.execute(text('INSERT INTO media_links(object_id,media_hash,role) VALUES(:object,:hash,:role) ON CONFLICT DO NOTHING'),
                           {'object':result['object_id'],'hash':media['sha256'],'role':payload['role']})
        db.execute(text('DELETE FROM source_account_media WHERE account_id=:account AND role=:role'),{'account':account['id'],'role':payload['role']})
        db.execute(text('INSERT INTO source_account_media(account_id,media_id,role) VALUES(:account,:media,:role) ON CONFLICT DO NOTHING'),
                   {'account':account['id'],'media':media_id,'role':payload['role']})
        linked=db.execute(text('SELECT profile_id FROM source_account_links WHERE account_id=:id'),{'id':account['id']}).scalar_one_or_none()
        if linked:
            rebuild_media(db,linked)
    elif payload['entity_type'] in ('social_post','social_story'):
        table='social_posts' if payload['entity_type']=='social_post' else 'social_stories'
        owner=db.execute(text(f'SELECT source_account_id,provider,external_id FROM {table} WHERE id=:id'),{'id':payload['entity_id']}).mappings().one_or_none()
        if owner and owner['source_account_id']:
            from .platform_store import object_id, platform_engine
            media=db.execute(text('SELECT * FROM media_assets WHERE id=:id'),{'id':media_id}).mappings().one()
            with platform_engine(owner['provider']).begin() as source:
                source.execute(text('''INSERT INTO media(sha256,media_type,byte_length,object_path)
                    VALUES(:sha256,:media_type,:byte_length,:object_path) ON CONFLICT DO NOTHING'''),media)
                source.execute(text('INSERT INTO media_links(object_id,media_hash,role) VALUES(:object,:hash,:role) ON CONFLICT DO NOTHING'),
                    {'object':object_id(owner['source_account_id'],'post' if table=='social_posts' else 'story',owner['external_id']),
                     'hash':media['sha256'],'role':payload['role']})


def materialize_image(db, payload, media_root, downloaded=None):
    """Commit already downloaded bytes; never perform network I/O in a transaction."""
    from .media_store import lock_media_slot
    lock_media_slot(db, payload['entity_type'], payload['entity_id'], payload['role'], payload['url'])
    url = payload["url"]
    media_id = db.execute(text("""
        SELECT media_id FROM remote_media_sources WHERE source_url = :url
    """), {"url": url}).scalar_one_or_none()
    previous_media_id=media_id
    if downloaded or not media_id:
        if downloaded is None:
            raise ValueError('Download media before opening the materialization transaction')
        sha, path, size, mime = downloaded
        provider = ("wechat" if urlsplit(url).hostname in WECHAT_IMAGE_HOSTS
                    else "linkedin" if linkedin_image_url(url) else "instagram")
        media_id = record_asset(db, sha256=sha, path=path, byte_length=size, media_type=mime,
                                source_type=provider, source_record_id=url)
        db.execute(text("""
            INSERT INTO remote_media_sources(source_url, media_id) VALUES (:url, :id)
            ON CONFLICT(source_url) DO UPDATE SET media_id = EXCLUDED.media_id, checked_at=now()
        """), {"url": url, "id": media_id})
    # Remove only the previous version of this exact source reference, keeping
    # revisions and other references to the same content-addressed asset.
    db.execute(text("""DELETE FROM media_links l USING media_source_revisions r
        WHERE r.source_url=:url AND l.media_id=r.media_id AND l.media_id<>:media_id
        AND l.entity_type=:entity_type AND l.entity_id=:entity_id AND l.role=:role"""),
        {**payload, 'media_id':media_id})
    added = db.execute(text("""
        INSERT INTO media_links(media_id, entity_type, entity_id, role)
        VALUES (:media_id, :entity_type, :entity_id, :role) ON CONFLICT DO NOTHING
    """), {**payload, "media_id": media_id}).rowcount
    if added:
        enqueue_image_ocr(db, media_id, payload["entity_type"])
    if added and payload["entity_type"] == "social_story":
        enqueue_story_media(db, payload["entity_id"])
    db.execute(text("""INSERT INTO media_manifest(source_url,entity_type,entity_id,role,state,media_id)
        VALUES (:url,:entity_type,:entity_id,:role,'ready',:media_id)
        ON CONFLICT(source_url,entity_type,entity_id,role) DO UPDATE
        SET state='ready',media_id=EXCLUDED.media_id,error=NULL,checked_at=now()"""), {**payload,'media_id':media_id})
    retain_source_media(db,payload,media_id)
    if payload["entity_type"] == "profile" and payload["role"] == "linkedin_avatar":
        # Keep an explicitly selected/WeChat avatar; LinkedIn remains accessible separately.
        db.execute(text("""
            UPDATE profiles SET avatar_media_id = :media_id
            WHERE id = :id AND (avatar_media_id IS NULL OR
              (avatar_media_id=:previous AND NOT EXISTS(SELECT 1 FROM media_links
                WHERE entity_id=:id AND entity_type='profile' AND role IN ('wechat_avatar','selected_avatar'))))
        """), {"media_id": media_id, "previous":previous_media_id, "id": payload["entity_id"]})
    return str(media_id)


def linked_media(db, entity_ids):
    if not entity_ids:
        return {}
    rows = db.execute(text("""
        SELECT l.entity_id, l.role, m.id, m.media_type, m.width, m.height,
            COALESCE(r.source_url, m.source_record_id) AS source_url, m.byte_length
        FROM media_links l JOIN media_assets m ON m.id = l.media_id
        LEFT JOIN remote_media_sources r ON r.media_id = m.id
        WHERE l.entity_id = ANY(:ids)
            AND l.entity_type IN ('social_post', 'social_story', 'post')
        ORDER BY l.role, m.id
    """), {"ids": entity_ids}).mappings()
    result = {}
    for row in rows:
        result.setdefault(str(row["entity_id"]), []).append(dict(row))
    return result


def merge_media(remote, local):
    """Keep provider order and use local bytes once each URL is downloaded."""
    remote = remote if isinstance(remote, list) else []
    by_url = {m.get("source_url"): m for m in local if m.get("source_url")}
    result, seen = [], set()
    for item in remote:
        if not isinstance(item, dict):
            continue
        url = item.get("source_url") or item.get("thumbnail_url")
        local_item = by_url.get(url) or by_url.get(item.get("thumbnail_url")) or {}
        resolved = {**item, **local_item}
        if item.get("media_type"):
            resolved["source_media_type"] = item["media_type"]
        # A cached video poster is a local image until the video itself arrives.
        # Once the video is available, keep its poster out of the media list.
        poster = by_url.get(item.get("thumbnail_url"))
        if local_item and poster and local_item.get("id") != poster["id"]:
            resolved["thumbnail_id"] = poster["id"]
        key = str(resolved.get("id") or url)
        if key not in seen:
            result.append(resolved)
            seen.add(key)
        if local_item and poster:
            seen.add(str(poster["id"]))
    for item in local:
        if str(item["id"]) not in seen:
            result.append(item)
            seen.add(str(item["id"]))
    return result
