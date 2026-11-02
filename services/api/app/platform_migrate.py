"""Resumable source-store migration; does not rerun the legacy full importer.

Accounts use exact source identifiers. CRM ownership remains a reversible link.
"""
import argparse
import json
import re
import signal
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from uuid import UUID

from sqlalchemy import text

from .db import SessionLocal, engine
from .platform_store import (
    PROVIDERS,
    account_id,
    ensure_account,
    object_id,
    observe,
    observe_immutable_many,
    observe_many,
    platform_engine,
    resolve_message_records,
)
from .profile_presentation import identity_key
from .source_time import message_key


def register_accounts(db,provider):
    providers=['wechat','wechat_group'] if provider=='wechat' else [provider]
    rows=[dict(row) for row in db.execute(text("""SELECT i.*,p.profile_type,p.archived_at FROM identities i
        JOIN profiles p ON p.id=i.profile_id
        WHERE i.provider=ANY(:providers) AND i.status NOT IN ('invalid','merged') ORDER BY i.external_id"""),{'providers':providers}).mappings()]
    # A numeric Instagram account supplies its own username; this explicitly
    # ties the two identifiers together without consulting names or Profile IDs.
    usernames=defaultdict(set)
    if provider=='instagram':
        for row in rows:
            authoritative=row['external_id'].isdigit()
            if authoritative and row.get('username'):
                usernames[row['username'].casefold().removeprefix('@')].add(row['external_id'])
    accounts=defaultdict(list);invalid=[]
    for row in rows:
        key=identity_key(provider,row['external_id'],row.get('profile_url'),row.get('username'))
        alias_key=str(key).casefold()
        if alias_key in usernames and len(usernames[alias_key])==1:key=next(iter(usernames[alias_key]))
        kind='group' if provider=='wechat' and row['external_id'].endswith('@chatroom') else 'person'
        if not key or kind!=row['profile_type']:
            invalid.append(str(row['id']));continue
        accounts[key].append(row)
    ambiguous=[];linked=0
    with platform_engine(provider).begin() as source:
        for external,identities in accounts.items():
            aid=account_id(provider,external);first=identities[0]
            kind='group' if provider=='wechat' and external.endswith('@chatroom') else 'person'
            args={'id':aid,'provider':provider,'external':external,'kind':kind,'name':first['display_name'] or first['username'] or external,
                  'username':next((i['username'] for i in identities if i['username']),None),
                  'url':next((i['profile_url'] for i in identities if i['profile_url']),None)}
            ensure_account(source,provider,external,kind=kind,display_name=args['name'],username=args['username'],
                           profile_url=args['url'],identifiers=[i['external_id'] for i in identities])
            db.execute(text("""INSERT INTO source_accounts(id,provider,external_id,object_kind,display_name,username,profile_url)
                VALUES(:id,:provider,:external,:kind,:name,:username,:url) ON CONFLICT(id) DO NOTHING"""),args)
            owners={i['profile_id'] for i in identities if not i['archived_at']}
            if len(owners)==1:
                # Initialize only once. An explicit unlink must survive retries.
                seen=db.execute(text('SELECT 1 FROM source_account_link_revisions WHERE account_id=:id LIMIT 1'),{'id':aid}).scalar_one_or_none()
                if not seen:
                    pid=next(iter(owners));evidence=json.dumps({'kind':'existing-exact-identity','identifiers':[i['external_id'] for i in identities]})
                    db.execute(text("""INSERT INTO source_account_links(account_id,profile_id,evidence)
                        VALUES(:account,:profile,CAST(:evidence AS jsonb)) ON CONFLICT DO NOTHING"""),{'account':aid,'profile':pid,'evidence':evidence})
                    db.execute(text("""INSERT INTO source_account_link_revisions(account_id,new_profile_id,evidence)
                        VALUES(:account,:profile,CAST(:evidence AS jsonb))"""),{'account':aid,'profile':pid,'evidence':evidence})
                    linked+=1
            elif owners:ambiguous.append(external)
            db.execute(text('UPDATE identities SET source_account_id=:account WHERE id=ANY(CAST(:ids AS uuid[])) AND source_account_id IS DISTINCT FROM :account'),
                       {'account':aid,'ids':[str(i['id']) for i in identities]})
    db.commit()
    return {'provider':provider,'accounts':len(accounts),'new_links':linked,'ambiguous_accounts':ambiguous,'invalid_identity_ids':invalid}


def account_lookup(db,provider):
    accounts={str(row.id):dict(row) for row in db.execute(text('SELECT * FROM source_accounts WHERE provider=:provider'),{'provider':provider}).mappings()}
    aliases={row.external_id:str(row.source_account_id) for row in db.execute(text("""SELECT external_id,source_account_id
        FROM identities WHERE source_account_id=ANY(CAST(:ids AS uuid[])) AND status NOT IN ('invalid','merged')"""),{'ids':list(accounts)})}
    for aid,row in accounts.items():aliases[row['external_id']]=aid
    profiles=defaultdict(set)
    for row in db.execute(text('SELECT account_id,profile_id FROM source_account_links WHERE account_id=ANY(CAST(:ids AS uuid[]))'),{'ids':list(accounts)}):
        profiles[str(row.profile_id)].add(str(row.account_id))
    return accounts,aliases,profiles


def resolve_account(provider,record,field,pid,aliases,profiles):
    value=str(record or '')
    for prefix in ('moments-locations:','moments-node:','instagram-profile:'):
        value = value.removeprefix(prefix)
    if field.startswith('instagram.accounts.'):
        value=field.split('.')[2]
    if value in aliases:return aliases[value]
    # Historical LinkedIn field snapshots append a content hash to the slug.
    # Only a recognized exact account identifier may strip that suffix.
    match=re.fullmatch(r'(.+):[a-f0-9]{64}',value)
    if match and match[1] in aliases:return aliases[match[1]]
    key=identity_key(provider,value)
    if key in aliases:return aliases[key]
    candidates=profiles.get(str(pid),set())
    # This fallback applies only to old records explicitly owned by a Profile
    # with one source account. Multi-account records remain unresolved.
    return next(iter(candidates)) if len(candidates)==1 else None


