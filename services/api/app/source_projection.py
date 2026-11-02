"""Source-first ingestion; CRM rows are replaceable indexes of linked accounts."""
from hashlib import sha256
from uuid import UUID

from sqlalchemy import text

from .content import canonical_json
from .ownership import lock_accounts, lock_profiles
from .platform_store import PROVIDERS, ensure_account, observe_many, platform_engine


def enabled(db,provider):
    if provider not in PROVIDERS:return False
    return bool(db.execute(text('SELECT enabled FROM platform_migration_state WHERE provider=:provider'),{'provider':provider}).scalar_one_or_none())


def prepare_records(db,request):
    """Persist one source batch before applying its rebuildable CRM projection.

    Account links stay locked for the CRM transaction. Source commits are per
    batch, rather than per record, so a NAS does not fsync hundreds of times for
    a single import. Stable observation IDs make a later CRM failure replayable.
    """
    provider=request.source
    if not enabled(db,provider):return request.records
    db.execute(text('SELECT pg_advisory_xact_lock_shared(11803295)'))
    from .platform_migrate import account_lookup
    context=db.info.setdefault('source_lookup',{})
    if provider not in context:context[provider]=account_lookup(db,provider)
    records=[];observations=[];links={};seen={}
    with platform_engine(provider).begin() as source:
        for record in request.records:
            prepared,values=_prepare_record(db,source,record,request,context[provider],links)
            records.append(prepared)
            for value in values:
                key=(str(value['account']),value['kind'],value['external_id'],value['source_event'])
                fingerprint=sha256(canonical_json(value['value'])).hexdigest()
                if key in seen:
                    if seen[key]!=fingerprint:raise ValueError('Observation ID was reused with different content')
                    continue
                seen[key]=fingerprint;observations.append(value)
        observe_many(source,observations)
    lock_accounts(db, [], shared=[record.source_account_id for record in records])
    current_links = dict(db.execute(text('SELECT account_id,profile_id FROM source_account_links WHERE account_id=ANY(CAST(:ids AS uuid[]))'),
                                   {'ids': [str(r.source_account_id) for r in records]}).all())
    append_social = all((record.entity_type in ('post', 'story') or request.stream in ('posts', 'moments', 'stories'))
                        and not any((record.content if isinstance(record.content, dict) else {}).get(key)
                                    for key in ('fields', 'field_key', 'address', 'employment', 'education'))
                        for record in records)
    lock_profiles(db, current_links.values(), shared=append_social)
    return [r.model_copy(update={'profile_id': current_links.get(r.source_account_id)}) for r in records]


def _prepare_record(db,source,record,request,lookup,links):
    """Resolve exact accounts; desktop may create an independent first profile."""
    from .platform_migrate import resolve_account
    provider=request.source
    _,aliases,profiles=lookup
    content=record.content if isinstance(record.content,dict) else {}
    kind=record.entity_type or ('post' if request.stream in ('posts','moments') else 'story' if request.stream=='stories' else 'profile')
    owner=record.source_account_external_id or (content.get('username') or str((content.get('owner') or {}).get('id') or '') if kind in ('post','story') else record.external_id)
    if kind in ('post','story') and owner:
        from .profile_presentation import identity_key
        aid=aliases.get(owner) or aliases.get(identity_key(provider,owner))
    else:aid=resolve_account(provider,owner,str(content.get('field_key','')),record.profile_id,aliases,profiles)
    if not aid:
        if not owner:raise ValueError('Source account ID is missing; leave the batch for import review')
        from .profile_presentation import identity_key
        external=identity_key(provider,owner)
        if not external:raise ValueError('An explicit source account ID is required')
        object_kind='group' if provider=='wechat' and external.endswith('@chatroom') else 'person'
        aid=ensure_account(source,provider,external,kind=object_kind)
        db.execute(text('''INSERT INTO source_accounts(id,provider,external_id,object_kind,display_name,migrated_at)
            VALUES(:id,:provider,:external,:kind,:external,now()) ON CONFLICT DO NOTHING'''),
            {'id':aid,'provider':provider,'external':external,'kind':object_kind})
        aliases[external]=str(aid)
    aid=str(aid)
    from .config import get_settings
    if get_settings().app_env == 'desktop' and provider in ('instagram', 'linkedin') and kind == 'profile':
        account = db.execute(text('SELECT external_id,object_kind,display_name,username,profile_url FROM source_accounts WHERE id=:id'), {'id': aid}).one()
        username = content.get('username') or account.username
        name = content.get('display_name') or content.get('full_name') or account.display_name or username or account.external_id
        url = content.get('profile_url') or (f'https://www.instagram.com/{username}/' if provider == 'instagram' and username else account.profile_url)
        from .desktop_init import refresh_profile_name
        refresh_profile_name(db, aid, account.display_name, name)
        identifiers = [account.external_id]
        if provider == 'instagram' and username:
            identifiers += [username, 'username:' + username]
        ensure_account(source, provider, account.external_id, kind=account.object_kind,
                       display_name=name, username=username, profile_url=url, identifiers=identifiers)
        db.execute(text('''UPDATE source_accounts SET display_name=:name,username=COALESCE(:username,username),
            profile_url=COALESCE(:url,profile_url) WHERE id=:id'''), {'id': aid, 'name': name, 'username': username, 'url': url})
        for identifier in identifiers:
            prior = db.execute(text('SELECT source_account_id FROM identities WHERE provider=:provider AND external_id=:external'),
                               {'provider': provider, 'external': identifier}).scalar_one_or_none()
            if prior is not None and str(prior) != aid:
                raise ValueError('Account identifier already belongs to another account; review required')
            db.execute(text('''INSERT INTO identities(provider,external_id,source_account_id,username,profile_url,display_name,status)
                VALUES(:provider,:external,:account,:username,:url,:name,'active') ON CONFLICT(provider,external_id)
                DO UPDATE SET source_account_id=EXCLUDED.source_account_id,username=EXCLUDED.username,
                    profile_url=EXCLUDED.profile_url,display_name=EXCLUDED.display_name,last_seen_at=now()'''),
                {'provider': provider, 'external': identifier, 'account': aid, 'username': username, 'url': url, 'name': name})
            aliases[identifier] = aid
    if aid not in links:
        links[aid]=db.execute(text('SELECT profile_id FROM source_account_links WHERE account_id=:id'),{'id':aid}).scalar_one_or_none()
        if links[aid] is None:
            from .desktop_init import account_profile
            account = db.execute(text('SELECT external_id,object_kind FROM source_accounts WHERE id=:id'), {'id': aid}).one()
            links[aid] = account_profile(db, aid, provider, account.external_id, account.object_kind, content)
    pid=links[aid]
    if get_settings().app_env == 'desktop' and provider in ('instagram', 'linkedin'):
        db.execute(text('UPDATE identities SET profile_id=:profile WHERE source_account_id=:account AND profile_id IS DISTINCT FROM CAST(:profile AS uuid)'),
                   {'account': aid, 'profile': pid})
    event='batch:'+request.idempotency_key+':'+record.external_id
    records=[{'account':aid,'kind':kind,'external_id':record.external_id,'value':record.content,
              'source_event':event,'observed_at':request.observed_at,'occurred_at':record.occurred_at}]
    fields=content.get('fields') or ({content['field_key']:content.get('value')} if content.get('field_key') else {})
    for field,value in fields.items():
        records.append({'account':aid,'kind':'field','external_id':field,'value':value,'source_event':event+':'+field,'observed_at':request.observed_at})
    return record.model_copy(update={'profile_id':pid,'source_account_id':UUID(aid),'source_is_authoritative':True}),records


