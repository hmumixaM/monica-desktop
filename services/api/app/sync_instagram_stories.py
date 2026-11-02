"""Import downloaded Story and Highlight manifests, retaining archives locally."""
import argparse
import hashlib
import json
import os
import time
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import text

from .archive_media import link
from .content import content_hash
from .db import SessionLocal, engine
from .imports import project_batch
from .jobs import heavy_io, lower_priority
from .schemas import ImportBatchRequest
from .source_projection import enabled

LOCK_ID = 11803301


def read_manifest(path):
    raw = path.read_bytes()
    doc = json.loads(raw)
    if doc.get('schema_version') != 1 or not str(doc.get('mediaid', '')).isdigit() or not str(doc.get('owner_id', '')).isdigit():
        raise ValueError('Invalid Story manifest identity or version')
    names = doc.get('files', [])
    if not names or any(not isinstance(n, str) or Path(n).name != n for n in names):
        raise ValueError('Invalid Story file list')
    paths = [path.parent / n for n in names]
    if path.is_symlink() or path.parent.is_symlink() or any(p.is_symlink() or not p.is_file() for p in paths):
        raise ValueError('Incomplete Story archive')
    media = [p for p in paths if p.suffix.lower() in ('.jpg', '.jpeg', '.png', '.webp', '.mp4')]
    required = '.mp4' if doc.get('is_video') else None
    if not media or (required and not any(p.suffix.lower() == required for p in media)):
        raise ValueError('Story media not yet complete')
    stamp = content_hash({'version': 2, 'manifest': hashlib.sha256(raw).hexdigest(),
                          'files': [(p.name, p.stat().st_size, p.stat().st_mtime_ns) for p in paths]})
    return doc, media, stamp



def read_highlight_manifest(path):
    raw = path.read_bytes()
    doc = json.loads(raw)
    highlight_id = str(doc.get('highlight_id') or '')
    title = doc.get('highlight_title')
    if (doc.get('schema_version') != 1 or doc.get('archive_kind') != 'highlight'
            or not highlight_id.isdigit() or not isinstance(title, str) or not title
            or not str(doc.get('mediaid', '')).isdigit() or not str(doc.get('owner_id', '')).isdigit()):
        raise ValueError('Invalid Highlight manifest identity or version')
    names = doc.get('files', [])
    if not names or any(not isinstance(n, str) or Path(n).name != n for n in names):
        raise ValueError('Invalid Highlight file list')
    paths = [path.parent / n for n in names]
    if (path.is_symlink() or path.parent.is_symlink() or path.parent.parent.is_symlink()
            or any(p.is_symlink() or not p.is_file() for p in paths)):
        raise ValueError('Incomplete Highlight archive')
    media = [p for p in paths if p.suffix.lower() in ('.jpg', '.jpeg', '.png', '.webp', '.mp4')]
    required = '.mp4' if doc.get('is_video') else None
    if not media or (required and not any(p.suffix.lower() == required for p in media)):
        raise ValueError('Highlight media not yet complete')
    stamp = content_hash({'version': 1, 'manifest': hashlib.sha256(raw).hexdigest(),
                          'files': [(p.name, p.stat().st_size, p.stat().st_mtime_ns) for p in paths]})
    return doc, media, stamp


def story_locations(raw):
    """Keep explicit place tags; never treat sticker screen x/y as GPS."""
    locations = []
    seen = set()
    for container in (raw, raw.get('iphone_struct') or {}):
        candidates = [container.get('location')]
        candidates += [item.get('location') for item in container.get('story_locations', []) if isinstance(item, dict)]
        for value in candidates:
            if not isinstance(value, dict):
                continue
            place = {k: v for k, v in value.items() if v is not None and k != 'profile_pic_url'}
            lat, lng = place.get('lat', place.get('latitude')), place.get('lng', place.get('longitude'))
            try:
                lat, lng = float(lat), float(lng)
                if -90 <= lat <= 90 and -180 <= lng <= 180:
                    place['latitude'], place['longitude'] = lat, lng
            except (ValueError, TypeError):
                pass
            place['coordinate_source'] = 'instagram_place_tag'
            key = str(place.get('pk') or place.get('id') or content_hash(place))
            if key not in seen:
                locations.append(place)
                seen.add(key)
    return locations