def migrate_fields(db,provider):
    _,aliases,profiles=account_lookup(db,provider)
    unresolved=[];count=0
    cursor=0
    while True:
        rows=db.execute(text("""SELECT r.*,c.payload_json AS value FROM profile_field_revisions r
            JOIN content_objects c ON c.id=r.content_object_id
            WHERE r.source_type=:provider AND r.source_account_id IS NULL AND r.revision_seq>:cursor
            ORDER BY r.revision_seq LIMIT 200"""),{'provider':provider,'cursor':cursor}).mappings().all()
        if not rows:break
        records=[];updates=[]
        with platform_engine(provider).begin() as source:
            for row in rows:
                aid=resolve_account(provider,row['source_record_id'],row['field_key'],row['profile_id'],aliases,profiles)
                if not aid and provider=='wechat' and re.fullmatch(r'[A-Za-z0-9_.@-]+',str(row['source_record_id'] or '')):
                    external=row['source_record_id'];kind='group' if external.endswith('@chatroom') else 'person'
                    aid=ensure_account(source,provider,external,kind=kind)
                    db.execute(text('''INSERT INTO source_accounts(id,provider,external_id,object_kind,display_name)
                        VALUES(:id,:provider,:external,:kind,:external) ON CONFLICT DO NOTHING'''),{'id':aid,'provider':provider,'external':external,'kind':kind})
                    aliases[external]=str(aid)
                if not aid:
                    unresolved.append(str(row['id']));continue
                records.append({'account':aid,'kind':'field','external_id':row['field_key'],'value':row['value'],
                                'source_event':'legacy-field:'+str(row['id']),'observed_at':row['observed_at']})
                updates.append({'account':aid,'id':row['id'],'object':object_id(aid,'field',row['field_key'])})
            observe_many(source,records)
        if updates:
            db.execute(text('UPDATE profile_field_revisions SET source_account_id=:account WHERE id=:id'),updates)
            db.execute(text('UPDATE profile_field_current SET source_account_id=:account WHERE current_revision_id=:id'),updates)
            db.execute(text("""INSERT INTO source_profile_projections(account_id,object_kind,source_object_id,projection_kind,projection_id)
                VALUES(:account,'field',:object,'field_revision',CAST(:id AS text)) ON CONFLICT DO NOTHING"""),updates)
            count+=len(updates)
        db.commit();cursor=rows[-1]['revision_seq']
    return {'provider':provider,'field_revisions_migrated':count,'unresolved_field_revision_ids':unresolved}


def archived_post_owners(provider,path):
    owners=defaultdict(set)
    if not path:return {}
    from .profile_presentation import identity_key
    if provider=='instagram':
        for filename in path.glob('*/*.json'):
            try:value=json.loads(filename.read_text());node=value.get('node') or value
            except (ValueError,OSError):continue
            if not isinstance(node,dict):continue
            owner=node.get('owner') or (node.get('iphone_struct') or {}).get('user') or {}
            if isinstance(owner,dict) and node.get('id') and owner.get('id'):
                owners[str(node['id'])].add(str(owner['id']))
    elif provider=='linkedin':
        for profile in json.loads(path.read_text()).get('profiles',[]):
            key=identity_key('linkedin',profile.get('provider_user_id'),profile.get('profile_url'))
            for post in profile.get('posts') or []:
                external=post.get('provider_post_id') or post.get('source_key') or post.get('id')
                if key and external:owners[str(external)].add(key)
    return {key:next(iter(value)) for key,value in owners.items() if len(value)==1}


def migrate_posts(db,provider,limit=0,archive=None):
    _,aliases,profiles=account_lookup(db,provider)
    archived_owners=archived_post_owners(provider,archive)
    migrated=0;unresolved=[];cursor='00000000-0000-0000-0000-000000000000'
    while True:
        rows=db.execute(text("""SELECT p.*,c.payload_json AS value FROM social_posts p
            JOIN content_objects c ON c.id=p.content_object_id
            WHERE p.provider=:provider AND p.source_account_id IS NULL AND p.id>CAST(:cursor AS uuid)
            ORDER BY p.id LIMIT :limit"""),{'provider':provider,'cursor':cursor,'limit':min(200,limit-migrated) if limit else 200}).mappings().all()
        if not rows:break
        cursor=str(rows[-1]['id']);selected={};records=[]
        for row in rows:
            value=row['value'];owner=archived_owners.get(row['external_id']) or value.get('username') or str((value.get('owner') or {}).get('id') or '')
            aid=resolve_account(provider,owner,'',row['profile_id'],aliases,profiles)
            if not aid and owner and re.fullmatch(r'[A-Za-z0-9_.@-]+',owner):
                with platform_engine(provider).begin() as source:
                    aid=ensure_account(source,provider,owner)
                db.execute(text("""INSERT INTO source_accounts(id,provider,external_id,object_kind,display_name)
                    VALUES(:id,:provider,:external,'person',:external) ON CONFLICT DO NOTHING"""),{'id':aid,'provider':provider,'external':owner})
                aliases[owner]=str(aid)
            if not aid:
                unresolved.append(str(row['id']));continue
            selected[row['id']]={**row,'account':aid}
        ids=[str(i) for i in selected]
        if not ids:continue
        revisions=db.execute(text("""SELECT r.*,c.payload_json AS value FROM social_post_revisions r
            JOIN content_objects c ON c.id=r.content_object_id WHERE r.post_id=ANY(CAST(:ids AS uuid[]))
            ORDER BY r.observed_at,r.id"""),{'ids':ids}).mappings().all()
        for revision in revisions:
            post=selected[revision['post_id']]
            records.append({'account':post['account'],'kind':'post','external_id':post['external_id'],
                            'value':revision['value'],'source_event':'legacy-post:'+str(revision['id']),
                            'observed_at':revision['observed_at'],'occurred_at':post['occurred_at']})
        for post in selected.values():
            records.append({'account':post['account'],'kind':'post','external_id':post['external_id'],
                            'value':post['value'],'source_event':'legacy-post-current:'+str(post['id'])+':'+post['current_content_hash'],
                            'occurred_at':post['occurred_at']})
        media=db.execute(text("""SELECT a.*,l.role,l.entity_id FROM media_links l JOIN media_assets a ON a.id=l.media_id
            WHERE l.entity_type='social_post' AND l.entity_id=ANY(CAST(:ids AS uuid[]))"""),{'ids':ids}).mappings().all()
        with platform_engine(provider).begin() as source:
            observe_many(source,records)
            if media:
                source.execute(text("""INSERT INTO media(sha256,media_type,byte_length,object_path)
                    VALUES(:hash,:type,:bytes,:path) ON CONFLICT DO NOTHING"""),
                    [{'hash':m['sha256'],'type':m['media_type'],'bytes':m['byte_length'],'path':m['object_path']} for m in media])
                source.execute(text('INSERT INTO media_links(object_id,media_hash,role) VALUES(:id,:hash,:role) ON CONFLICT DO NOTHING'),
                    [{'id':object_id(selected[m['entity_id']]['account'],'post',selected[m['entity_id']]['external_id']),
                      'hash':m['sha256'],'role':m['role']} for m in media])
        updates=[{'account':p['account'],'id':p['id'],'object':object_id(p['account'],'post',p['external_id'])} for p in selected.values()]
        db.execute(text('UPDATE social_posts SET source_account_id=:account WHERE id=:id'),updates)
        db.execute(text("""INSERT INTO source_profile_projections(account_id,object_kind,source_object_id,projection_kind,projection_id)
            VALUES(:account,'post',:object,'social_post',CAST(:id AS text)) ON CONFLICT DO NOTHING"""),updates)
        db.commit();migrated+=len(updates)
        print(json.dumps({'provider':provider,'posts_migrated':migrated}),flush=True)
        if limit and migrated>=limit:break
    return {'provider':provider,'posts_migrated':migrated,'unresolved_post_ids':unresolved}


