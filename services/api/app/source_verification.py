"""Bounded, read-only checks before enabling source account reassignment."""
from hashlib import sha256

from sqlalchemy import text

from .content import canonical_json, content_hash
from .platform_store import object_id, platform_engine, resolve_message_records
from .source_time import message_key


def _hashes(db,source,sql,provider,kind,value_from_row=None):
    checked=0;mismatched=0;examples=[]
    rows=db.execute(text(sql),{'provider':provider},execution_options={'yield_per':1000}).mappings()
    for batch in rows.partitions(1000):
        # CRM hashes bare strings as UTF-8, while source payloads hash JSON.
        # Compare the canonical source representation, not incompatible hashes.
        def expected_hash(row):
            if value_from_row:return sha256(canonical_json(value_from_row(row))).hexdigest()
            return sha256(canonical_json(row['value'])).hexdigest() if 'value' in row else row['hash']
        if kind == 'message':
            valid = [dict(r, kind=kind, external_id=message_key(r['source_record_id'], r['occurred_at'])) for r in batch if r['account']]
            resolved = resolve_message_records(source, valid)
            batch = [{**r, 'external': r['external_id']} for r in resolved] + [r for r in batch if not r['account']]
        expected={str(object_id(r['account'],kind,r['external'])):expected_hash(r) for r in batch if r['account']}
        actual=dict(source.execute(text('SELECT id::text,current_hash FROM objects WHERE id=ANY(CAST(:ids AS uuid[]))'),{'ids':list(expected)}).all()) if expected else {}
        for row in batch:
            checked+=1
            oid=str(object_id(row['account'],kind,row['external'])) if row['account'] else None
            if oid is None or actual.get(oid)!=expected_hash(row):
                mismatched+=1
                if len(examples)<20:examples.append(oid or str(row['external']))
    return {'checked':checked,'mismatched':mismatched,'example_ids':examples}


def projection_content(db,source,provider):
    checks={'fields':_hashes(db,source,"""SELECT f.account_id AS account,f.field_key AS external,o.payload_json AS value
        FROM source_account_fields f JOIN source_accounts a ON a.id=f.account_id
        JOIN profile_field_revisions r ON r.id=f.revision_id
        JOIN content_objects o ON o.id=r.content_object_id WHERE a.provider=:provider""",provider,'field')}
    checks['tags']=_hashes(db,source,"""SELECT t.account_id AS account,n.name AS external,n.name AS value
        FROM source_account_tags t JOIN source_accounts a ON a.id=t.account_id
        JOIN tags n ON n.id=t.tag_id WHERE a.provider=:provider""",provider,'tag')
    checks['relationships']=_hashes(db,source,"""SELECT e.from_account_id AS account,e.id::text AS external,
            jsonb_build_object('to_account_id',e.to_account_id::text,'type',e.relationship_type_id::text,
                'note',e.note,'evidence_kind',e.evidence_kind) AS value
        FROM source_relationship_edges e JOIN source_accounts a ON a.id=e.from_account_id
        WHERE a.provider=:provider""",provider,'relationship')
    for kind in ('address','employment','education'):
        checks[kind]=_hashes(db,source,f"""SELECT f.account_id AS account,f.fact_key AS external,o.payload_json AS value
            FROM source_account_facts f JOIN source_accounts a ON a.id=f.account_id
            JOIN profile_{kind}_revisions r ON r.id=f.revision_id
            JOIN content_objects o ON o.id=r.content_object_id
            WHERE a.provider=:provider AND f.fact_type='{kind}'""",provider,kind)
    if provider=='wechat':
        # Preserve the exact legacy representation, including unknown roles
        # and Python's timezone-aware datetime format used by the migration.
        checks['memberships']=_hashes(db,source,"""SELECT group_account_id AS account,person_account_id::text AS external,
            group_account_id AS "group",person_account_id AS person,role,joined_at AS joined,
            left_at AS "left",source_record_id AS record FROM source_group_memberships""",provider,'membership',
            lambda row:{key:str(row[key]) if row[key] is not None else None
                        for key in ('group','person','role','joined','left','record')})
        # Source-native identity is room + stable message ID + original time.
        # Never use display names or a total count as proof of message parity.
        checks['messages']=_hashes(db,source,"""SELECT c.source_account_id AS account,
            m.source_record_id,m.occurred_at,'' AS external,
            o.sha256 AS hash
            FROM message_events m JOIN conversations c ON c.id=m.conversation_id
            JOIN content_objects o ON o.id=m.content_object_id""",provider,'message')
    checks['media']=_media(db,source,provider)
    return checks


