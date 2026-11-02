"""Reconcile Monica notes/fields and source-proven tags without reimporting feeds.

Only existing exact Monica contact IDs and WeChat account IDs are eligible.
Unmapped objects remain in the source export and are reported, never name-matched.
"""
import argparse
import json
from collections import defaultdict
from pathlib import Path

from sqlalchemy import text

from .content import content_hash
from .db import SessionLocal
from .full_migration import add_activity
from .repositories import append_field_revision
from .source_time import iso


def reconcile(db, export, wechat_tags, apply=False):
    tables=defaultdict(list)
    for line in export.read_text().splitlines():
        item=json.loads(line)
        tables[item['table']].append(item['row'])
    required={'contacts','contact_field_types','contact_fields','tags','contact_tag'}
    if not required.issubset(tables):
        raise ValueError('Incomplete notebook export; refusing to reconcile tags')
    manifests=tables.get('export_manifest',[])
    if len(manifests)!=1 or any(len(tables.get(table,[]))!=count for table,count in manifests[0]['counts'].items()):
        raise ValueError('Notebook export is truncated or changed during export')
    mapping={(r.provider,r.external_id):str(r.profile_id) for r in db.execute(text("""
        SELECT i.provider,i.external_id,i.profile_id FROM identities i JOIN profiles p ON p.id=i.profile_id
        WHERE i.provider IN ('monica','wechat') AND i.status NOT IN ('invalid','merged') AND p.archived_at IS NULL
        AND (i.provider='monica' OR p.profile_type='person')"""))}
    field_types={str(r['id']):r['name'] for r in tables['contact_field_types']}
    custom=defaultdict(lambda:defaultdict(list))
    for row in tables['contact_fields']:
        key=field_types.get(str(row.get('contact_field_type_id',row.get('type_id'))))
        if key:custom[str(row['contact_id'])][key].append(row['data'])
    current={(str(r.profile_id),r.field_key):r.current_content_hash for r in db.execute(text('SELECT profile_id,field_key,current_content_hash FROM profile_field_current'))}
    fields=[];unmapped=[]
    for row in tables['contacts']:
        cid=str(row['id']);pid=mapping.get(('monica','contact:'+cid))
        if not pid:
            unmapped.append(cid);continue
        values={'monica.custom_fields':dict(custom[cid])}
        for key in ('nickname','food_preferences','first_met_where','first_met_additional_info','job','company'):
            if row.get(key):values['monica.'+key]=row[key]
        if row.get('description'):values['biography']=row['description']
        for key,value in values.items():
            if current.get((pid,key))!=content_hash(value):
                fields.append((pid,key,value,'contact:'+cid,iso(row.get('updated_at'))))
    tags={str(r['id']):r['name'] for r in tables['tags']}
    wanted={};missing_tags=[]
    for row in tables['contact_tag']:
        cid=str(row['contact_id']);pid=mapping.get(('monica','contact:'+cid));name=tags.get(str(row['tag_id']))
        if pid and name:wanted[(pid,name)]='monica'
        else:missing_tags.append(f"{cid}:{row['tag_id']}")
    source_tags=json.loads(wechat_tags.read_text()).get('tags')
    if not isinstance(source_tags,dict):raise TypeError('Invalid WeChat tag export')
    missing_wechat=[]
    for wxid,names in source_tags.items():
        pid=mapping.get(('wechat',wxid))
        if not pid:
            missing_wechat.append(wxid);continue
        for name in names:wanted.setdefault((pid,name),'wechat')
    existing={(str(r.profile_id),r.name):dict(r) for r in db.execute(text("""
        SELECT m.*,t.name FROM tag_memberships m JOIN tags t ON t.id=m.tag_id""")).mappings()}
    additions={k:v for k,v in wanted.items() if k not in existing}
    removals={k:v for k,v in existing.items() if v['source_type'] in ('monica','wechat') and k not in wanted}
    report={'contacts':len(tables['contacts']),'contact_fields':len(tables['contact_fields']),
            'field_differences':len(fields),'unmapped_contacts':unmapped,
            'monica_tag_links':len(tables['contact_tag']),'wechat_tag_accounts':len(source_tags),
            'resolved_tag_links':len(wanted),'tags_added':len(additions),'tags_quarantined':len(removals),
            'unmapped_monica_tags':missing_tags,'unmapped_wechat_tag_accounts':missing_wechat,
            'notes':len(tables['notes'])}
    if not apply:return report
    for pid,key,value,record,at in fields:
        append_field_revision(db,profile_id=pid,field_key=key,value=value,source_type='monica',
                              operation='import',source_record_id=record,observed_at=at)
    # Only the tag projection is corrected; original memberships and all field
    # histories are retained as audit evidence. Manual tags are never removed.
    for (_,name),row in removals.items():
        args={'pid':row['profile_id'],'tag':row['tag_id'],'source':row['source_type']}
        db.execute(text("""INSERT INTO repair_snapshots(repair_key,entity_type,entity_key,payload)
            SELECT 'notebook-source-tags-v1','tag_membership',profile_id::text||':'||tag_id::text,to_jsonb(m)
            FROM tag_memberships m WHERE profile_id=:pid AND tag_id=:tag ON CONFLICT DO NOTHING"""),args)
        db.execute(text("""INSERT INTO tag_membership_revisions(tag_id,profile_id,source_type,operation)
            VALUES(:tag,:pid,:source,'removed')"""),args)
        db.execute(text('DELETE FROM tag_memberships WHERE profile_id=:pid AND tag_id=:tag'),args)
    for (pid,name),source in additions.items():
        tag=db.execute(text('INSERT INTO tags(name) VALUES(:name) ON CONFLICT(name) DO UPDATE SET name=EXCLUDED.name RETURNING id'),{'name':name}).scalar_one()
        args={'tag':tag,'pid':pid,'source':source}
        if db.execute(text('SELECT 1 FROM tag_membership_removals WHERE tag_id=:tag AND profile_id=:pid'),args).first():
            continue
        db.execute(text('INSERT INTO tag_memberships(tag_id,profile_id,source_type) VALUES(:tag,:pid,:source)'),args)
        db.execute(text("""INSERT INTO tag_membership_revisions(tag_id,profile_id,source_type,operation)
            VALUES(:tag,:pid,:source,'added')"""),args)
    for row in tables['notes']:
        pid=mapping.get(('monica','contact:'+str(row['contact_id'])))
        if pid:
            add_activity(db,activity_type='note',occurred_at=row.get('created_at'),title=row.get('title') or 'Monica 笔记',
                         body=row.get('body') or row.get('content') or '',source_type='monica',
                         source_record_id='note:'+str(row['id']),profile_ids=[pid])
    db.execute(text("""INSERT INTO source_reconciliation(source,stream,source_digest,source_count,projected_count,stage,details)
        VALUES('monica','notebook',:digest,:count,:projected,'projected',CAST(:details AS jsonb))
        ON CONFLICT(source,stream) DO UPDATE SET source_digest=EXCLUDED.source_digest,source_count=EXCLUDED.source_count,
        projected_count=EXCLUDED.projected_count,details=EXCLUDED.details,checked_at=now()"""),
        {'digest':content_hash(tables),'count':len(tables['contacts']),'projected':len(tables['contacts'])-len(unmapped),'details':json.dumps(report)})
    db.commit()
    return report


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--export',type=Path,required=True)
    parser.add_argument('--wechat-tags',type=Path,required=True)
    parser.add_argument('--apply',action='store_true')
    args=parser.parse_args()
    with SessionLocal() as session:
        print(json.dumps(reconcile(session,args.export,args.wechat_tags,args.apply),ensure_ascii=False))