def finish_record(db,record,request):
    aid=getattr(record,'source_account_id',None)
    if not aid:return
    args={'account':aid,'profile':record.profile_id,'source':request.source,'external':record.external_id}
    for table in ('social_posts','social_stories'):
        db.execute(text(f'''UPDATE {table} SET source_account_id=:account,profile_id=:profile
            WHERE provider=:source AND external_id=:external
              AND (source_account_id,profile_id) IS DISTINCT FROM (CAST(:account AS uuid),CAST(:profile AS uuid))'''),args)
    db.execute(text('''UPDATE social_interactions s SET author_account_id=i.source_account_id,
        author_profile_id=i.profile_id FROM identities i WHERE s.provider=:source AND i.provider=:source
        AND s.author_external_id=i.external_id AND i.source_account_id IS NOT NULL
        AND (s.author_account_id,s.author_profile_id) IS DISTINCT FROM (i.source_account_id,i.profile_id)
        AND i.status NOT IN ('invalid','merged') AND s.post_id IN
          (SELECT id FROM social_posts WHERE provider=:source AND external_id=:external)'''),args)
    content=record.content if isinstance(record.content,dict) else {}
    kind=record.entity_type or ('post' if request.stream in ('posts','moments') else 'story' if request.stream=='stories' else 'profile')
    # A post changes its own content and interactions, not every profile field
    # and job/school revision on its author's account.
    if kind in ('post','story') and not any(content.get(key) for key in ('fields','field_key','address','employment','education')):
        return
    if record.profile_id:
        db.execute(text('''UPDATE profile_field_revisions SET source_account_id=:account
            WHERE profile_id=:profile AND source_type=:source AND source_record_id=:external AND source_account_id IS NULL'''),args)
        db.execute(text('''UPDATE profile_field_current c SET source_account_id=r.source_account_id FROM profile_field_revisions r
            WHERE c.current_revision_id=r.id AND r.source_account_id=:account
              AND c.source_account_id IS DISTINCT FROM r.source_account_id'''),args)
        db.execute(text('''INSERT INTO source_account_fields(account_id,field_key,revision_id)
            SELECT :account,c.field_key,c.current_revision_id FROM profile_field_current c WHERE c.source_account_id=:account
            ON CONFLICT(account_id,field_key) DO UPDATE SET revision_id=EXCLUDED.revision_id
            WHERE source_account_fields.revision_id IS DISTINCT FROM EXCLUDED.revision_id'''),args)
        for kind in ('address','employment','education'):
            db.execute(text(f'''UPDATE profile_{kind}_revisions SET source_account_id=:account
                WHERE profile_id=:profile AND source_type=:source AND source_record_id=:external AND source_account_id IS NULL'''),args)
            db.execute(text(f'''UPDATE profile_structured_current c SET source_account_id=:account FROM profile_{kind}_revisions r
                WHERE c.current_revision_id=r.id AND r.source_account_id=:account
                  AND c.source_account_id IS DISTINCT FROM CAST(:account AS uuid)'''),args)
        db.execute(text('''INSERT INTO source_account_facts(account_id,fact_type,fact_key,revision_id)
            SELECT :account,c.fact_type,c.fact_key,c.current_revision_id FROM profile_structured_current c WHERE c.source_account_id=:account
            ON CONFLICT(account_id,fact_type,fact_key) DO UPDATE SET revision_id=EXCLUDED.revision_id
            WHERE source_account_facts.revision_id IS DISTINCT FROM EXCLUDED.revision_id'''),args)
