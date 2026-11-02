"""Reversible account links and rebuildable CRM indexes, never Profile merges."""
import json
import os

from fastapi import HTTPException
from sqlalchemy import text

from .link_ownership import BLOCKER_REASONS, link_blockers
from .ownership import lock_accounts, lock_profiles, related_accounts


def rebuild_profile(db,profile_id):
    if not profile_id:return
    lock_profiles(db, [profile_id])
    args={'profile':profile_id}
    db.execute(text('DELETE FROM profile_field_current WHERE profile_id=:profile AND source_account_id IS NOT NULL'),args)
    db.execute(text('DELETE FROM profile_structured_current WHERE profile_id=:profile AND source_account_id IS NOT NULL'),args)
    db.execute(text('''INSERT INTO profile_field_current(profile_id,field_key,current_revision_id,
        current_content_hash,current_source_type,updated_at,source_account_id)
        SELECT DISTINCT ON(r.field_key) CAST(:profile AS uuid),r.field_key,r.id,r.content_hash,r.source_type,r.observed_at,r.source_account_id
        FROM source_account_fields f JOIN source_account_links l ON l.account_id=f.account_id
        JOIN profile_field_revisions r ON r.id=f.revision_id
        WHERE l.profile_id=:profile ORDER BY r.field_key,r.revision_seq DESC
        ON CONFLICT(profile_id,field_key) DO NOTHING'''),args)
    for kind in ('address','employment','education'):
        db.execute(text(f'''INSERT INTO profile_structured_current(profile_id,fact_type,fact_key,current_revision_id,
            current_content_hash,current_source_type,updated_at,source_account_id)
            SELECT CAST(:profile AS uuid),:kind,r.fact_key,r.id,r.content_hash,r.source_type,r.observed_at,r.source_account_id
            FROM source_account_facts f JOIN source_account_links l ON l.account_id=f.account_id
            JOIN profile_{kind}_revisions r ON r.id=f.revision_id
            WHERE l.profile_id=:profile AND f.fact_type=:kind
            ON CONFLICT(profile_id,fact_type,fact_key) DO NOTHING'''),{**args,'kind':kind})
    rebuild_media(db, profile_id)
    db.execute(text('DELETE FROM tag_memberships WHERE profile_id=:profile AND source_account_id IS NOT NULL'),args)
    db.execute(text('''INSERT INTO tag_memberships(tag_id,profile_id,source_type,source_account_id)
        SELECT t.tag_id,l.profile_id,a.provider,t.account_id FROM source_account_tags t
        JOIN source_account_links l ON l.account_id=t.account_id JOIN source_accounts a ON a.id=t.account_id
        WHERE l.profile_id=:profile AND NOT EXISTS(SELECT 1 FROM tag_membership_removals r
          WHERE r.tag_id=t.tag_id AND r.profile_id=l.profile_id) ON CONFLICT DO NOTHING'''),args)
    db.execute(text('DELETE FROM profile_metrics_current WHERE profile_id=:profile'),args)


def rebuild_media(db, profile_id):
    """Rebuild source media projection without touching facts, tags or metrics."""
    if not profile_id:
        return
    lock_profiles(db, [profile_id])
    args = {'profile': profile_id}
    db.execute(text("DELETE FROM media_links WHERE entity_type='profile' AND entity_id=:profile AND source_account_id IS NOT NULL"),args)
    db.execute(text('''INSERT INTO media_links(entity_type,entity_id,media_id,role,source_account_id)
        SELECT 'profile',l.profile_id,m.media_id,m.role,m.account_id FROM source_account_media m
        JOIN source_account_links l ON l.account_id=m.account_id WHERE l.profile_id=:profile ON CONFLICT DO NOTHING'''),args)
    db.execute(text('''UPDATE profiles p SET avatar_media_id=(SELECT l.media_id FROM media_links l JOIN media_assets a ON a.id=l.media_id
        WHERE l.entity_type='profile' AND l.entity_id=p.id AND l.role IN ('selected_avatar','wechat_avatar','instagram_avatar','linkedin_avatar')
        ORDER BY CASE l.role WHEN 'selected_avatar' THEN 0 WHEN 'wechat_avatar' THEN 1 ELSE 2 END,a.created_at DESC LIMIT 1)
        WHERE p.id=:profile AND (p.avatar_media_id IS NULL OR EXISTS(SELECT 1 FROM source_account_media m WHERE m.media_id=p.avatar_media_id))'''),args)