def _media(db,source,provider):
    checked=0;mismatched=0;examples=[]
    # Sources retain old media versions, so the current projection must be a
    # subset of their links. Extra immutable historical files are expected.
    rows=db.execute(text("""SELECT p.source_account_id AS account,'post' AS kind,p.external_id AS external,
            m.sha256,l.role,m.object_path,m.byte_length,m.media_type
        FROM social_posts p JOIN media_links l ON l.entity_type='social_post' AND l.entity_id=p.id
        JOIN media_assets m ON m.id=l.media_id WHERE p.provider=:provider
        UNION ALL
        SELECT l.account_id,'profile_media',l.role,m.sha256,l.role,m.object_path,m.byte_length,m.media_type
        FROM source_account_media l JOIN source_accounts a ON a.id=l.account_id
        JOIN media_assets m ON m.id=l.media_id WHERE a.provider=:provider"""),{'provider':provider},execution_options={'yield_per':1000}).mappings()
    for batch in rows.partitions(1000):
        ids={str(object_id(r['account'],r['kind'],r['external'])) for r in batch if r['account']}
        actual={(str(r.object_id),r.media_hash,r.role):(r.object_path,r.byte_length,r.media_type)
            for r in source.execute(text("""SELECT l.*,m.object_path,m.byte_length,m.media_type FROM media_links l
                JOIN media m ON m.sha256=l.media_hash WHERE l.object_id=ANY(CAST(:ids AS uuid[]))"""),{'ids':list(ids)})} if ids else {}
        for row in batch:
            checked+=1
            oid=str(object_id(row['account'],row['kind'],row['external'])) if row['account'] else None
            if actual.get((oid,row['sha256'],row['role']))!=(row['object_path'],row['byte_length'],row['media_type']):
                mismatched+=1
                if len(examples)<20:examples.append({'object_id':oid,'sha256':row['sha256'],'role':row['role']})
    return {'checked':checked,'mismatched':mismatched,'examples':examples}


def repair_field_types(db,provider,apply=False):
    """Append only proven legacy scalar hash collisions; leave other differences for review."""
    from .repositories import append_field_revision
    rows=db.execute(text("""SELECT f.account_id,f.field_key,f.revision_id,r.content_hash,r.source_record_id,o.payload_json AS value,
            l.profile_id FROM source_account_fields f JOIN source_accounts a ON a.id=f.account_id
        JOIN profile_field_revisions r ON r.id=f.revision_id JOIN content_objects o ON o.id=r.content_object_id
        LEFT JOIN source_account_links l ON l.account_id=f.account_id
        WHERE a.provider=:provider AND jsonb_typeof(o.payload_json) IN ('string','number','boolean','null')"""),{'provider':provider}).mappings().all()
    changes=[];other=0
    with platform_engine(provider).connect() as source:
        for offset in range(0,len(rows),1000):
            batch=rows[offset:offset+1000]
            ids=[str(object_id(r['account_id'],'field',r['field_key'])) for r in batch]
            originals={str(r.id):r for r in source.execute(text("""SELECT o.id,o.revision_number,p.value,r.observed_at FROM objects o
                JOIN payloads p ON p.sha256=o.current_hash JOIN revisions r ON r.object_id=o.id AND r.revision_number=o.revision_number
                WHERE o.id=ANY(CAST(:ids AS uuid[]))"""),{'ids':ids})}
            for row,oid in zip(batch,ids):
                original=originals.get(oid)
                if original is None or canonical_json(original.value)==canonical_json(row['value']):continue
                if content_hash(original.value)!=row['content_hash']:
                    other+=1;continue
                changes.append({**row,'source_value':original.value,'source_observed_at':original.observed_at,'source_revision':original.revision_number})
    report={'provider':provider,'checked':len(rows),'typed_collisions':len(changes),'other_differences':other,'appended_revisions':0,'changed_during_audit':0}
    if apply:
        # Reuse the normal conflict policy: manual values remain primary and
        # the source's previous revisions stay intact. Never rewrite payloads.
        db.execute(text('SELECT pg_advisory_xact_lock(11803295)'))
        with platform_engine(provider).connect() as source:
            for row in sorted(changes,key=lambda r:(str(r['account_id']),r['field_key'])):
                db.execute(text('SELECT id FROM source_accounts WHERE id=:id FOR UPDATE'),{'id':row['account_id']})
                current=db.execute(text('SELECT revision_id FROM source_account_fields WHERE account_id=:id AND field_key=:field'),{'id':row['account_id'],'field':row['field_key']}).scalar_one_or_none()
                latest=source.execute(text('SELECT revision_number FROM objects WHERE id=:id'),{'id':object_id(row['account_id'],'field',row['field_key'])}).scalar_one_or_none()
                if current!=row['revision_id'] or latest!=row['source_revision']:
                    report['changed_during_audit']+=1;continue
                pid=db.execute(text('SELECT profile_id FROM source_account_links WHERE account_id=:id'),{'id':row['account_id']}).scalar_one_or_none()
                _,added=append_field_revision(db,profile_id=pid,field_key=row['field_key'],value=row['source_value'],source_type=provider,
                    source_record_id=row['source_record_id'],operation='import',observed_at=row['source_observed_at'],source_account_id=row['account_id'])
                report['appended_revisions']+=int(added)
        db.commit()
    return report