def migrate_facts(db,provider):
    _,aliases,profiles=account_lookup(db,provider)
    count=0;unresolved=[]
    for kind in ('address','employment','education'):
        table=f'profile_{kind}_revisions'
        rows=db.execute(text(f'''SELECT r.*,c.payload_json AS value FROM {table} r
            JOIN content_objects c ON c.id=r.content_object_id
            WHERE r.source_type=:provider AND r.source_account_id IS NULL ORDER BY r.revision_seq'''),{'provider':provider}).mappings().all()
        for offset in range(0,len(rows),200):
            records=[];updates=[]
            for row in rows[offset:offset+200]:
                aid=resolve_account(provider,row['source_record_id'],'',row['profile_id'],aliases,profiles)
                if not aid:unresolved.append(str(row['id']));continue
                records.append({'account':aid,'kind':kind,'external_id':row['fact_key'],'value':row['value'],
                                'source_event':'legacy-fact:'+str(row['id']),'observed_at':row['observed_at']})
                updates.append({'account':aid,'id':row['id']})
            with platform_engine(provider).begin() as source:observe_many(source,records)
            if updates:
                db.execute(text(f'UPDATE {table} SET source_account_id=:account WHERE id=:id'),updates)
                db.execute(text('UPDATE profile_structured_current SET source_account_id=:account WHERE current_revision_id=:id'),updates)
            db.commit();count+=len(updates)
    return {'provider':provider,'facts_migrated':count,'unresolved_fact_ids':unresolved}


def migrate_pointers(db,provider,archive=None):
    """Copy only valid current projections; historical quarantines stay historical."""
    db.execute(text('''INSERT INTO repair_snapshots(repair_key,entity_type,entity_key,payload)
        SELECT 'source-account-current-owner','profile_field_current',c.profile_id::text||':'||c.field_key,to_jsonb(c)
        FROM profile_field_current c JOIN source_accounts a ON a.id=c.source_account_id
        WHERE a.provider=:provider AND NOT EXISTS(SELECT 1 FROM source_account_links l WHERE l.account_id=a.id AND l.profile_id=c.profile_id)
        ON CONFLICT DO NOTHING'''),{'provider':provider})
    db.execute(text('''DELETE FROM profile_field_current c USING source_accounts a
        WHERE c.source_account_id=a.id AND a.provider=:provider
          AND NOT EXISTS(SELECT 1 FROM source_account_links l WHERE l.account_id=a.id AND l.profile_id=c.profile_id)'''),{'provider':provider})
    db.execute(text('''INSERT INTO source_account_fields(account_id,field_key,revision_id)
        SELECT DISTINCT ON(c.source_account_id,c.field_key) c.source_account_id,c.field_key,c.current_revision_id
        FROM profile_field_current c JOIN source_accounts a ON a.id=c.source_account_id
        WHERE a.provider=:provider ORDER BY c.source_account_id,c.field_key,c.updated_at DESC
        ON CONFLICT DO NOTHING'''),{'provider':provider})
    db.execute(text('''INSERT INTO source_account_fields(account_id,field_key,revision_id)
        SELECT DISTINCT ON(r.source_account_id,r.field_key) r.source_account_id,r.field_key,r.id
        FROM profile_field_revisions r JOIN source_accounts a ON a.id=r.source_account_id
        WHERE a.provider=:provider
          AND NOT EXISTS(SELECT 1 FROM repair_snapshots q WHERE q.entity_type='profile_field_current'
            AND q.payload->>'current_revision_id'=r.id::text)
          AND NOT (a.object_kind='group' AND r.field_key IN ('wechat.location_history','wechat.moments_summary'))
        ORDER BY r.source_account_id,r.field_key,r.revision_seq DESC ON CONFLICT DO NOTHING'''),{'provider':provider})
    db.execute(text('''INSERT INTO source_account_facts(account_id,fact_type,fact_key,revision_id)
        SELECT DISTINCT ON(c.source_account_id,c.fact_type,c.fact_key) c.source_account_id,c.fact_type,c.fact_key,c.current_revision_id
        FROM profile_structured_current c JOIN source_accounts a ON a.id=c.source_account_id
        WHERE a.provider=:provider ORDER BY c.source_account_id,c.fact_type,c.fact_key,c.updated_at DESC
        ON CONFLICT DO NOTHING'''),{'provider':provider})
    _,aliases,profiles=account_lookup(db,provider)
    # Source media roles provide platform evidence. Untyped manual selections
    # stay with the Profile. No picture/hash is used to match a person.
    cover_owners=defaultdict(set)
    if provider=='wechat' and archive:
        for external,entry in json.loads(archive.read_text()).items():
            if isinstance(entry,dict) and entry.get('file') and external in aliases:
                cover_owners[entry['file']].add(aliases[external])
    roles={'wechat':['cover','wechat_avatar'],'linkedin':['linkedin_avatar'],'instagram':['instagram_avatar']}
    media=db.execute(text('''SELECT l.*,m.sha256,m.media_type,m.byte_length,m.object_path,m.source_record_id FROM media_links l
        JOIN media_assets m ON m.id=l.media_id WHERE l.entity_type='profile' AND l.role=ANY(:roles)
        ORDER BY m.created_at,l.media_id,l.entity_id,l.role'''),{'roles':roles[provider]}).mappings().all()
    tags=db.execute(text('SELECT t.*,g.name FROM tag_memberships t JOIN tags g ON g.id=t.tag_id WHERE t.source_type=:provider'),{'provider':provider}).mappings().all()
    # Finish the small pointer index transaction before copying source media.
    # Previously every avatar forced a separate source and CRM disk commit.
    db.commit()
    media_saved=0;tags_saved=0
    for offset in range(0,len(media),100):
        selected=[]
        for row in media[offset:offset+100]:
            explicit=Path(row['source_record_id'] or '').stem if row['role']=='wechat_avatar' else None
            cover_candidates=cover_owners.get(Path(row['source_record_id'] or '').name,set()) & profiles.get(str(row['entity_id']),set())
            aid=row['source_account_id'] or (next(iter(cover_candidates)) if row['role']=='cover' and len(cover_candidates)==1 else resolve_account(provider,explicit,'',row['entity_id'],aliases,profiles))
            if not aid and provider=='wechat':
                candidates=db.execute(text("""SELECT DISTINCT source_account_id FROM profile_field_current WHERE profile_id=:id
                    AND field_key IN ('wechat.signature','wechat.direct_stats','wechat.location_history') AND source_account_id IS NOT NULL"""),{'id':row['entity_id']}).scalars().all()
                if len(candidates)==1:aid=candidates[0]
            if aid:selected.append({**row,'account':aid,'object':object_id(aid,'profile_media',row['role'])})
        if not selected:continue
        with platform_engine(provider).begin() as source:
            observe_many(source,[{'account':r['account'],'kind':'profile_media','external_id':r['role'],
                'value':{'sha256':r['sha256']},'source_event':'legacy-media:'+str(r['media_id'])} for r in selected])
            source.execute(text('''INSERT INTO media(sha256,media_type,byte_length,object_path)
                VALUES(:sha256,:media_type,:byte_length,:object_path) ON CONFLICT DO NOTHING'''),selected)
            source.execute(text('INSERT INTO media_links(object_id,media_hash,role) VALUES(:object,:sha256,:role) ON CONFLICT DO NOTHING'),selected)
        db.execute(text('INSERT INTO source_account_media(account_id,media_id,role) VALUES(:account,:media_id,:role) ON CONFLICT DO NOTHING'),selected)
        db.execute(text('''UPDATE media_links SET source_account_id=:account
            WHERE entity_type=:entity_type AND entity_id=:entity_id AND media_id=:media_id AND role=:role
              AND source_account_id IS DISTINCT FROM CAST(:account AS uuid)'''),selected)
        db.commit();media_saved+=len(selected)
        print(json.dumps({'provider':provider,'profile_media_checked':offset+len(media[offset:offset+100]),
                          'source_media_saved':media_saved,'profile_media_total':len(media)}),flush=True)
    for offset in range(0,len(tags),100):
        selected=[]
        for row in tags[offset:offset+100]:
            aid=row['source_account_id'] or resolve_account(provider,None,'',row['profile_id'],aliases,profiles)
            if aid:selected.append({**row,'account':aid})
        if not selected:continue
        with platform_engine(provider).begin() as source:
            observe_many(source,[{'account':r['account'],'kind':'tag','external_id':r['name'],'value':r['name'],
                'source_event':'legacy-tag:'+str(r['tag_id'])} for r in selected])
        db.execute(text('INSERT INTO source_account_tags(account_id,tag_id) VALUES(:account,:tag_id) ON CONFLICT DO NOTHING'),selected)
        db.execute(text('''UPDATE tag_memberships SET source_account_id=:account WHERE profile_id=:profile_id AND tag_id=:tag_id
            AND source_type=:source_type AND source_account_id IS DISTINCT FROM CAST(:account AS uuid)'''),selected)
        db.commit();tags_saved+=len(selected)
        print(json.dumps({'provider':provider,'source_tags_saved':tags_saved,'tag_links_checked':offset+len(tags[offset:offset+100]),'tag_links_total':len(tags)}),flush=True)
    names=db.execute(text('''SELECT DISTINCT ON(f.account_id) f.account_id,c.payload_json #>> '{}' AS name
        FROM source_account_fields f JOIN profile_field_revisions r ON r.id=f.revision_id
        JOIN content_objects c ON c.id=r.content_object_id JOIN source_accounts a ON a.id=f.account_id
        WHERE a.provider=:provider AND f.field_key=ANY(:fields) AND jsonb_typeof(c.payload_json)='string'
          AND length(c.payload_json #>> '{}')>0
        ORDER BY f.account_id,array_position(CAST(:fields AS text[]),f.field_key)'''),
        {'provider':provider,'fields':{'wechat':['wechat.remark','wechat.nickname'],'linkedin':['linkedin.name'],
                                    'instagram':['instagram.full_name','instagram.username']}[provider]}).mappings().all()
    if names:
        with platform_engine(provider).begin() as source:
            source.execute(text('UPDATE accounts SET display_name=:name WHERE id=:account_id AND display_name IS DISTINCT FROM :name'),[dict(r) for r in names])
        db.execute(text('UPDATE source_accounts SET display_name=:name WHERE id=:account_id AND display_name IS DISTINCT FROM :name'),[dict(r) for r in names]);db.commit()
    return {'provider':provider,'current_pointers_saved':True,'source_media_saved':media_saved,'source_tags_saved':tags_saved}


