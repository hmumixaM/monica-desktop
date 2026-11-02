"""Source-specific, diff-only repairs. Never invoke the whole migration pipeline.

Each command reports its own source denominator and unprojected IDs. --apply
writes only the examined differences. --profile limits validation to one person.
"""
import argparse
import hashlib
import json
import sqlite3
import xml.etree.ElementTree as ET
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import text

from .archive_media import link, register_file
from .bulk_moments import project_moments
from .content import canonical_json, content_hash
from .db import SessionLocal
from .imports import project_batch
from .moments_posts import moments_record
from .schemas import ImportBatchRequest


def identities(db, provider):
    return {r.external_id: str(r.profile_id) for r in db.execute(text("""
        SELECT i.external_id,i.profile_id FROM identities i JOIN profiles p ON p.id=i.profile_id
        WHERE i.provider=:provider AND i.status NOT IN ('invalid','merged') AND p.archived_at IS NULL
        AND p.profile_type=CASE WHEN :provider='wechat_group' THEN 'group' ELSE 'person' END
    """), {'provider': provider})}


def project_differences(db, provider, records, apply, scope=None):
    current = {r.external_id: (r.current_content_hash, str(r.profile_id), r.occurred_at)
               for r in db.execute(text('SELECT external_id,current_content_hash,profile_id,occurred_at FROM social_posts WHERE provider=:p'), {'p': provider})}
    differences = [r for r in records if (r['external_id'] not in current or
        current[r['external_id']][0] != content_hash(r['content']) or
        current[r['external_id']][1] != str(r.get('profile_id')) or
        (r.get('occurred_at') and current[r['external_id']][2] is None))]
    report = {'source_objects': len(records), 'already_equal': len(records)-len(differences),
              'differences': len(differences), 'missing_profile': sum(not r.get('profile_id') for r in records)}
    if apply:
        run_id=__import__('uuid').uuid4().hex
        for start in range(0, len(differences), 200):
            part = differences[start:start+200]
            digest = content_hash(part)
            request = ImportBatchRequest(source=provider, stream='moments' if provider=='wechat' else 'posts',
                batch_id=f'reconcile-{digest}-{run_id}', idempotency_key=f'{provider}:reconcile:{digest}:{run_id}',
                observed_at=datetime.now(UTC), records=part)
            if provider=='wechat':
                project_moments(db, request)
            else:
                project_batch(db, request)
            db.commit()
            from .runtime_status import progress
            progress(stage='projecting', processed=start+len(part), total=len(differences))
            print(json.dumps({'projected_differences': start+len(part), 'of': len(differences)}), flush=True)
    if apply:
        db.execute(text("""INSERT INTO source_reconciliation(source,stream,source_digest,source_count,projected_count,stage,details)
            VALUES (:source,:stream,:digest,:count,:count,'projected',CAST(:details AS jsonb))
            ON CONFLICT(source,stream) DO UPDATE SET source_digest=EXCLUDED.source_digest,source_count=EXCLUDED.source_count,
            projected_count=EXCLUDED.projected_count,stage=EXCLUDED.stage,details=EXCLUDED.details,checked_at=now()"""),
            {'source':provider,'stream':f'posts:{scope}' if scope else 'posts','digest':content_hash(records),'count':len(records),'details':json.dumps(report)})
        db.commit()
    return report


def moment_fingerprints(db, root):
    """Raw XML hash and resolved author per post: unchanged pairs need no parse."""
    mapping = identities(db, 'wechat')
    from .jobs import ShutdownRequested
    from .runtime_status import STOP, progress

    fingerprints = {}
    with closing(sqlite3.connect(f'file:{root}?mode=ro', uri=True)) as source:
        for index, (tid, username, xml) in enumerate(source.execute('SELECT tid,user_name,content FROM SnsTimeLine'), 1):
            if STOP.is_set():
                raise ShutdownRequested('Stopped while examining Moments at snapshot checkpoint')
            fingerprints[str(tid)] = [hashlib.sha256((xml or '').encode()).hexdigest(), mapping.get(username)]
            if index % 1000 == 0:
                progress(stage='fingerprints', processed=index)
    return fingerprints


def changed_moments(previous, current):
    return {tid for tid, fingerprint in current.items() if previous.get(tid) != fingerprint}