def import_story(db, doc, files, media_root):
    external = str(doc['mediaid'])
    raw = doc.get('raw_metadata') or {}
    video = bool(doc.get('is_video'))
    video_urls = raw.get('video_resources') or (raw.get('iphone_struct') or {}).get('video_versions') or []
    video_url = raw.get('video_url') or next((r.get('src') or r.get('url') for r in video_urls if r.get('src') or r.get('url')), None)
    image_url = raw.get('display_url') or f'archive://instagram/{external}/image'
    media = [{'media_type': 'video' if video else 'image',
              'source_url': (video_url or f'archive://instagram/{external}/video') if video else image_url,
              'thumbnail_url': image_url}]
    content = {'text': doc.get('caption') or '', 'username': doc.get('owner_username'),
               'owner': {'id': str(doc['owner_id']), 'username': doc.get('owner_username')},
               'expires_at': doc['expiring_at'], 'coverage_status': 'archived', 'media': media,
               'permalink': f"https://www.instagram.com/stories/{doc['owner_username']}/{external}/",
               'archive_manifest': doc}
    locations = story_locations(raw)
    if locations:
        content['locations'] = locations
        content['location'] = locations[0]
    digest = content_hash(content)
    previous = db.execute(text("SELECT current_content_hash FROM social_stories WHERE provider='instagram' AND external_id=:id"), {'id': external}).scalar_one_or_none()
    # Replays keep the same observation ID, including cross-database retries.
    key = 'instagram-story-archive:' + external + ':' + digest
    request = ImportBatchRequest(source='instagram', stream='stories', batch_id=key, idempotency_key=key,
        observed_at=datetime.fromtimestamp(doc['captured_at'], UTC),
        records=[{'external_id': external, 'source_account_external_id': str(doc['owner_id']),
                  'entity_type': 'story', 'occurred_at': doc['taken_at'], 'content': content}])
    receipt = project_batch(db, request)
    if receipt.get('status') == 'queued':
        db.commit()
        return {'pending_projection': 1}
    story = db.execute(text("SELECT id FROM social_stories WHERE provider='instagram' AND external_id=:id"), {'id': external}).scalar_one()
    from .archive_media import prepare_archive_batch, register_prepared_file
    prepared = prepare_archive_batch(db, files, media_root, 'instagram')
    added = 0
    for file in files:
        stamp, asset, snapshot = prepared[file.resolve()]
        asset = asset or register_prepared_file(db, file, stamp, snapshot, 'instagram')
        if not asset:
            raise ValueError('Unsupported Story media')
        role = 'thumbnail-1' if video and file.suffix.lower() != '.mp4' else 'media-1'
        url = media[0]['source_url'] if file.suffix.lower() == '.mp4' or not video else image_url
        added += link(db, asset, story, role, url, entity_type='social_story') or 0
    db.commit()
    return {'inserted': int(previous is None), 'changed': int(previous is not None and previous != digest), 'added_media_links': added}


def import_highlight(db, doc, files, media_root):
    """Store a Highlight separately from its original Story media ID."""
    media_id = str(doc['mediaid'])
    highlight_id = str(doc['highlight_id'])
    external = f'highlight:{highlight_id}:{media_id}'
    raw = doc.get('raw_metadata') or {}
    video = bool(doc.get('is_video'))
    video_urls = raw.get('video_resources') or []
    video_url = raw.get('video_url') or next((r.get('src') or r.get('url') for r in video_urls if isinstance(r, dict) and (r.get('src') or r.get('url'))), None)
    image_url = raw.get('display_url') or f'archive://instagram/{external}/image'
    media = [{'media_type': 'video' if video else 'image',
              'source_url': (video_url or f'archive://instagram/{external}/video') if video else image_url,
              'thumbnail_url': image_url}]
    content = {'text': doc.get('caption') or '', 'username': doc.get('owner_username'),
               'owner': {'id': str(doc['owner_id']), 'username': doc.get('owner_username')},
               'archive_kind': 'highlight', 'coverage_status': 'archived', 'media': media,
               'highlight': {'id': highlight_id, 'title': doc['highlight_title']},
               'permalink': f"https://www.instagram.com/stories/highlights/{highlight_id}/",
               'archive_manifest': doc}
    locations = story_locations(raw)
    if locations:
        content['locations'] = locations
        content['location'] = locations[0]
    digest = content_hash(content)
    previous = db.execute(text("SELECT current_content_hash FROM social_stories WHERE provider='instagram' AND external_id=:id"), {'id': external}).scalar_one_or_none()
    key = 'instagram-highlight-archive:' + external + ':' + digest
    request = ImportBatchRequest(source='instagram', stream='stories', batch_id=key, idempotency_key=key,
        observed_at=datetime.fromtimestamp(doc['captured_at'], UTC),
        records=[{'external_id': external, 'source_account_external_id': str(doc['owner_id']),
                  'entity_type': 'story', 'occurred_at': doc['taken_at'], 'content': content}])
    receipt = project_batch(db, request)
    if receipt.get('status') == 'queued':
        db.commit()
        return {'pending_projection': 1}
    story = db.execute(text("SELECT id FROM social_stories WHERE provider='instagram' AND external_id=:id"), {'id': external}).scalar_one()
    from .archive_media import prepare_archive_batch, register_prepared_file
    prepared = prepare_archive_batch(db, files, media_root, 'instagram')
    added = 0
    for file in files:
        stamp, asset, snapshot = prepared[file.resolve()]
        asset = asset or register_prepared_file(db, file, stamp, snapshot, 'instagram')
        if not asset:
            raise ValueError('Unsupported Highlight media')
        role = 'thumbnail-1' if video and file.suffix.lower() != '.mp4' else 'media-1'
        url = media[0]['source_url'] if file.suffix.lower() == '.mp4' or not video else image_url
        added += link(db, asset, story, role, url, entity_type='social_story') or 0
    db.commit()
    return {'inserted': int(previous is None), 'changed': int(previous is not None and previous != digest), 'added_media_links': added}