def migrate_interactions(db,provider,archive=None):
    # Author IDs are explicit source IDs; names never participate in mapping.
    count=db.execute(text('''UPDATE social_interactions s SET author_account_id=i.source_account_id
        FROM identities i WHERE s.provider=:provider AND i.provider=:provider AND i.external_id=s.author_external_id
          AND i.status NOT IN ('invalid','merged') AND i.source_account_id IS NOT NULL
          AND s.author_account_id IS DISTINCT FROM i.source_account_id'''),{'provider':provider}).rowcount
    db.commit()
    if provider=='wechat':
        _,aliases,profiles=account_lookup(db,provider)
        graph=json.loads(archive.read_text()) if archive else {}
        graph_edges={json.dumps(edge,sort_keys=True):edge for edge in graph.get('edges',[])}
        nodes=graph.get('nodes',[])
        rows=db.execute(text("SELECT * FROM relationship_edges WHERE source_type='wechat' AND from_account_id IS NULL")).mappings().all()
        pending=[];relationships=0
        def commit_relationships():
            nonlocal relationships
            if not pending:return
            with platform_engine(provider).begin() as source:
                observe_many(source,[{'account':row['from_account'],'kind':'relationship','external_id':str(row['id']),
                    'value':{'to_account_id':row['to_account'],'type':str(row['relationship_type_id']),'note':row['note'],
                             'evidence_kind':row['evidence_kind']},'source_event':'legacy-relationship:'+str(row['id'])} for row in pending])
            db.execute(text('''INSERT INTO source_relationship_edges(id,from_account_id,to_account_id,relationship_type_id,note,confidence,
                valid_from,valid_to,source_type,evidence_kind) VALUES(:id,:from_account,:to_account,:relationship_type_id,:note,:confidence,
                :valid_from,:valid_to,:source_type,:evidence_kind) ON CONFLICT DO NOTHING'''),pending)
            db.execute(text('UPDATE relationship_edges SET from_account_id=:from_account,to_account_id=:to_account WHERE id=:id'),pending)
            db.commit();relationships+=len(pending);pending.clear()
            print(json.dumps({'provider':provider,'relationships_committed':relationships}),flush=True)
        for row in rows:
            a=resolve_account(provider,None,'',row['from_profile_id'],aliases,profiles)
            b=resolve_account(provider,None,'',row['to_profile_id'],aliases,profiles)
            try:
                note=json.loads(row['note'] or '{}')
                exact=graph_edges.get(json.dumps(note,sort_keys=True))
                if exact and isinstance(exact['a'],int) and isinstance(exact['b'],int):
                    a=aliases.get(str(nodes[exact['a']]['u']));b=aliases.get(str(nodes[exact['b']]['u']))
            except (ValueError,KeyError,IndexError,TypeError):pass
            if not a or not b:continue
            pending.append({**row,'from_account':a,'to_account':b})
            if len(pending)>=100:commit_relationships()
        commit_relationships()
    return {'provider':provider,'interaction_authors_assigned':count}


def migrate_groups(db):
    accounts,aliases,profiles=account_lookup(db,'wechat');records=[];count=0;created=0;unresolved=[]
    def explicit_account(external,kind):
        nonlocal created
        if not re.fullmatch(r'[A-Za-z0-9_.@-]+',external) or external.endswith('@chatroom')!=(kind=='group'):
            return None
        aid=aliases.get(external)
        if aid:return aid if accounts[aid]['object_kind']==kind else None
        # A member's native account is still a source object when no human
        # Profile link exists. Do not fall back to another account on the old
        # Profile, create a person, or infer a new link from a display name.
        with platform_engine('wechat').begin() as source:
            aid=str(ensure_account(source,'wechat',external,kind=kind))
        db.execute(text('''INSERT INTO source_accounts(id,provider,external_id,object_kind,display_name)
            VALUES(:id,'wechat',:external,:kind,:external) ON CONFLICT DO NOTHING'''),
            {'id':aid,'external':external,'kind':kind})
        aliases[external]=aid;accounts[aid]={'object_kind':kind};created+=1
        return aid
    for row in db.execute(text('SELECT * FROM group_memberships')).mappings().all():
        explicit=str(row['source_record_id'] or '').split(':')
        if len(explicit)==3 and explicit[0] in ('chatlog','membership'):
            group=explicit_account(explicit[1],'group');person=explicit_account(explicit[2],'person')
        else:
            group=resolve_account('wechat',None,'',row['group_profile_id'],aliases,profiles)
            person=resolve_account('wechat',None,'',row['person_profile_id'],aliases,profiles)
        if not group or not person:
            unresolved.append(row['source_record_id']);continue
        args={'group':group,'person':person,'role':row['role'],'joined':row['joined_at'],'left':row['left_at'],'record':row['source_record_id']}
        db.execute(text('''INSERT INTO source_group_memberships(group_account_id,person_account_id,role,joined_at,left_at,source_record_id)
            VALUES(:group,:person,:role,:joined,:left,:record) ON CONFLICT DO NOTHING'''),args)
        records.append({'account':group,'kind':'membership','external_id':person,'value':{key:str(value) if value is not None else None for key,value in args.items()},
                        'source_event':'legacy-membership:'+person})
        if len(records)>=200:
            with platform_engine('wechat').begin() as source:observe_many(source,records)
            count+=len(records);records=[];db.commit()
            print(json.dumps({'memberships_committed':count,'unlinked_source_accounts_created':created}),flush=True)
    with platform_engine('wechat').begin() as source:observe_many(source,records)
    count+=len(records);db.commit()
    return {'provider':'wechat','memberships':count,'unlinked_source_accounts_created':created,
            'unresolved_memberships':unresolved}


