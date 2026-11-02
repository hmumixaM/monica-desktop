"""Enrich existing posts from the exact archived XML, without reimporting events."""
import argparse
import hashlib
import json
import sqlite3
import time
import xml.etree.ElementTree as ET
from contextlib import nullcontext

from sqlalchemy import text

from .content import content_hash, get_or_create_content
from .db import SessionLocal
from .moments_xml import finder_media, parse_moments_xml
from .platform_store import object_id, observe_many, platform_engine


def repair(db, archive, *, apply=False, profile_id=None):
    report = {'matched': 0, 'updated': 0, 'unchanged': 0, 'source_mismatch': 0, 'conflicting_media': 0}
    candidates = {}
    invalid = 0
    with sqlite3.connect(f'file:{archive}?mode=ro', uri=True) as source:
        for tid, xml in source.execute("SELECT tid,content FROM SnsTimeLine WHERE content LIKE '%<finderFeed>%' "):
            try:
                root, _ = parse_moments_xml(xml)
                obj = root.find('TimelineObject')
                media = finder_media(obj) if obj is not None else []
                if media:
                    candidates[obj.findtext('id') or str(tid)] = (xml, media)
            except ET.ParseError:
                invalid += 1
    report['source_candidates'] = len(candidates)
    report['invalid_xml'] = invalid
    clause = ' AND p.profile_id=CAST(:profile AS uuid)' if profile_id else ''
    rows = db.execute(text("""SELECT p.id,p.external_id,p.source_account_id,p.occurred_at,
        p.current_content_hash,c.payload_json FROM social_posts p JOIN content_objects c ON c.id=p.content_object_id
        WHERE p.provider='wechat' AND p.external_id=ANY(:ids) """ + clause),
        {'profile':profile_id, 'ids':list(candidates)}).mappings().all()
    db.rollback()
    report['missing_posts'] = len(candidates) - len(rows) if not profile_id else 0
    pending = []
    for post in rows:
        xml, media = candidates[post['external_id']]
        report['matched'] += 1
        old = post['payload_json']
        if hashlib.sha256(xml.encode()).hexdigest() != old.get('raw_xml_sha256'):
            report['source_mismatch'] += 1
            continue
        # Seven legacy photo-style shares retain a direct image as well as the
        # nested Channels object. Keep both, with distinct stable media slots.
        prior = old.get('media') or []
        if prior and not any(m.get('source_kind') == 'wechat_channels' for m in prior):
            media = media + [{**m, 'role':f'media-{len(media)+i+1}'} for i,m in enumerate(prior)]
        elif prior:
            direct = [m for m in prior if m.get('source_kind') != 'wechat_channels']
            media = media + [{**m, 'role':f'media-{len(media)+i+1}'} for i,m in enumerate(direct)]
        if old.get('media') == media:
            report['unchanged'] += 1
            continue
        pending.append((post, {**old, 'media':media}))
    report['pending'] = len(pending)
    if not apply:
        return report
    for offset in range(0, len(pending), 25):
        part = pending[offset:offset + 25]
        ids = [str(post['id']) for post, _ in part]
        current = dict(db.execute(text('SELECT id,current_content_hash FROM social_posts WHERE id=ANY(CAST(:ids AS uuid[])) ORDER BY id FOR UPDATE'), {'ids': ids}).all())
        valid = []
        records = []
        with (platform_engine('wechat').begin() if any(p['source_account_id'] for p, _ in part) else nullcontext()) as origin:
            for post, value in part:
                if current.get(post['id']) != post['current_content_hash']:
                    report['source_mismatch'] += 1
                    continue
                event = 'moments-channels-enrichment:' + str(post['id']) + ':' + post['payload_json']['raw_xml_sha256']
                if post['source_account_id']:
                    oid = object_id(post['source_account_id'], 'post', post['external_id'])
                    origin.execute(text('SELECT pg_advisory_xact_lock(hashtextextended(:key,0))'), {'key': str(oid)})
                    existing = origin.execute(text('SELECT current_hash FROM objects WHERE id=:id'), {'id': oid}).scalar_one_or_none()
                    if existing and existing not in (post['current_content_hash'], content_hash(value)):
                        report['source_mismatch'] += 1
                        continue
                    records.append({'account': post['source_account_id'], 'kind': 'post', 'external_id': post['external_id'],
                                    'value': value, 'source_event': event, 'occurred_at': post['occurred_at']})
                valid.append((post, value, event))
            if records:
                observe_many(origin, records)
        for post, value, event in valid:
            content_id, digest = get_or_create_content(db, value=value, content_kind='post_body')
            db.execute(text('UPDATE social_posts SET content_object_id=:content,current_content_hash=:hash WHERE id=:id'),
                       {'content': content_id, 'hash': digest, 'id': post['id']})
            db.execute(text('''INSERT INTO social_post_revisions(post_id,content_object_id,content_hash,source_record_id,observed_at)
                VALUES(:id,:content,:hash,:event,now())'''), {'id': post['id'], 'content': content_id, 'hash': digest, 'event': event})
            batch = db.execute(text("""INSERT INTO import_batches(source,stream,schema_version,batch_id,idempotency_key,status,completed_at,changed_count)
                VALUES('wechat','moments',1,:event,:event,'complete',now(),1)
                ON CONFLICT(idempotency_key) DO UPDATE SET status='complete' RETURNING id"""), {'event': event}).scalar_one()
            db.execute(text("""INSERT INTO source_observations(source,stream,external_id,batch_id,content_hash,observed_at,state)
                VALUES('wechat','moments',:external,:batch,:hash,now(),'changed') ON CONFLICT DO NOTHING"""),
                {'external': post['external_id'], 'batch': batch, 'hash': digest})
        db.commit()
        report['updated'] += len(valid)
        print(json.dumps(report), flush=True)
        time.sleep(.05)
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--sns-db', required=True)
    parser.add_argument('--profile-id')
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    with SessionLocal() as db:
        print(json.dumps(repair(db, args.sns_db, apply=args.apply, profile_id=args.profile_id)), flush=True)
