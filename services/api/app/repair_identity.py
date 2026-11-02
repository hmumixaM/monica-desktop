"""Repair only source-proven identities; preserve the original rows in repair_snapshots.

No names, signatures, counts or avatar similarities are used as matching evidence.
Run dry first, then --apply after examining ambiguous/skipped counts.
"""
import argparse
import json
from collections import defaultdict
from pathlib import Path
from uuid import uuid4

from sqlalchemy import text

from .db import SessionLocal
from .full_migration import find_file, json_load
from .identity_rules import monica_identities
from .merge_profiles import _merge_profile


def repair(db, root: Path, apply=False):
    data = json_load(find_file(root, 'chatlog_extract.json'))
    contacts = {str(c['username']): c for c in data['contacts'] if not str(c['username']).endswith('@chatroom')}
    rooms = {str(r.get('id') or r.get('name')): r for r in data['rooms']}
    canonical = defaultdict(set)
    aliases = {str(c.get('alias')): key for key, c in contacts.items() if c.get('alias')}
    for row in db.execute(text("""SELECT c.profile_id, co.payload_json AS value
        FROM profile_field_current c JOIN profile_field_revisions r ON r.id=c.current_revision_id
        JOIN content_objects co ON co.id=r.content_object_id JOIN profiles p ON p.id=c.profile_id
        WHERE c.field_key='monica.custom_fields' AND p.archived_at IS NULL""")).mappings():
        for provider, value, kind in monica_identities(row['value'] or {}):
            if provider=='wechat' and kind=='alias':
                value=aliases.get(value)
                if not value:
                    continue
            canonical[(provider,value)].add(row['profile_id'])
    identities = {(i['provider'],i['external_id']):dict(i) for i in db.execute(text('SELECT * FROM identities')).mappings()}
    profiles = {p['id']:dict(p) for p in db.execute(text('SELECT id,profile_type,archived_at FROM profiles')).mappings()}
    monica_ids={i['profile_id'] for i in identities.values() if i['provider']=='monica'}
    valid = {('wechat',key) for key in contacts} | {('wechat_group',key) for key in rooms}
    invalid=[i for key,i in identities.items() if i['provider'] in ('wechat','wechat_group') and key not in valid and i['status']!='invalid']
    confirmed_groups = {pid for (provider, _), ids in canonical.items() if provider == "wechat_group" for pid in ids}
    plan=[]; ambiguous=0; create=0
    for provider,values,kind in [('wechat',contacts,'person'),('wechat_group',rooms,'group')]:
        for ext,item in values.items():
            candidates=canonical.get((provider,ext),set())
            if len(candidates)>1:
                ambiguous+=1
                continue
            identity=identities.get((provider,ext))
            old=identity['profile_id'] if identity else None
            target=next(iter(candidates), None)
            if target is None and old and profiles[old]['profile_type']==kind and not profiles[old]['archived_at'] and not (kind=='person' and old in confirmed_groups):
                target=old
            if target is None:
                target=uuid4(); create+=1
            merge=bool(old and old!=target and old not in monica_ids and not profiles[old]['archived_at'] and profiles[old]['profile_type']==kind)
            plan.append((provider,ext,item,kind,old,target,merge))
    report={'source_people':len(contacts),'source_groups':len(rooms),'invalid_identities':len(invalid),
            'ambiguous_identifiers':ambiguous,'new_source_profiles':create,
            'confirmed_merges':sum(x[-1] for x in plan),
            'identity_reassignments':sum(x[4]!=x[5] for x in plan)}
    if not apply:
        return report
    def snapshot(table,key):
        db.execute(text(f"""INSERT INTO repair_snapshots(repair_key,entity_type,entity_key,payload)
            SELECT 'identity-v1',:table,{key},to_jsonb(t) FROM {table} t
            ON CONFLICT DO NOTHING"""), {'table':table})
    for table,key in [('identities','id::text'),('profiles','id::text'),('group_memberships',"group_profile_id::text||':'||person_profile_id::text")]:
        snapshot(table,key)
    # Preserve valid membership events on reruns; quarantine only illegal types.
    db.execute(text("DELETE FROM group_memberships WHERE group_profile_id=person_profile_id OR NOT EXISTS (SELECT 1 FROM profiles p WHERE p.id=group_profile_id AND p.profile_type='group') OR NOT EXISTS (SELECT 1 FROM profiles p WHERE p.id=person_profile_id AND p.profile_type='person')"))
    # A source-proven group can still have the old type 'person'. Its existing
    # person-side memberships look structurally valid until the type correction.
    # Snapshot above preserves them; remove those obsolete endpoints first and
    # rebuild only source-proven memberships after applying the confirmed types.
    changing=[target for _,_,_,kind,_,target,_ in plan
              if target in profiles and profiles[target]['profile_type']!=kind]
    if changing:
        db.execute(text("DELETE FROM group_memberships WHERE group_profile_id=ANY(CAST(:ids AS uuid[])) OR person_profile_id=ANY(CAST(:ids AS uuid[]))"),{'ids':changing})
    for identity in invalid:
        db.execute(text("UPDATE identities SET status='invalid' WHERE id=:id"),{'id':identity['id']})
    mapping={}; merged=set()
    for provider,ext,item,kind,old,target,merge in plan:
        if target not in profiles:
            db.execute(text("INSERT INTO profiles(id,profile_type,display_name) VALUES (:id,:type,:name)"),
                       {'id':target,'type':kind,'name':item.get('remark') or item.get('nick_name') or item.get('label') or ext})
            profiles[target]={'profile_type':kind,'archived_at':None}
        elif profiles[target]['profile_type']!=kind:
            db.execute(text('UPDATE profiles SET profile_type=:type WHERE id=:id'),{'id':target,'type':kind})
        if merge and old not in merged:
            _merge_profile(db,old,target); merged.add(old)
        db.execute(text("""INSERT INTO identities(provider,external_id,profile_id,username,status)
            VALUES (:provider,:ext,:target,:username,'active') ON CONFLICT(provider,external_id)
            DO UPDATE SET profile_id=EXCLUDED.profile_id,username=EXCLUDED.username,status='active'"""),
            {'provider':provider,'ext':ext,'target':target,'username':item.get('alias') or ext})
        evidence = {'kind':'monica-confirmed-identifier' if canonical.get((provider,ext)) else 'source-object', 'external_id':ext}
        db.execute(text("""INSERT INTO source_crosswalk(source,object_kind,external_id,profile_id,confirmation_status,evidence)
            VALUES (:source,:kind,:ext,:pid,:status,CAST(:evidence AS jsonb))
            ON CONFLICT(source,account_scope,object_kind,external_id) DO UPDATE SET
              profile_id=EXCLUDED.profile_id,confirmation_status=EXCLUDED.confirmation_status,evidence=EXCLUDED.evidence
            WHERE (source_crosswalk.profile_id,source_crosswalk.confirmation_status,source_crosswalk.evidence)
               IS DISTINCT FROM (EXCLUDED.profile_id,EXCLUDED.confirmation_status,EXCLUDED.evidence)"""),
            {'source':'wechat','kind':kind,'ext':ext,'pid':target,
             'status':'confirmed' if canonical.get((provider,ext)) else 'source_only','evidence':json.dumps(evidence)})
        mapping[(provider,ext)]=target
    # Correct source-owned child rows even when a polluted identity previously pointed at a group.
    snapshot('profile_field_current', "profile_id::text||':'||field_key")
    db.execute(text("""UPDATE profile_field_revisions r SET profile_id=i.profile_id
        FROM identities i WHERE r.source_type='wechat' AND r.source_record_id=i.external_id
          AND i.provider IN ('wechat','wechat_group') AND i.status='active'
          AND r.profile_id<>i.profile_id"""))
    db.execute(text("""DELETE FROM profile_field_current c USING profile_field_revisions r
        WHERE c.current_revision_id=r.id AND c.profile_id<>r.profile_id"""))
    db.execute(text("""INSERT INTO profile_field_current(profile_id,field_key,current_revision_id,
        current_content_hash,current_source_type,updated_at)
        SELECT DISTINCT ON (r.profile_id,r.field_key) r.profile_id,r.field_key,r.id,r.content_hash,r.source_type,r.observed_at
        FROM profile_field_revisions r JOIN profiles p ON p.id=r.profile_id AND p.archived_at IS NULL
        ORDER BY r.profile_id,r.field_key,CASE r.source_type WHEN 'manual' THEN 0 WHEN 'monica' THEN 1 ELSE 2 END,
                 r.revision_seq DESC ON CONFLICT(profile_id,field_key) DO NOTHING"""))
    db.execute(text("""UPDATE social_posts p SET profile_id=i.profile_id FROM content_objects co,identities i
        WHERE p.content_object_id=co.id AND p.provider='wechat' AND i.provider='wechat'
          AND i.external_id=co.payload_json->>'username' AND i.status='active'
          AND p.profile_id IS DISTINCT FROM i.profile_id"""))
    db.execute(text("""UPDATE social_interactions s SET author_profile_id=i.profile_id FROM identities i
        WHERE s.provider='wechat' AND i.provider='wechat' AND s.author_external_id=i.external_id
          AND i.status='active' AND s.author_profile_id IS DISTINCT FROM i.profile_id"""))
    db.execute(text("""UPDATE conversations c SET profile_id=i.profile_id FROM identities i
        WHERE c.external_id=CASE i.provider WHEN 'wechat' THEN 'direct:' ELSE 'group:' END||i.external_id
          AND i.provider IN ('wechat','wechat_group') AND i.status='active'
          AND c.profile_id IS DISTINCT FROM i.profile_id"""))
    members=0
    for ext,room in rooms.items():
        group=mapping.get(('wechat_group',ext))
        if not group:
            continue
        room_members=dict(room.get('members') or {})
        if room.get('owner'):
            room_members.setdefault(room['owner'],'')
        for wxid in room_members:
            person=mapping.get(('wechat',wxid))
            if not person:
                continue
            db.execute(text("""INSERT INTO group_memberships(group_profile_id,person_profile_id,role,source_record_id)
                VALUES (:g,:p,:role,:record) ON CONFLICT(group_profile_id,person_profile_id)
                DO UPDATE SET role=EXCLUDED.role,source_record_id=EXCLUDED.source_record_id,left_at=NULL"""),
                {'g':group,'p':person,'role':'owner' if wxid==room.get('owner') else 'member','record':f'chatlog:{ext}:{wxid}'})
            members+=1
    report['rebuilt_memberships']=members
    return report


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-root',type=Path,required=True)
    parser.add_argument('--apply',action='store_true')
    args=parser.parse_args()
    with SessionLocal() as db:
        report=repair(db,args.source_root,args.apply)
        if args.apply:
            db.commit()
        print(json.dumps(report,ensure_ascii=False),flush=True)


if __name__=='__main__':
    main()