def migrate_notebook_fields(db,provider):
    from .profile_presentation import ALIASES, clean_text
    from .repositories import append_field_revision
    _,aliases,profiles=account_lookup(db,provider);count=0;unresolved=0
    rows=db.execute(text("""SELECT r.*,o.payload_json AS value FROM profile_field_current c
        JOIN profile_field_revisions r ON r.id=c.current_revision_id JOIN content_objects o ON o.id=r.content_object_id
        WHERE c.field_key='monica.custom_fields' ORDER BY r.revision_seq""")).mappings().all()
    existing={(str(r.account_id),r.field_key) for r in db.execute(text('SELECT account_id,field_key FROM source_account_fields'))}
    pending=[]
    for row in rows:
        value=row['value']
        if not isinstance(value,dict):continue
        key={'wechat':'微信内部 ID','linkedin':'LinkedIn','instagram':'Instagram'}[provider]
        explicit=value.get(key);explicit=explicit[0] if isinstance(explicit,list) and len(explicit)==1 else explicit if isinstance(explicit,str) else None
        aid=resolve_account(provider,explicit,'',row['profile_id'],aliases,profiles)
        values={mapped:clean_text(value[label]) for label,mapped in ALIASES.items() if mapped.startswith(provider+'.') and label in value}
        if not aid:
            if values:unresolved+=1
            continue
        for field,content in values.items():
            if (str(aid),field) in existing:continue
            pending.append({'row':row,'account':aid,'field':field,'value':content})
            existing.add((str(aid),field))
    for offset in range(0,len(pending),100):
        part=pending[offset:offset+100]
        with platform_engine(provider).begin() as source:
            observe_many(source,[{'account':item['account'],'kind':'field','external_id':item['field'],'value':item['value'],
                'source_event':'legacy-notebook:'+str(item['row']['id'])+':'+item['field'],'observed_at':item['row']['observed_at']} for item in part])
        for item in part:
            row=item['row']
            append_field_revision(db,profile_id=row['profile_id'],field_key=item['field'],value=item['value'],source_type=provider,
                source_record_id='monica:'+str(row['id']),operation='import',observed_at=row['observed_at'],source_account_id=item['account'])
        db.commit();count+=len(part)
        print(json.dumps({'provider':provider,'notebook_fields_committed':count,'fields_to_copy':len(pending)}),flush=True)
    return {'provider':provider,'notebook_fields':count,'unresolved_notebooks':unresolved}


def migrate_tags(db,archive):
    if not archive:raise ValueError('The exact source tag archive is required')
    _,aliases,_profiles=account_lookup(db,'wechat');count=0;missing=[];pending=[]
    names_by_id=dict(db.execute(text('SELECT name,id FROM tags')).all())
    for external,names in json.loads(archive.read_text()).get('tags',{}).items():
        aid=aliases.get(external)
        if not aid:missing.append(external);continue
        for name in dict.fromkeys(names):
            if name not in names_by_id:
                db.execute(text('INSERT INTO tags(name) VALUES(:name) ON CONFLICT(name) DO NOTHING'),{'name':name})
                names_by_id[name]=db.execute(text('SELECT id FROM tags WHERE name=:name'),{'name':name}).scalar_one()
            pending.append({'account':aid,'tag':names_by_id[name],'name':name})
    db.commit()
    for offset in range(0,len(pending),100):
        batch=pending[offset:offset+100]
        with platform_engine('wechat').begin() as source:
            observe_many(source,[{'account':r['account'],'kind':'tag','external_id':r['name'],'value':r['name'],
                'source_event':'source-tag:'+r['name']} for r in batch])
        db.execute(text('INSERT INTO source_account_tags(account_id,tag_id) VALUES(:account,:tag) ON CONFLICT DO NOTHING'),batch)
        db.execute(text('''UPDATE tag_memberships t SET source_account_id=:account FROM source_account_links l
            WHERE l.account_id=:account AND t.profile_id=l.profile_id AND t.tag_id=:tag AND t.source_type='wechat'
              AND t.source_account_id IS NULL'''),batch)
        db.commit();count+=len(batch)
        print(json.dumps({'source_tag_links':count,'of':len(pending)}),flush=True)
    return {'source_tag_links':count,'unmapped_tag_accounts':missing}


def ensure_message_senders(db, aliases, external_ids, *, migrated=False):
    """Register exact source sender IDs without creating or linking people.

    The source commit may survive a failed CRM transaction. Reuse its metadata
    on retry; never downgrade an existing source account to an ID-only name.
    """
    missing=sorted({value for value in external_ids if isinstance(value,str) and value and value not in aliases})
    if not missing:return 0
    with platform_engine('wechat').begin() as source:
        existing={r.external_id:dict(r) for r in source.execute(text(
            'SELECT * FROM accounts WHERE external_id=ANY(:ids)'),{'ids':missing}).mappings()}
        rows=[]
        for external in missing:
            row=existing.get(external)
            if row is None:
                kind='group' if external.endswith('@chatroom') else 'person'
                aid=ensure_account(source,'wechat',external,kind=kind)
                row={'id':aid,'external_id':external,'object_kind':kind,'display_name':external,'username':None,'profile_url':None}
            rows.append({**row,'migrated':migrated})
    db.execute(text('''INSERT INTO source_accounts(id,provider,external_id,object_kind,display_name,username,profile_url,migrated_at)
        VALUES(:id,'wechat',:external_id,:object_kind,:display_name,:username,:profile_url,
            CASE WHEN :migrated THEN now() ELSE NULL END) ON CONFLICT(id) DO NOTHING'''),rows)
    aliases.update({r['external_id']:str(r['id']) for r in rows})
    return len(rows)