def repair_legacy_fact_order(db,provider,apply=False):
    """Correct only a proven timestamp tie formerly ordered by random UUID."""
    from .platform_store import observe
    changes=[]
    with platform_engine(provider).connect() as source:
        for kind in ('address','employment','education'):
            table=f'profile_{kind}_revisions'
            rows=db.execute(text(f"""SELECT f.account_id,r.*,c.payload_json AS value
                FROM source_account_facts f JOIN source_accounts a ON a.id=f.account_id
                JOIN {table} r ON r.id=f.revision_id JOIN content_objects c ON c.id=r.content_object_id
                WHERE a.provider=:provider AND f.fact_type=:kind
                AND NOT EXISTS(SELECT 1 FROM {table} later WHERE later.source_account_id=f.account_id
                    AND later.fact_key=f.fact_key AND later.revision_seq>r.revision_seq)"""),{'provider':provider,'kind':kind}).mappings().all()
            for row in rows:
                oid=object_id(row['account_id'],kind,row['fact_key'])
                current=source.execute(text("""SELECT o.revision_number,p.value,r.source_event FROM objects o
                    JOIN payloads p ON p.sha256=o.current_hash
                    JOIN revisions r ON r.object_id=o.id AND r.revision_number=o.revision_number
                    WHERE o.id=:id"""),{'id':oid}).mappings().one_or_none()
                if not current or canonical_json(current['value'])==canonical_json(row['value']):continue
                if not current['source_event'].startswith('legacy-fact:'):continue
                old=db.execute(text(f"""SELECT revision_seq,observed_at FROM {table} WHERE id=:id
                    AND source_account_id=:account AND fact_key=:key"""),{'id':current['source_event'].removeprefix('legacy-fact:'),
                    'account':row['account_id'],'key':row['fact_key']}).mappings().one_or_none()
                if not old or old['observed_at']!=row['observed_at'] or old['revision_seq']>=row['revision_seq']:continue
                changes.append({**row,'kind':kind,'object_id':oid,'source_revision':current['revision_number']})
    report={'provider':provider,'tied_legacy_currents':len(changes),'source_corrections':0,'changed_during_audit':0}
    if apply:
        db.execute(text('SELECT pg_advisory_xact_lock(11803295)'))
        with platform_engine(provider).begin() as source:
            for row in sorted(changes,key=lambda r:(str(r['account_id']),r['kind'])):
                db.execute(text('SELECT id FROM source_accounts WHERE id=:id FOR UPDATE'),{'id':row['account_id']})
                pointer=db.execute(text('SELECT revision_id FROM source_account_facts WHERE account_id=:id AND fact_type=:kind AND fact_key=:key'),
                    {'id':row['account_id'],'kind':row['kind'],'key':row['fact_key']}).scalar_one_or_none()
                latest=source.execute(text('SELECT revision_number FROM objects WHERE id=:id'),{'id':row['object_id']}).scalar_one_or_none()
                if pointer!=row['id'] or latest!=row['source_revision']:
                    report['changed_during_audit']+=1;continue
                observe(source,row['account_id'],row['kind'],row['fact_key'],row['value'],
                    source_event='repair-legacy-order:'+str(row['id']),observed_at=row['observed_at'])
                report['source_corrections']+=1
        db.commit()
    return report


if __name__=='__main__':
    import argparse
    import json

    from .db import SessionLocal
    from .platform_store import PROVIDERS
    parser=argparse.ArgumentParser(description='Audit legacy field JSON types; --apply appends only proven hash-collision corrections')
    parser.add_argument('action',nargs='?',default='fields',choices=['fields','facts'])
    parser.add_argument('--provider',choices=PROVIDERS,required=True)
    parser.add_argument('--apply',action='store_true')
    args=parser.parse_args()
    repair=repair_field_types if args.action=='fields' else repair_legacy_fact_order
    with SessionLocal() as db:print(json.dumps(repair(db,args.provider,args.apply)),flush=True)