def moments(db, root, profile=None, apply=False, tids=None):
    """`tids` limits parsing and projection to posts whose fingerprint changed."""
    mapping = identities(db, 'wechat')
    records=[]; invalid=0; total=0; recovered=0
    with closing(sqlite3.connect(f'file:{root}?mode=ro', uri=True)) as source:
        for tid, username, xml in source.execute('SELECT tid,user_name,content FROM SnsTimeLine'):
            total+=1
            if profile and mapping.get(username)!=profile:
                continue
            if tids is not None and str(tid) not in tids:
                continue
            try:
                record=moments_record(tid,username,xml,mapping.get(username))
                if record is None:
                    invalid+=1;continue
                recovered+=record.pop('repairs')
                records.append(record)
            except ET.ParseError:
                invalid+=1
    report={'source_rows':total,'invalid_xml':invalid,'recovered_xml':recovered,**project_differences(db,'wechat',records,apply,profile or ('incremental' if tids is not None else None))}
    if tids is not None:
        report['examined_changed_posts']=len(records)
    if apply and not profile and tids is None:
        db.execute(text("""UPDATE source_reconciliation SET source_count=:total,excluded_count=:invalid,
            missing_ids=CAST(:missing AS jsonb),details=CAST(:details AS jsonb) WHERE source='wechat' AND stream='posts'"""),
            {'total':total,'invalid':invalid,'missing':json.dumps([r['external_id'] for r in records if not r['profile_id']]),
             'details':json.dumps({**report,'excluded_reason':'XML has no parseable TimelineObject','missing_ids_meaning':'post exists but author identity remains unresolved'})})
        db.commit()
    return report


def instagram_location(node):
    location=node.get('location') or (node.get('iphone_struct') or {}).get('location')
    if not isinstance(location,dict):return None
    return {k:v for k,v in location.items() if v is not None and k!='profile_pic_url'}


def instagram_locations(db,root,profile=None,apply=False):
    current={r['external_id']:dict(r) for r in db.execute(text("""SELECT p.external_id,p.profile_id,p.occurred_at,
        a.external_id AS account,o.payload_json AS content FROM social_posts p
        JOIN content_objects o ON o.id=p.content_object_id LEFT JOIN source_accounts a ON a.id=p.source_account_id
        WHERE p.provider='instagram'""")).mappings()}
    locations={};files=0;missing=[];differences=[]
    for path in sorted(root.glob('*/*_UTC_*.json')):
        node=json.loads(path.read_text()).get('node',{});location=instagram_location(node)
        if not location:continue
        files+=1;locations[str(node.get('id') or node.get('shortcode'))]=(location,node.get('owner') or {})
    for external,(location,owner) in locations.items():
        row=current.get(external)
        if not row:missing.append(external);continue
        if profile and str(row['profile_id'])!=profile:continue
        if row['content'].get('location')==location:continue
        account=row['account'] or str(owner.get('id') or owner.get('username') or '')
        if not account:missing.append(external);continue
        differences.append({'external_id':external,'profile_id':row['profile_id'],'source_account_external_id':account,
            'entity_type':'post','occurred_at':row['occurred_at'],'content':{**row['content'],'location':location}})
    if apply:
        run_id=__import__('uuid').uuid4().hex
        for offset in range(0,len(differences),100):
            part=differences[offset:offset+100];key=f'instagram-location:{run_id}:{offset}'
            project_batch(db,ImportBatchRequest(source='instagram',stream='posts',batch_id=key,idempotency_key=key,
                observed_at=datetime.now(UTC),records=part));db.commit()
            print(json.dumps({'locations_updated':offset+len(part),'of':len(differences)}),flush=True)
    return {'source_files_with_location':files,'unique_source_locations':len(locations),'changed_posts':len(differences),'unresolved_ids':missing}