def cycle(db, root, media_root, checkpoint):
    return _cycle(db, root, media_root, checkpoint, read_manifest, import_story, '*/*.manifest.json')


def highlight_cycle(db, root, media_root, checkpoint):
    return _cycle(db, root, media_root, checkpoint, read_highlight_manifest, import_highlight, '*/*/*.manifest.json')


def _cycle(db, root, media_root, checkpoint, reader, importer, pattern):
    root = (root / "CURRENT").resolve() if (root / "CURRENT").is_symlink() else root
    if not root.is_dir():
        return {'waiting_for_archive': True}
    if not enabled(db, 'instagram'):
        raise ValueError('Instagram independent source store must be enabled')
    db.rollback()
    state = json.loads(checkpoint.read_text()) if checkpoint.exists() else {}
    report = {'manifests': 0, 'skipped': 0, 'inserted': 0, 'changed': 0, 'added_media_links': 0, 'failed': 0, 'pending_projection': 0}
    dirty = 0
    flushed = time.monotonic()

    def flush():
        checkpoint.parent.mkdir(parents=True, exist_ok=True)
        temporary = checkpoint.with_suffix('.tmp')
        temporary.write_text(json.dumps(state, sort_keys=True))
        temporary.replace(checkpoint)

    from .runtime_status import STOP
    for path in sorted(root.glob(pattern)):
        if STOP.is_set():
            report['stopped_at_checkpoint'] = True
            break
        report['manifests'] += 1
        try:
            doc, files, stamp = reader(path)
            key = path.relative_to(root).as_posix()
            if state.get(key) == stamp:
                report['skipped'] += 1
                continue
            with heavy_io('instagram story'):
                result = importer(db, doc, files, media_root)
            for field, count in result.items():
                report[field] += count
            if result.get('pending_projection'):
                continue
            state[key] = stamp
            dirty += 1
            if dirty >= 50 or time.monotonic() - flushed >= 5:
                flush()
                dirty, flushed = 0, time.monotonic()
        except (ValueError, OSError, KeyError) as exc:
            db.rollback()
            report['failed'] += 1
            print(json.dumps({'story_import_error': type(exc).__name__, 'manifest': path.name}), flush=True)
    if dirty:
        flush()
    if report['pending_projection']:
        report['state'] = 'pending'
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    staging = Path(os.environ.get('STAGING_ROOT', '/data/staging'))
    parser.add_argument('--root', type=Path, default=staging / 'instagram-stories')
    parser.add_argument('--highlight-root', type=Path, default=staging / 'instagram-highlights')
    parser.add_argument('--highlight-checkpoint', type=Path, default=staging / 'instagram-highlights-checkpoint.json')
    parser.add_argument('--media-root', type=Path, default=Path(os.environ.get('MEDIA_ROOT', '/data/media')))
    parser.add_argument('--checkpoint', type=Path, default=staging / 'instagram-stories-checkpoint.json')
    parser.add_argument('--watch', action='store_true')
    parser.add_argument('--interval', type=int, default=86400)
    args = parser.parse_args()
    lower_priority()
    from .runtime_status import STOP, IdleWait, install_shutdown, phase
    install_shutdown()
    idle = IdleWait()
    while not STOP.is_set():
        pending = False
        try:
            with engine.connect() as lease:
                locked = lease.execute(text('SELECT pg_try_advisory_lock(:id)'), {'id': LOCK_ID}).scalar_one()
                lease.commit()
                if locked:
                    try:
                        with SessionLocal() as db, phase('instagram', 'stories') as details:
                            details.update(cycle(db, args.root, args.media_root, args.checkpoint))
                            pending |= bool(details.get('pending_projection'))
                            print(json.dumps(details), flush=True)
                        if not STOP.is_set():
                            with SessionLocal() as db, phase('instagram', 'highlights') as details:
                                details.update(highlight_cycle(db, args.highlight_root, args.media_root, args.highlight_checkpoint))
                                pending |= bool(details.get('pending_projection'))
                                print(json.dumps(details), flush=True)
                    finally:
                        lease.execute(text('SELECT pg_advisory_unlock(:id)'), {'id': LOCK_ID})
                        lease.commit()
        except Exception as exc:  # Never advance failed checkpoints.
            print(json.dumps({'story_sync_error': type(exc).__name__}), flush=True)
            if not args.watch:
                raise
        if not args.watch:
            break
        idle.wait(3 if pending else max(args.interval, 60))


if __name__ == '__main__':
    main()