def set_link(db,account_id,profile_id,*,actor=None,expected_profile_id=None,evidence=None):
    # Maintenance may still take the exclusive barrier; ordinary imports and
    # account links share it and synchronize only on affected accounts.
    retirement = expected_profile_id is not None or bool(db.execute(text('''SELECT 1 FROM source_account_link_revisions
        WHERE account_id=:account AND old_profile_id IS NOT NULL LIMIT 1'''), {'account': account_id}).first())
    if profile_id and not retirement:
        retirement = bool(db.execute(text('SELECT 1 FROM profiles WHERE id=:id AND archived_at IS NOT NULL'), {'id': profile_id}).first())
    # First links still run concurrently. Moves/redirect chains use the exclusive
    # barrier from the outset, never a shared-to-exclusive lock upgrade.
    barrier = 'pg_advisory_xact_lock' if retirement or os.environ.get('MONICA_SCOPED_OWNERSHIP_LOCKS') != '1' else 'pg_advisory_xact_lock_shared'
    db.execute(text(f'SELECT {barrier}(11803295)'))
    neighbors = related_accounts(db, account_id)
    lock_accounts(db, [account_id], neighbors)
    account=db.execute(text('SELECT * FROM source_accounts WHERE id=:id FOR UPDATE'),{'id':account_id}).mappings().one_or_none()
    if not account:raise HTTPException(status_code=404,detail='Source account not found')
    # An account with ambiguous ownership may always be detached from a Profile;
    # a quarantined legacy identity may never change.
    blocker=link_blockers(db,[account_id]).get(str(account_id))
    if blocker=='quarantined' or (profile_id and blocker):
        raise HTTPException(status_code=409,detail=BLOCKER_REASONS[blocker])
    old=db.execute(text('SELECT profile_id FROM source_account_links WHERE account_id=:id'),{'id':account_id}).scalar_one_or_none()
    neighbor_profiles = db.execute(text('SELECT profile_id FROM source_account_links WHERE account_id=ANY(CAST(:ids AS uuid[]))'),
                                   {'ids': [str(x) for x in neighbors]}).scalars().all()
    lock_profiles(db, [old, profile_id, *neighbor_profiles])
    if str(old or '')!=str(expected_profile_id or ''):
        raise HTTPException(status_code=409,detail='The account link changed; refresh before confirming')
    if str(old or '')==str(profile_id or ''):return {'account_id':str(account_id),'profile_id':str(profile_id) if profile_id else None,'changed':False}
    if profile_id:
        from .profile_retirement import restore
        if retirement:
            restore(db, profile_id, actor)
        target=db.execute(text('SELECT profile_type FROM profiles WHERE id=:id AND archived_at IS NULL'),{'id':profile_id}).scalar_one_or_none()
        if not target:raise HTTPException(status_code=404,detail='Profile not found')
        if target!=account['object_kind']:raise HTTPException(status_code=422,detail='People link to person accounts; groups link to group accounts')
    proof=json.dumps(evidence or {'kind':'manual-confirmation'})
    args={'account':account_id,'profile':profile_id,'old':old,'actor':actor,'evidence':proof,'neighbors':[str(p) for p in neighbor_profiles]}
    db.execute(text('DELETE FROM source_account_links WHERE account_id=:account'),args)
    if profile_id:
        db.execute(text("""INSERT INTO source_account_links(account_id,profile_id,evidence,linked_by)
            VALUES(:account,:profile,CAST(:evidence AS jsonb),:actor)"""),args)
    db.execute(text("""INSERT INTO source_account_link_revisions(account_id,old_profile_id,new_profile_id,actor_user_id,evidence)
        VALUES(:account,:old,:profile,:actor,CAST(:evidence AS jsonb))"""),args)
    # These rows are CRM indexes of independently stored source objects.
    # No source payload, source revision, media file, or Profile is modified.
    for table,column,account_column in [
        ('identities','profile_id','source_account_id'),('social_posts','profile_id','source_account_id'),
        ('social_stories','profile_id','source_account_id'),('conversations','profile_id','source_account_id'),
        ('social_interactions','author_profile_id','author_account_id'),
    ]:
        db.execute(text(f'UPDATE {table} SET {column}=:profile WHERE {account_column}=:account AND {column} IS DISTINCT FROM :profile'),args)
    if profile_id:
        # A newly collected account has no legacy identity row. Create its
        # display/search projection only after explicit human confirmation.
        db.execute(text("""INSERT INTO identities(profile_id,provider,external_id,username,profile_url,display_name,source_account_id)
            SELECT :profile,:provider,:external,:username,:url,:name,:account
            WHERE NOT EXISTS(SELECT 1 FROM identities WHERE source_account_id=:account)
            ON CONFLICT(provider,external_id) DO NOTHING"""),
            {**args,'provider':'wechat_group' if account['object_kind']=='group' else account['provider'],
             'external':account['external_id'],'username':account['username'],'url':account['profile_url'],'name':account['display_name']})
    rebuild_profile(db,old)
    rebuild_profile(db,profile_id)
    # Memberships are derived from both account links. A disconnected room or
    # person retains its source membership but disappears from Profile queries.
    if old:
        db.execute(text("""DELETE FROM group_memberships m USING source_group_memberships s
            LEFT JOIN source_account_links g ON g.account_id=s.group_account_id
            LEFT JOIN source_account_links p ON p.account_id=s.person_account_id
            WHERE (s.group_account_id=:account AND m.group_profile_id=:old AND m.person_profile_id=p.profile_id)
               OR (s.person_account_id=:account AND m.person_profile_id=:old AND m.group_profile_id=g.profile_id)"""),args)
    db.execute(text("""INSERT INTO group_memberships(group_profile_id,person_profile_id,role,joined_at,left_at,source_record_id)
        SELECT DISTINCT ON (g.profile_id,p.profile_id) g.profile_id,p.profile_id,s.role,s.joined_at,s.left_at,s.source_record_id
        FROM source_group_memberships s JOIN source_account_links g ON g.account_id=s.group_account_id
        JOIN source_account_links p ON p.account_id=s.person_account_id
        WHERE s.group_account_id=:account OR s.person_account_id=:account
          OR (g.profile_id IN (:old,:profile) AND p.profile_id=ANY(CAST(:neighbors AS uuid[])))
          OR (p.profile_id IN (:old,:profile) AND g.profile_id=ANY(CAST(:neighbors AS uuid[])))

        ORDER BY g.profile_id,p.profile_id,s.group_account_id,s.person_account_id
        ON CONFLICT(group_profile_id,person_profile_id) DO UPDATE SET role=EXCLUDED.role,
          joined_at=EXCLUDED.joined_at,left_at=EXCLUDED.left_at,source_record_id=EXCLUDED.source_record_id"""),args)
    db.execute(text('DELETE FROM relationship_edges WHERE from_account_id=:account OR to_account_id=:account'),args)
    db.execute(text('''INSERT INTO relationship_edges(id,from_profile_id,to_profile_id,relationship_type_id,note,confidence,
        valid_from,valid_to,source_type,evidence_kind,from_account_id,to_account_id)
        SELECT r.id,a.profile_id,b.profile_id,r.relationship_type_id,r.note,r.confidence,r.valid_from,r.valid_to,
          r.source_type,r.evidence_kind,r.from_account_id,r.to_account_id
        FROM source_relationship_edges r JOIN source_account_links a ON a.account_id=r.from_account_id
        JOIN source_account_links b ON b.account_id=r.to_account_id
        WHERE (r.from_account_id=:account OR r.to_account_id=:account
) AND a.profile_id<>b.profile_id
        ON CONFLICT DO NOTHING'''),args)
    if retirement:
        from .profile_retirement import KIND, reconcile
        origins = db.execute(text("SELECT old_profile_id FROM profile_redirects WHERE evidence->>'kind'=:kind"), {'kind': KIND}).scalars().all()
        reconcile(db, [old, *origins], apply=True, actor=actor)
    from .ai.link_changes import refresh_after_link
    refresh_after_link(db, [old, profile_id])
    return {'account_id':str(account_id),'profile_id':str(profile_id) if profile_id else None,'changed':True}