def instagram(db, root, profile=None, apply=False):
    from .source_projection import enabled
    independent=enabled(db,'instagram')
    mapping=identities(db,'instagram'); records={}; missing=[]; total=0; source_only=set(); missing_owners=set()
    for path in sorted(root.glob('*/*_UTC_*.json')):
        from .instagram_posts import read_post
        node,content=read_post(path)
        external=str(node.get('id') or node.get('shortcode') or '')
        if not external:continue
        total+=1
        owner=node.get('owner') or {}
        profile_id=mapping.get(str(owner.get('id'))) or mapping.get('username:'+str(owner.get('username'))) or mapping.get(str(owner.get('username')))
        if profile and profile_id!=profile:continue
        if not profile_id and independent:
            missing_owners.add(str(owner.get('id') or owner.get('username') or external))
        if not profile_id and not independent:
            account=str(owner.get('id') or owner.get('username') or '')
            if not account:
                missing.append(external);continue
            source_only.add(account)
            if not apply:
                missing.append(external);continue
            profile_id=str(db.execute(text("INSERT INTO profiles(profile_type,display_name) VALUES('person',:name) RETURNING id"),
                {'name':owner.get('full_name') or owner.get('username') or account}).scalar_one())
            db.execute(text("""INSERT INTO identities(provider,external_id,profile_id,username,status)
                VALUES('instagram',:account,:profile,:username,'pending')"""),
                {'account':account,'profile':profile_id,'username':owner.get('username')})
            mapping[account]=profile_id
            db.execute(text("""INSERT INTO source_crosswalk(source,object_kind,external_id,profile_id,confirmation_status,evidence)
                VALUES('instagram','person',:account,:profile,'source_only','{"kind":"archive-owner-id"}') ON CONFLICT DO NOTHING"""),
                {'account':account,'profile':profile_id})
            # Commit the complete source-only identity before reading the next
            # archive file. Slow NAS file reads must not hold profile FK locks.
            db.commit()
        records[external]={'external_id':external,'profile_id':profile_id,'entity_type':'post',
            'source_account_external_id':str(owner.get('id') or owner.get('username') or ''),
            'occurred_at':content['published_at'],'content':content}
    report={'archive_files':total,'source_only_accounts':len(source_only),'excluded_unmapped':len(missing),'unmapped_ids':missing,
            'missing_owner_ids':sorted(missing_owners),
            **project_differences(db,'instagram',list(records.values()),apply,profile)}
    if apply and not profile:
        db.execute(text("""UPDATE source_reconciliation SET source_count=:total,missing_ids=CAST(:missing AS jsonb),
            details=CAST(:details AS jsonb) WHERE source='instagram' AND stream='posts'"""),
            {'total':len(records)+len(set(missing)),'missing':json.dumps(missing),
             'details':json.dumps({**report,'duplicate_archive_files':total-len(records)-len(missing)})})
        db.commit()
    return report


def instagram_profiles(db, root, profile=None, apply=False):
    from .source_projection import enabled
    independent=enabled(db,'instagram');aliases={};links={}
    if independent:
        from .platform_migrate import account_lookup
        _,aliases,_=account_lookup(db,'instagram')
        links={str(r.account_id):str(r.profile_id) for r in db.execute(text('SELECT account_id,profile_id FROM source_account_links'))}
    mapping=identities(db,'instagram'); differences=[];missing=[];total=0;snapshots=[]
    current={(str(r.profile_id),r.field_key):canonical_json(r.value) for r in db.execute(text("""SELECT f.profile_id,f.field_key,o.payload_json AS value
        FROM profile_field_current f JOIN profile_field_revisions r ON r.id=f.current_revision_id
        JOIN content_objects o ON o.id=r.content_object_id WHERE f.field_key LIKE 'instagram.%'"""))}
    if independent:
        current={(str(r.account_id),r.field_key):canonical_json(r.value) for r in db.execute(text('''SELECT f.account_id,f.field_key,o.payload_json AS value
            FROM source_account_fields f JOIN profile_field_revisions r ON r.id=f.revision_id
            JOIN content_objects o ON o.id=r.content_object_id'''))}
    for path in sorted(root.glob('*/profile.json')):
        node=json.loads(path.read_text()); total+=1;snapshots.append(node)
        account=str(node.get('userid') or node.get('id') or '')
        username=str(node.get('username') or path.parent.name)
        candidates={mapping[k] for k in (account,'username:'+username,username) if k in mapping}
        aid=next((aliases[k] for k in (account,'username:'+username,username) if k in aliases),None)
        if independent:
            pid=links.get(aid)
        elif len(candidates)==1:
            pid=candidates.pop()
        else:
            missing.append(account or username);continue
        if profile and pid!=profile:continue
        values={'instagram.biography':node.get('biography'), 'instagram.full_name':node.get('full_name'),
            'instagram.profile_url':f'https://www.instagram.com/{username}/',
            'instagram.profile_pic_url':node.get('profile_pic_url_hd') or node.get('profile_pic_url'),
            'instagram.followers_count':node.get('followers'), 'instagram.followees_count':node.get('followees'),
            'instagram.media_count':node.get('mediacount'), 'instagram.external_url':node.get('external_url'),
            'instagram.is_private':node.get('is_private'), 'instagram.is_verified':node.get('is_verified')}
        scoped={f'instagram.accounts.{account or username}.{k.split(".",1)[1]}':v for k,v in values.items()}
        scoped[f'instagram.accounts.{account or username}.username']=username
        fields={k:v for k,v in scoped.items() if v is not None and current.get((aid if independent else pid,k))!=canonical_json(v)}
        if fields:differences.append({'external_id':account or 'username:'+username,'profile_id':pid,
            'content':{'fields':fields,'display_name':node.get('full_name') or username,
                       'username':username,'avatar_url':values['instagram.profile_pic_url']}})
    if apply:
        run_id=__import__('uuid').uuid4().hex
        for offset in range(0,len(differences),50):
            records=differences[offset:offset+50]
            digest=content_hash(records)+':'+run_id
            project_batch(db,ImportBatchRequest(source='instagram',stream='profiles',batch_id=f'profile-fields-{digest}',
                idempotency_key=f'instagram:profile-fields:{digest}',observed_at=datetime.now(UTC),records=records))
            db.commit()
            print(json.dumps({'profile_fields_updated':offset+len(records),'of':len(differences)}),flush=True)
        db.execute(text("""INSERT INTO source_reconciliation(source,stream,source_digest,source_count,projected_count,stage,missing_ids,details)
            VALUES ('instagram','profile-details',:digest,:total,:projected,'projected',CAST(:missing AS jsonb),CAST(:details AS jsonb))
            ON CONFLICT(source,stream) DO UPDATE SET source_count=EXCLUDED.source_count,projected_count=EXCLUDED.projected_count,
              missing_ids=EXCLUDED.missing_ids,details=EXCLUDED.details,checked_at=now()"""),
            {'digest':content_hash(snapshots),'total':total,'projected':total-len(missing),'missing':json.dumps(missing),'details':json.dumps({'changed_profiles':len(differences),'scope':profile})})
        db.commit()
    return {'source_profiles':total,'changed_profiles':len(differences),'unmapped_or_ambiguous':len(missing),'missing_ids':missing}


