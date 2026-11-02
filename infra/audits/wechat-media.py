"""Read-only comparison of a fixed WeChat SNS snapshot with local media projections.

Run inside an API environment after the selected local archive pass completes.
The report contains identifiers and local paths: keep it outside version control.
"""
import argparse
import hashlib
import json
import sqlite3
from collections import Counter, defaultdict
from pathlib import Path

from app.archive_media import fix_url
from app.db import SessionLocal
from app.moments_xml import parse_moments_xml
from app.platform_store import object_id, platform_engine
from sqlalchemy import text


def audit(sns, cache):
    counts = Counter()
    gaps = []
    differences = []
    available = {path.name for path in cache.iterdir() if path.name.endswith('.bin')}
    matched_files = set()
    with SessionLocal() as db, platform_engine('wechat').connect() as source:
        db.execute(text('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY'))
        source.execute(text('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY'))
        posts = {r['external_id']: dict(r) for r in db.execute(text(
            "SELECT id,external_id,source_account_id FROM social_posts WHERE provider='wechat'"
        )).mappings()}
        files = {r['source_path']: dict(r) for r in db.execute(text('''
            SELECT f.source_path,f.byte_length,f.modified_ns,f.media_id,a.sha256,a.object_path
            FROM archive_media_files f JOIN media_assets a ON a.id=f.media_id
            WHERE f.provider='wechat' ''')).mappings()}
        links = {(str(r.entity_id), str(r.media_id), r.role) for r in db.execute(text('''
            SELECT l.entity_id,l.media_id,l.role FROM media_links l
            JOIN social_posts p ON p.id=l.entity_id
            WHERE l.entity_type='social_post' AND p.provider='wechat' '''))}
        retained = {(str(r.object_id), r.media_hash, r.role) for r in source.execute(text(
            'SELECT object_id,media_hash,role FROM media_links'))}
    slot_assets = defaultdict(set)
    for post_id, media_id, role in links:
        slot_assets[post_id, role].add(media_id)
    with sqlite3.connect(f'file:{sns}?mode=ro', uri=True) as original:
        for tid, xml in original.execute('SELECT tid,content FROM SnsTimeLine'):
            counts['source_posts'] += 1
            root, repairs = parse_moments_xml(xml)
            counts['recovered_xml'] += bool(repairs)
            obj = root.find('TimelineObject')
            if obj is None:
                differences.append({'tid': str(tid), 'reason': 'missing_timeline_object'})
                continue
            external = obj.findtext('id') or str(tid)
            post = posts.get(external)
            for position, media in enumerate(obj.findall('ContentObject/mediaList/media'), 1):
                counts['media_references'] += 1
                original_url, thumbnail = media.find('url'), media.find('thumb')
                attrs = {**(thumbnail.attrib if thumbnail is not None else {}),
                         **(original_url.attrib if original_url is not None else {})}
                key, token = attrs.get('key', ''), attrs.get('token', '')
                if not key or key == '0':
                    enc = root.find('.//enc')
                    key = enc.get('key', '') if enc is not None else key
                matched = None
                for raw, video in ((media.findtext('url'), media.findtext('type') in ('6', '15')),
                                   (media.findtext('thumb'), False)):
                    if not raw:
                        continue
                    for url in (fix_url(raw, token, video), raw):
                        name = hashlib.md5((url + '|' + key).encode()).hexdigest() + '.bin'
                        if name in available:
                            matched = cache / name
                            break
                    if matched:
                        break
                evidence = {'post': external, 'position': position}
                if matched is None:
                    counts['cache_misses'] += 1
                    gaps.append({**evidence, 'reason': 'cache_miss'})
                    continue
                counts['cache_matches'] += 1
                matched_files.add(str(matched))
                asset = files.get(str(matched))
                reason = None
                if not post:
                    reason = 'post_missing'
                elif not asset:
                    reason = 'asset_not_registered'
                else:
                    stamp = matched.stat()
                    role = f'media-{position}'
                    if (stamp.st_size, stamp.st_mtime_ns) != (asset['byte_length'], asset['modified_ns']):
                        reason = 'cache_file_changed'
                    elif not Path(asset['object_path']).is_file():
                        reason = 'asset_file_missing'
                    elif (str(post['id']), str(asset['media_id']), role) not in links:
                        reason = 'crm_link_missing'
                    elif slot_assets[str(post['id']), role] != {str(asset['media_id'])}:
                        reason = 'crm_slot_has_extra_assets'
                    elif not post['source_account_id']:
                        reason = 'source_account_missing'
                    elif (str(object_id(post['source_account_id'], 'post', external)), asset['sha256'], role) not in retained:
                        reason = 'source_link_missing'
                if reason:
                    counts[reason] += 1
                    differences.append({**evidence, 'file': str(matched), 'reason': reason})
                else:
                    counts['verified_references'] += 1
    counts['distinct_matched_files'] = len(matched_files)
    return {'counts': dict(counts), 'gaps': gaps, 'differences': differences}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--sns-db', type=Path, required=True)
    parser.add_argument('--cache', type=Path, required=True)
    parser.add_argument('--report', type=Path, required=True)
    args = parser.parse_args()
    result = audit(args.sns_db, args.cache)
    args.report.write_text(json.dumps(result, indent=2))
    print(json.dumps({'counts': result['counts'], 'differences': len(result['differences']),
                      'cache_gaps': len(result['gaps']), 'report': str(args.report)}), flush=True)
    if result['differences']:
        raise SystemExit(1)