def update_message_senders(db, updates):
    """Project a bounded source batch with one exact-time update, including clears."""
    if not updates:return 0
    rows=[{'id':str(r['id']),'at':r['at'].isoformat(),
           'account':str(r['account']) if r['account'] else None} for r in updates]
    return db.execute(text('''UPDATE message_events SET sender_account_id=i.account
        FROM jsonb_to_recordset(CAST(:rows AS jsonb)) i(id uuid,at timestamptz,account uuid)
        WHERE message_events.id=i.id AND message_events.occurred_at=i.at
          AND message_events.occurred_at=ANY(CAST(:dates AS timestamptz[]))
          AND message_events.sender_account_id IS DISTINCT FROM i.account'''),
        {'rows':json.dumps(rows),'dates':sorted({r['at'] for r in updates})}).rowcount


def message_cursor_complete(cursor, watermark):
    """Return whether a durable forward cursor reached a room's newest message."""
    if not cursor or not watermark:
        return False
    try:
        saved=json.loads(cursor) if isinstance(cursor,str) else cursor
        saved_key=(datetime.fromisoformat(saved['at']),UUID(saved['id']))
    except (KeyError,TypeError,ValueError,json.JSONDecodeError):
        return False
    return saved_key >= (watermark.occurred_at,watermark.message_id)


def migrate_messages(db,limit=0,*,batch_size=1000,batch_pause=0.05,stop_requested=None):
    """Copy message sources in bounded batches; skip durable completed cursors."""
    _,aliases,_profiles=account_lookup(db,'wechat');count=0;conversations=0
    rooms=db.execute(text("SELECT * FROM conversations WHERE conversation_type IN ('direct','group') ORDER BY id")).mappings().all()
    watermarks={row.conversation_id:row for row in db.execute(text('''SELECT c.id AS conversation_id,
        latest.message_id,latest.occurred_at FROM conversations c JOIN LATERAL (
          SELECT message_id,occurred_at FROM message_search
          WHERE conversation_id=c.id ORDER BY occurred_at DESC,message_id DESC LIMIT 1
        ) latest ON TRUE WHERE c.conversation_type IN ('direct','group')'''))}
    with platform_engine('wechat').connect() as source:
        cursors=dict(source.execute(text(
            "SELECT stream,cursor FROM migration_cursors WHERE stream LIKE 'messages:%'"
        )).all())
    for room in rooms:
        if stop_requested and stop_requested():
            db.rollback();return {'messages_copied':count,'conversations_checked':conversations,'stopped_at_checkpoint':True}
        stream='messages:'+str(room['id'])
        if room['source_account_id'] and message_cursor_complete(cursors.get(stream),watermarks.get(room['id'])):
            conversations+=1
            if conversations%20==0:print(json.dumps({'messages_copied':count,'conversations_checked':conversations}),flush=True)
            continue
        external=room['external_id'].split(':',1)[-1]
        aid=aliases.get(external)
        if not aid:
            # A source-native room may exist without a human Profile link.
            with platform_engine('wechat').begin() as source:
                aid=ensure_account(source,'wechat',external,kind='group' if external.endswith('@chatroom') else 'person')
            db.execute(text('''INSERT INTO source_accounts(id,provider,external_id,object_kind,display_name)
                VALUES(:id,'wechat',:external,:kind,:external) ON CONFLICT DO NOTHING'''),
                {'id':aid,'external':external,'kind':'group' if external.endswith('@chatroom') else 'person'})
            aliases[external]=str(aid)
        with platform_engine('wechat').begin() as source:
            observe(source,aid,'conversation',external,{'type':room['conversation_type'],'external_id':external},source_event='legacy-conversation:'+str(room['id']))
        db.execute(text('UPDATE conversations SET source_account_id=:account WHERE id=:id'),{'account':aid,'id':room['id']});db.commit()
        cursor=cursors.get(stream)
        last=json.loads(cursor) if cursor else {'at':'1900-01-01T00:00:00+00:00','id':'00000000-0000-0000-0000-000000000000'}
        while True:
            if stop_requested and stop_requested():
                db.rollback();return {'messages_copied':count,'conversations_checked':conversations,'stopped_at_checkpoint':True}
            selected=db.execute(text('''SELECT s.message_id,s.occurred_at FROM message_search s
                WHERE s.conversation_id=:room AND (s.occurred_at,s.message_id)>(CAST(:at AS timestamptz),CAST(:id AS uuid))
                ORDER BY s.occurred_at,s.message_id LIMIT :limit'''),
                {'room':room['id'],**last,'limit':min(batch_size,limit-count) if limit else batch_size}).all()
            if not selected:break
            page=[{'id':str(r.message_id),'at':r.occurred_at.isoformat()} for r in selected]
            rows=db.execute(text('''SELECT m.*,c.payload_json AS value FROM message_events m
                JOIN jsonb_to_recordset(CAST(:page AS jsonb)) chosen(id uuid,at timestamptz)
                  ON m.id=chosen.id AND m.occurred_at=chosen.at
                JOIN content_objects c ON c.id=m.content_object_id
                WHERE m.occurred_at=ANY(CAST(:dates AS timestamptz[]))
                ORDER BY m.occurred_at,m.id'''),
                {'page':json.dumps(page),'dates':sorted({r.occurred_at for r in selected})}).mappings().all()
            if len(rows)!=len(selected):raise ValueError('Message search and archive page disagree; source cursor not advanced')
            ensure_message_senders(db,aliases,[r['value'].get('sender_external_id') for r in rows])
            records=[{'account':aid,'kind':'message','external_id':message_key(row['source_record_id'], row['occurred_at']),
                      'value':row['value'],'occurred_at':row['occurred_at'],'source_event':'legacy-message:'+str(row['id'])} for row in rows]
            last={'at':rows[-1]['occurred_at'].isoformat(),'id':str(rows[-1]['id'])}
            with platform_engine('wechat').begin() as source:
                observe_immutable_many(source,records)
            updates=[]
            for row in rows:
                account=aliases.get(str(row['value'].get('sender_external_id')))
                if str(row['sender_account_id'] or '')!=str(account or ''):
                    updates.append({'account':account,'id':row['id'],'at':row['occurred_at']})
            update_message_senders(db,updates)
            db.commit()
            # The source commit can survive a failed CRM projection. Advance
            # only after both sides succeed, so a retry repairs sender links
            # using the same immutable events instead of skipping the page.
            with platform_engine('wechat').begin() as source:
                source.execute(text('''INSERT INTO migration_cursors(stream,cursor,object_count) VALUES(:stream,:cursor,:count)
                    ON CONFLICT(stream) DO UPDATE SET cursor=EXCLUDED.cursor,object_count=migration_cursors.object_count+EXCLUDED.object_count,checked_at=now()'''),
                    {'stream':stream,'cursor':json.dumps(last),'count':len(rows)})
            count+=len(rows)
            print(json.dumps({'messages_copied':count,'conversations_checked':conversations,
                              'conversation_cursor_committed':True}),flush=True)
            if stop_requested and stop_requested():
                return {'messages_copied':count,'conversations_checked':conversations,'stopped_at_checkpoint':True}
            if limit and count>=limit:return {'messages_copied':count,'conversations_checked':conversations}
            if batch_pause:time.sleep(batch_pause)
        conversations+=1
        if conversations%20==0:print(json.dumps({'messages_copied':count,'conversations_checked':conversations}),flush=True)
    return {'messages_copied':count,'conversations_checked':conversations}


