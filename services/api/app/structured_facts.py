"""Keep simultaneous source entries separate without turning edits into new jobs."""
from collections import defaultdict
from hashlib import sha256

from sqlalchemy import text

from .content import canonical_json


def fingerprint(value):
    return sha256(canonical_json(value)).hexdigest()


def fact_key(source,record,kind,value):
    if isinstance(value,dict):
        stable=value.get('source_key') or value.get('id') or value.get('urn')
        if not stable:
            keys={'employment':('company_name','company','linkedin_url','start_date','from_date'),
                  'education':('school_name','school','linkedin_url','degree','start_date','from_date'),
                  'address':('label','type','address','city')}[kind]
            stable=fingerprint({k:value[k] for k in keys if value.get(k)} or value)
    else:stable=str(value)
    return f'{source}:{record}:{stable}'


def _label(value,kind):
    if not isinstance(value,dict):return None
    keys={'employment':('title',),'education':('degree','field_of_study'),'address':('label','type')}[kind]
    return tuple(str(value.get(k) or '').strip() for k in keys)


def collection_keys(db,*,source,record,kind,values,account=None,profile=None):
    """An observed list proves coexistence; company and start month alone do not.

    Match exact current values first, then an unambiguous role label, then the
    sole remaining entry. Keep the original key for an existing entry and give
    new siblings deterministic keys. Reordering and description/date edits
    preserve those keys. Ambiguous multi-entry edits require source IDs.
    """
    table=f'profile_{kind}_revisions'
    if account:
        rows=db.execute(text(f'''SELECT f.fact_key,o.payload_json AS value FROM source_account_facts f
            JOIN {table} r ON r.id=f.revision_id JOIN content_objects o ON o.id=r.content_object_id
            WHERE f.account_id=:owner AND f.fact_type=:kind'''),{'owner':account,'kind':kind}).mappings()
    else:
        rows=db.execute(text(f'''SELECT f.fact_key,o.payload_json AS value FROM profile_structured_current f
            JOIN {table} r ON r.id=f.current_revision_id JOIN content_objects o ON o.id=r.content_object_id
            WHERE f.profile_id=:owner AND f.fact_type=:kind AND f.current_source_type=:source'''),
            {'owner':profile,'kind':kind,'source':source}).mappings()
    current={r['fact_key']:r['value'] for r in rows};groups=defaultdict(dict);result=[]
    for value in values:groups[fact_key(source,record,kind,value)][fingerprint(value)]=value
    for base,entries in groups.items():
        existing={k:v for k,v in current.items() if k==base or k.startswith(base+':entry:') or (
            k.startswith(f'{source}:{record}:') and fact_key(source,record,kind,v)==base)}
        # Earlier importer versions used whole-value hashes for their keys.
        # Reuse an exact archived entry instead of creating another copy.
        by_value={}
        for key,value in sorted(existing.items(),key=lambda item:(item[0]!=base,':entry:' not in item[0],item[0])):
            by_value.setdefault(fingerprint(value),(key,value))
        existing=dict(by_value.values())
        if len(entries)==1 and (not existing or set(existing)=={base}):
            result.append((next(iter(entries.values())),base));continue
        if any(isinstance(v,dict) and any(v.get(k) for k in ('source_key','id','urn')) for v in entries.values()):
            raise ValueError('Distinct simultaneous facts share one explicit source ID')
        remaining=dict(entries);assigned={}
        for digest,value in list(remaining.items()):
            matches=[k for k,v in existing.items() if fingerprint(v)==digest]
            if len(matches)==1:
                key=matches[0];assigned[digest]=key;existing.pop(key);remaining.pop(digest)
        for digest,value in list(remaining.items()):
            label=_label(value,kind)
            candidates=[k for k,v in existing.items() if label and _label(v,kind)==label]
            peers=[v for v in remaining.values() if _label(v,kind)==label]
            if len(candidates)==len(peers)==1:
                key=candidates[0];assigned[digest]=key;existing.pop(key);remaining.pop(digest)
        if len(remaining)==len(existing)==1:
            assigned[next(iter(remaining))]=next(iter(existing));remaining.clear();existing.clear()
        if remaining and existing:
            raise ValueError('Ambiguous simultaneous fact edits need explicit source IDs')
        for digest in remaining:
            assigned[digest]=base if base not in current and base not in assigned.values() else base+':entry:'+digest
        result.extend((value,assigned[digest]) for digest,value in entries.items())
    return result


def repair_linkedin_collections(db,archive,apply=False):
    """Repair only collisions proved by coexisting entries in an original export."""
    import json
    from datetime import UTC, datetime

    from .imports import _project_structured_fact
    from .platform_migrate import account_lookup
    from .profile_presentation import identity_key
    _,aliases,_=account_lookup(db,'linkedin')
    report={'collision_groups':0,'source_entries':0,'different_entries':0,'appended_entries':0,'unmapped_accounts':0}
    at=datetime.now(UTC)
    for item in json.loads(archive.read_text()).get('profiles',[]):
        external=str(item.get('provider_user_id') or item.get('profile_url') or '')
        aid=aliases.get(external) or aliases.get(identity_key('linkedin',external))
        for kind,field in [('employment','experiences'),('education','educations')]:
            groups=defaultdict(dict)
            for value in (item.get('profile') or {}).get(field) or []:
                groups[fact_key('linkedin',external,kind,value)][fingerprint(value)]=value
            collisions=[list(g.values()) for g in groups.values() if len(g)>1]
            if not collisions:continue
            if not aid:report['unmapped_accounts']+=1;continue
            if apply:
                db.execute(text('SELECT pg_advisory_xact_lock(11803295)'))
                db.execute(text('SELECT id FROM source_accounts WHERE id=:id FOR UPDATE'),{'id':aid})
            pid=db.execute(text('SELECT profile_id FROM source_account_links WHERE account_id=:id'),{'id':aid}).scalar_one_or_none()
            table=f'profile_{kind}_revisions'
            current={r.fact_key:fingerprint(r.value) for r in db.execute(text(f'''SELECT f.fact_key,o.payload_json AS value
                FROM source_account_facts f JOIN {table} r ON r.id=f.revision_id
                JOIN content_objects o ON o.id=r.content_object_id WHERE f.account_id=:id AND f.fact_type=:kind'''),{'id':aid,'kind':kind})}
            for values in collisions:
                report['collision_groups']+=1;report['source_entries']+=len(values)
                for value,key in collection_keys(db,source='linkedin',record=external,kind=kind,values=values,account=aid,profile=pid):
                    if current.get(key)==fingerprint(value):continue
                    report['different_entries']+=1
                    if apply:
                        _project_structured_fact(db,profile_id=pid,fact_type=kind,value=value,source_type='linkedin',
                            source_record_id=external,observed_at=at,source_account_id=aid,fact_key_override=key)
                        report['appended_entries']+=1
            if apply:db.commit()
    return report


if __name__=='__main__':
    import argparse
    import json
    from pathlib import Path

    from .db import SessionLocal
    parser=argparse.ArgumentParser(description='Audit simultaneous source careers; only --apply appends missing entries')
    parser.add_argument('--linkedin-export',type=Path,required=True)
    parser.add_argument('--apply',action='store_true')
    args=parser.parse_args()
    with SessionLocal() as db:print(json.dumps(repair_linkedin_collections(db,args.linkedin_export,args.apply)),flush=True)