def avatars(db, root, media_root, profile=None, apply=False):
    from .source_projection import enabled
    independent=enabled(db,'wechat');accounts={}
    mapping={**identities(db,'wechat'),**identities(db,'wechat_group')}
    if independent:
        from .platform_migrate import account_lookup
        _,accounts,_=account_lookup(db,'wechat')
        linked={str(r.account_id):str(r.profile_id) for r in db.execute(text('SELECT account_id,profile_id FROM source_account_links'))}
        mapping={external:linked.get(str(aid)) for external,aid in accounts.items()}
    covers=json.loads((root/'covers/manifest.json').read_text())
    avatar_files={}
    for directory in ('avatars','avatars-group'):
        if (root/directory).is_dir():
            for path in sorted((root/directory).iterdir()):
                if path.suffix.lower() in {'.jpg','.png','.jpeg'}:avatar_files.setdefault(path.stem,path)
    report={'avatar_files':0,'cover_files':0,'missing_avatar_files':0,'linked':0}
    for external, pid in mapping.items():
        if profile and profile!=pid:continue
        avatar=avatar_files.get(external)
        if avatar:report['avatar_files']+=1
        else:report['missing_avatar_files']+=1
        entry=covers.get(external,{})
        cover=root/'covers'/entry.get('file','__missing__')
        if cover.is_file():report['cover_files']+=1
        for path,role in [(avatar,'wechat_avatar'),(cover if cover.is_file() else None,'cover')]:
            if path and apply:
                asset=register_file(db,path,media_root,'wechat')
                link(db,asset,accounts[external] if independent else pid,role,entity_type='source_account' if independent else 'profile')
                if not independent and role=='wechat_avatar' and asset:
                    db.execute(text("""UPDATE profiles SET avatar_media_id=:asset WHERE id=:id AND
                        (avatar_media_id IS NULL OR EXISTS (SELECT 1 FROM media_assets a
                          WHERE a.id=profiles.avatar_media_id AND a.source_type='wechat'))"""),{'asset':asset,'id':pid})
                report['linked']+=bool(asset)
    if apply:db.commit()
    return report


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('source',choices=['moments','instagram','instagram_profiles','instagram_locations','avatars']);p.add_argument('--root',type=Path,required=True);p.add_argument('--media-root',type=Path,default=Path('/data/media'));p.add_argument('--profile');p.add_argument('--apply',action='store_true');p.add_argument('--report',type=Path);args=p.parse_args()
    with SessionLocal() as db:
        if args.source=='avatars':report=avatars(db,args.root,args.media_root,args.profile,args.apply)
        else:report=globals()[args.source](db,args.root,args.profile,args.apply)
        if args.report:args.report.write_text(json.dumps(report,ensure_ascii=False,indent=2))
        print(json.dumps({k:v for k,v in report.items() if not isinstance(v,list)},ensure_ascii=False),flush=True)


if __name__=='__main__':main()