def reconcile_messages(db,apply=False,limit=0,*,batch_size=1000,batch_pause=0.05,stop_requested=None):
    """Find messages behind a migration watermark without replaying their bodies.

    Historical Chatlog files can arrive after a room was copied. The forward
    cursor cannot prove coverage of those older messages. Compare source-native
    object IDs and hashes in bounded pages; only missing objects read payloads.
    Existing source values are never overwritten by this repair.
    """
    import hashlib

    from .content import canonical_json

    _,aliases,_=account_lookup(db,'wechat')
    report={'messages_checked':0,'missing_messages':0,'different_messages':0,
            'unassigned_rooms':0,'messages_added':0,'sender_mismatches':0,'senders_repaired':0,
            'unregistered_sender_messages':0,'sender_accounts_registered':0,'examples':[]}
    rooms=db.execute(text("SELECT id,source_account_id FROM conversations ORDER BY id")).all()
    for room in rooms:
        if not room.source_account_id:
            report['unassigned_rooms']+=1;continue
        last={'at':'1900-01-01T00:00:00+00:00','id':'00000000-0000-0000-0000-000000000000'}
        while True:
            if stop_requested and stop_requested():
                db.rollback();return {**report,'stopped_at_checkpoint':True}
            selected=db.execute(text('''SELECT message_id,occurred_at FROM message_search
                WHERE conversation_id=:room AND (occurred_at,message_id)>(CAST(:at AS timestamptz),CAST(:id AS uuid))
                ORDER BY occurred_at,message_id LIMIT :limit'''),
                {'room':room.id,**last,'limit':min(batch_size,limit-report['messages_checked']) if limit else batch_size}).all()
            if not selected:break
            page=[{'id':str(r.message_id),'at':r.occurred_at.isoformat()} for r in selected]
            params={'page':json.dumps(page),'dates':sorted({r.occurred_at for r in selected})}
            metadata=db.execute(text('''SELECT m.id,m.occurred_at,m.source_record_id,c.sha256,
                    m.sender_account_id,c.payload_json->>'sender_external_id' AS sender_external_id
                FROM message_events m JOIN jsonb_to_recordset(CAST(:page AS jsonb)) chosen(id uuid,at timestamptz)
                  ON m.id=chosen.id AND m.occurred_at=chosen.at
                JOIN content_objects c ON c.id=m.content_object_id
                WHERE m.occurred_at=ANY(CAST(:dates AS timestamptz[]))'''),params).mappings().all()
            if len(metadata)!=len(selected):raise ValueError('Message search and archive page disagree during reconciliation')
            with platform_engine('wechat').connect() as source:
                resolved = resolve_message_records(source, [dict(r, account=room.source_account_id, kind='message',
                    external_id=message_key(r['source_record_id'], r['occurred_at'])) for r in metadata])
                expected = {str(object_id(r['account'], 'message', r['external_id'])): r for r in resolved}
                actual=dict(source.execute(text('SELECT id::text,current_hash FROM objects WHERE id=ANY(CAST(:ids AS uuid[]))'),
                    {'ids':list(expected)}).all())
            report['unregistered_sender_messages']+=sum(bool(r['sender_external_id']) and r['sender_external_id'] not in aliases for r in metadata)
            if apply:
                report['sender_accounts_registered']+=ensure_message_senders(db,aliases,
                    [r['sender_external_id'] for oid,r in expected.items() if oid not in actual or actual[oid]==r['sha256']])
            missing=[];senders=[]
            for oid,row in expected.items():
                sender=aliases.get(str(row['sender_external_id']))
                if str(row['sender_account_id'] or '')!=str(sender or ''):
                    report['sender_mismatches']+=1
                    if oid not in actual or actual[oid]==row['sha256']:
                        senders.append({'account':sender,'id':row['id'],'at':row['occurred_at']})
                if oid not in actual:
                    report['missing_messages']+=1;missing.append(row)
                    reason='missing'
                elif actual[oid]!=row['sha256']:
                    report['different_messages']+=1;reason='different'
                else:continue
                if len(report['examples'])<20:report['examples'].append({'object_id':oid,'reason':reason})
            if apply and missing:
                params={'page':json.dumps([{'id':str(r['id']),'at':r['occurred_at'].isoformat()} for r in missing]),
                        'dates':sorted({r['occurred_at'] for r in missing})}
                rows=db.execute(text('''SELECT m.id,m.occurred_at,m.source_record_id,c.payload_json AS value
                    FROM message_events m JOIN jsonb_to_recordset(CAST(:page AS jsonb)) chosen(id uuid,at timestamptz)
                      ON m.id=chosen.id AND m.occurred_at=chosen.at
                    JOIN content_objects c ON c.id=m.content_object_id
                    WHERE m.occurred_at=ANY(CAST(:dates AS timestamptz[]))'''),params).mappings().all()
                if len(rows)!=len(missing):raise ValueError('Message archive changed during reconciliation')
                records=[{'account':room.source_account_id,'kind':'message',
                          'external_id':message_key(r['source_record_id'], r['occurred_at']),
                          'value':r['value'],'occurred_at':r['occurred_at'],'source_event':'legacy-message:'+str(r['id'])}
                         for r in rows]
                with platform_engine('wechat').begin() as source:
                    # A collector may have supplied the missing object since
                    # the read-only comparison. Lock in the ordinary order and
                    # refuse to replace a newly observed, different value.
                    records = resolve_message_records(source, records)
                    ids=sorted(str(object_id(r['account'],r['kind'],r['external_id'])) for r in records)
                    source.execute(text('SELECT pg_advisory_xact_lock(hashtextextended(id,0)) FROM (SELECT unnest(CAST(:ids AS text[])) id ORDER BY id) locks'),{'ids':ids})
                    latest=dict(source.execute(text('SELECT id::text,current_hash FROM objects WHERE id=ANY(CAST(:ids AS uuid[]))'),{'ids':ids}).all())
                    for record in records:
                        oid=str(object_id(record['account'],record['kind'],record['external_id']))
                        if oid in latest and latest[oid]!=hashlib.sha256(canonical_json(record['value'])).hexdigest():
                            raise ValueError('Source message changed during reconciliation; no source value overwritten')
                    observe_immutable_many(source,records)
                report['messages_added']+=sum(oid not in latest for oid in ids)
            if apply and senders:
                report['senders_repaired']+=update_message_senders(db,senders)
            report['messages_checked']+=len(selected)
            last={'at':selected[-1].occurred_at.isoformat(),'id':str(selected[-1].message_id)}
            db.commit()
            print(json.dumps({k:v for k,v in report.items() if k!='examples'}),flush=True)
            if stop_requested and stop_requested():return {**report,'stopped_at_checkpoint':True}
            if limit and report['messages_checked']>=limit:return report
            if batch_pause:time.sleep(batch_pause)
    return report


def verify(db,provider,activate=False):
    """Compare source objects with the CRM indexes before enabling link changes."""
    report={'provider':provider};errors=[]
    with platform_engine(provider).connect() as source:
        source_counts={row.object_kind:row.n for row in source.execute(text('SELECT object_kind,count(*) n FROM objects GROUP BY object_kind'))}
        expected_posts=db.execute(text('SELECT count(*) FROM social_posts WHERE provider=:provider'),{'provider':provider}).scalar_one()
        report['posts']={'source':source_counts.get('post',0),'projection':expected_posts}
        if source_counts.get('post',0)!=expected_posts:errors.append('post count mismatch')
        # Compare each projected current payload hash, not just totals.
        hashes={str(row.id):row.current_hash for row in source.execute(text("SELECT id,current_hash FROM objects WHERE object_kind='post'"))}
        mismatch=0
        for row in db.execute(text('SELECT source_account_id,external_id,current_content_hash FROM social_posts WHERE provider=:provider'),{'provider':provider}):
            if not row.source_account_id or hashes.get(str(object_id(row.source_account_id,'post',row.external_id)))!=row.current_content_hash:mismatch+=1
        report['post_hash_mismatches']=mismatch
        if mismatch:errors.append('post current hash mismatch')
        report['objects']=source_counts
        report['source_revision_count']=source.execute(text('SELECT count(*) FROM revisions')).scalar_one()
        from .source_verification import projection_content
        report['projection_checks']=projection_content(db,source,provider)
        for kind,result in report['projection_checks'].items():
            if result['mismatched']:errors.append(kind+' content or media mismatch')
        if provider=='wechat':
            expected_messages=db.execute(text('SELECT count(*) FROM message_search')).scalar_one()
            report['messages']={'source':source_counts.get('message',0),'projection':expected_messages}
            if expected_messages!=source_counts.get('message',0):errors.append('message count mismatch')
            expected_groups=db.execute(text('SELECT count(*) FROM group_memberships')).scalar_one()
            report['memberships']={'source':source_counts.get('membership',0),'projection':expected_groups}
            if expected_groups!=source_counts.get('membership',0):errors.append('membership count mismatch')
    for kind,table,column in [('fields','profile_field_revisions','source_type'),('addresses','profile_address_revisions','source_type'),
                              ('employment','profile_employment_revisions','source_type'),('education','profile_education_revisions','source_type')]:
        missing=db.execute(text(f'SELECT count(*) FROM {table} WHERE {column}=:provider AND source_account_id IS NULL'),{'provider':provider}).scalar_one()
        report['unassigned_'+kind]=missing
        if missing:errors.append('unassigned '+kind)
    report['errors']=errors;report['activated']=False
    if activate:
        if errors:raise ValueError(json.dumps(report))
        db.execute(text('UPDATE source_accounts SET migrated_at=now() WHERE provider=:provider'),{'provider':provider})
        # Accounts with source ownership still ambiguous remain visible but
        # cannot unlink a partially assigned projection. Preserve all evidence.
        pending=db.execute(text('''UPDATE source_accounts a SET migrated_at=NULL FROM source_account_links l
            WHERE a.id=l.account_id AND a.provider=:provider AND (
              EXISTS(SELECT 1 FROM media_links m WHERE m.entity_type='profile' AND m.entity_id=l.profile_id
                AND m.source_account_id IS NULL AND m.role=ANY(:roles))
              OR EXISTS(SELECT 1 FROM tag_memberships t WHERE t.profile_id=l.profile_id AND t.source_type=:provider AND t.source_account_id IS NULL)
              OR EXISTS(SELECT 1 FROM relationship_edges e WHERE e.source_type=:provider
                AND e.evidence_kind='relationship'
                AND (e.from_profile_id=l.profile_id OR e.to_profile_id=l.profile_id)
                AND (e.from_account_id IS NULL OR e.to_account_id IS NULL))
            )'''),{'provider':provider,'roles':{'wechat':['cover','wechat_avatar'],'instagram':['instagram_avatar'],'linkedin':['linkedin_avatar']}[provider]}).rowcount
        report['accounts_waiting_ownership_review']=pending
        quarantined=db.execute(text("""UPDATE source_accounts a SET migrated_at=NULL WHERE a.provider=:provider
            AND EXISTS(SELECT 1 FROM identities i WHERE i.source_account_id=a.id AND i.status='invalid')
            AND NOT EXISTS(SELECT 1 FROM identities i WHERE i.source_account_id=a.id AND i.status NOT IN ('invalid','merged'))"""),{'provider':provider}).rowcount
        report['quarantined_legacy_accounts']=quarantined
        db.execute(text('''INSERT INTO platform_migration_state(provider,enabled) VALUES(:provider,true)
            ON CONFLICT(provider) DO UPDATE SET enabled=true,updated_at=now()'''),{'provider':provider})
        db.commit();report['activated']=True
    return report


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('stage',choices=['accounts','fields','posts','facts','pointers','groups','messages','reconcile-messages','notebook','interactions','tags','verify'])
    parser.add_argument('--provider',choices=PROVIDERS,required=True)
    parser.add_argument('--limit',type=int,default=0)
    parser.add_argument('--archive',type=Path)
    parser.add_argument('--activate',action='store_true')
    parser.add_argument('--apply',action='store_true',help='Add only proven missing source objects during reconciliation')
    parser.add_argument('--batch-size',type=int,default=500,help='Messages per source copy or reconciliation checkpoint')
    parser.add_argument('--batch-pause',type=float,default=0.05,help='Seconds to yield after a committed message checkpoint')
    args=parser.parse_args()
    if not 1<=args.batch_size<=5000 or not 0<=args.batch_pause<=10:
        parser.error('Batch size must be 1–5000 and pause must be 0–10 seconds')
    if args.stage in ('messages','reconcile-messages'):
        if args.provider!='wechat':parser.error('Message sources are WeChat only')
        stopping=False
        def stop_at_checkpoint(_signal,_frame):
            nonlocal stopping
            stopping=True
        signal.signal(signal.SIGTERM,stop_at_checkpoint);signal.signal(signal.SIGINT,stop_at_checkpoint)
        with engine.connect() as lease:
            acquired=lease.execute(text('SELECT pg_try_advisory_lock(11803293)')).scalar_one();lease.commit()
            if not acquired:raise SystemExit('A source message copy or reconciliation is already running')
            try:
                options={'batch_size':args.batch_size,'batch_pause':args.batch_pause,'stop_requested':lambda:stopping}
                with SessionLocal() as db:
                    report=migrate_messages(db,args.limit,**options) if args.stage=='messages' else reconcile_messages(db,args.apply,args.limit,**options)
                    print(json.dumps(report),flush=True)
                if report.get('stopped_at_checkpoint'):raise SystemExit(75)
            finally:
                lease.execute(text('SELECT pg_advisory_unlock(11803293)'));lease.commit()
        return
    with SessionLocal() as db:
        if args.stage=='accounts':report=register_accounts(db,args.provider)
        elif args.stage=='fields':report=migrate_fields(db,args.provider)
        elif args.stage=='facts':report=migrate_facts(db,args.provider)
        elif args.stage=='pointers':report=migrate_pointers(db,args.provider,args.archive)
        elif args.stage=='groups':report=migrate_groups(db)
        elif args.stage=='notebook':report=migrate_notebook_fields(db,args.provider)
        elif args.stage=='interactions':report=migrate_interactions(db,args.provider,args.archive)
        elif args.stage=='verify':report=verify(db,args.provider,args.activate)
        elif args.stage=='tags':report=migrate_tags(db,args.archive)
        else:report=migrate_posts(db,args.provider,args.limit,args.archive)
        print(json.dumps(report,default=lambda v:v.isoformat() if isinstance(v,datetime) else str(v)),flush=True)


if __name__=='__main__':main()
