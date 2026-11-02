"""Reversible retirement of proven empty account-move origins; never delete source data."""
import argparse
import json
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import text

from .auth import current_user
from .db import get_db
from .profile_presentation import ALIASES

KIND = 'empty-after-account-move'
router = APIRouter()


def meaningful(value):
    if isinstance(value, dict):
        return any(meaningful(v) for v in value.values())
    if isinstance(value, list):
        return any(meaningful(v) for v in value)
    return value is not None and (not isinstance(value, str) or bool(value.strip()))


def resolve_profile(db, profile_id):
    seen = set()
    for _ in range(32):
        if str(profile_id) in seen:
            raise HTTPException(409, 'Profile redirect cycle')
        seen.add(str(profile_id))
        target = db.execute(text('''SELECT r.profile_id FROM profile_redirects r
            JOIN profiles p ON p.id=r.old_profile_id WHERE r.old_profile_id=:id AND p.archived_at IS NOT NULL'''),
            {'id': profile_id}).scalar_one_or_none()
        if target is None:
            return profile_id
        profile_id = target
    raise HTTPException(409, 'Too many profile redirects')


def audit(db, profile_id, action, actor=None, evidence=None):
    db.execute(text('''INSERT INTO audit_events(user_id,action,entity_type,entity_id,metadata)
        VALUES(:actor,:action,'profile',:id,CAST(:proof AS jsonb))'''),
        {'actor': actor, 'action': action, 'id': profile_id, 'proof': json.dumps(evidence or {})})


def restore(db, profile_id, actor=None, *, explicit=True):
    db.execute(text('SELECT pg_advisory_xact_lock(11803295)'))
    row = db.execute(text('''SELECT r.evidence FROM profile_redirects r JOIN profiles p ON p.id=r.old_profile_id
        WHERE r.old_profile_id=:id AND r.evidence->>'kind'=:kind FOR UPDATE OF p'''),
        {'id': profile_id, 'kind': KIND}).scalar_one_or_none()
    if row is None:
        return False
    db.execute(text('DELETE FROM profile_redirects WHERE old_profile_id=:id'), {'id': profile_id})
    db.execute(text('UPDATE profiles SET archived_at=NULL WHERE id=:id'), {'id': profile_id})
    audit(db, profile_id, 'profile.restored' if explicit else 'profile.auto_restored', actor)
    return True


def candidate(db, profile_id):
    args = {'id': profile_id}
    row = db.execute(text('SELECT * FROM profiles WHERE id=:id'), args).mappings().one()
    result = {'profile_id': str(profile_id), 'target_id': None, 'accounts': [], 'blockers': []}
    blockers = result['blockers']
    if row['profile_type'] != 'person':
        blockers.append('not-person')
    if db.execute(text('SELECT 1 FROM source_account_links WHERE profile_id=:id LIMIT 1'), args).first():
        blockers.append('linked-accounts')
    if blockers:
        return result
    accounts = db.execute(text('''SELECT DISTINCT r.account_id,l.profile_id,p.archived_at
        FROM source_account_link_revisions r LEFT JOIN source_account_links l ON l.account_id=r.account_id
        LEFT JOIN profiles p ON p.id=l.profile_id
        WHERE r.old_profile_id=:id AND r.old_profile_id IS DISTINCT FROM r.new_profile_id'''), args).mappings().all()
    targets = {str(a['profile_id']) for a in accounts if a['profile_id'] and a['archived_at'] is None}
    if not accounts or len(targets) != 1 or any(a['profile_id'] is None or a['archived_at'] for a in accounts) or str(profile_id) in targets:
        blockers.append('no-unique-current-target')
    else:
        result['target_id'] = next(iter(targets))
    result['accounts'] = [str(a['account_id']) for a in accounts]
    if meaningful(row['summary']) or row['avatar_media_id']:
        blockers.append('summary-or-avatar')
    guards = {
        'manual-profile': "SELECT 1 FROM audit_events WHERE entity_id=:id AND entity_type='profile' AND action NOT IN ('profile.auto_archived','profile.auto_restored')",
        'manual-history': "SELECT 1 FROM profile_field_revisions WHERE profile_id=:id AND (actor_user_id IS NOT NULL OR source_type='manual')",
        'identity': "SELECT 1 FROM identities WHERE profile_id=:id AND provider<>'monica' AND status NOT IN ('invalid','merged')",
        'structured-data': 'SELECT 1 FROM profile_structured_current WHERE profile_id=:id',
        'dates': 'SELECT 1 FROM profile_dates WHERE profile_id=:id',
        'activities': 'SELECT 1 FROM activity_participants WHERE profile_id=:id',
        'faces': "SELECT 1 FROM faces WHERE profile_id=:id AND status='confirmed'",
        'media': "SELECT 1 FROM media_links WHERE entity_type='profile' AND entity_id=:id",
        'relationships': 'SELECT 1 FROM relationship_edges WHERE from_profile_id=:id OR to_profile_id=:id',
        'groups': 'SELECT 1 FROM group_memberships WHERE person_profile_id=:id OR group_profile_id=:id',
        'tags': 'SELECT 1 FROM tag_memberships WHERE profile_id=:id',
        'posts': 'SELECT 1 FROM social_posts WHERE profile_id=:id',
        'stories': 'SELECT 1 FROM social_stories WHERE profile_id=:id',
        'conversations': 'SELECT 1 FROM conversations WHERE profile_id=:id',
        'legacy-messages': 'SELECT 1 FROM message_events WHERE sender_profile_id=:id AND sender_account_id IS NULL',
    }
    for reason, sql in guards.items():
        if db.execute(text(sql + ' LIMIT 1'), args).first():
            blockers.append(reason)
    # A legacy platform value is redundant only if the destination has the SAME
    # value through a currently linked source account. Unknown values stay protected.
    fields = db.execute(text('''SELECT c.field_key,o.payload_json AS value
        FROM profile_field_current c JOIN profile_field_revisions r ON r.id=c.current_revision_id
        JOIN content_objects o ON o.id=r.content_object_id WHERE c.profile_id=:id'''), args).mappings()
    target_values = {}
    if result['target_id']:
        target_values = dict(db.execute(text('''SELECT c.field_key,o.payload_json FROM profile_field_current c
            JOIN profile_field_revisions r ON r.id=c.current_revision_id JOIN content_objects o ON o.id=r.content_object_id
            JOIN source_account_links l ON l.account_id=c.source_account_id AND l.profile_id=c.profile_id
            WHERE c.profile_id=CAST(:target AS uuid)'''), {'target': result['target_id']}).all())
    for field in fields:
        key, value = field['field_key'], field['value']
        if not meaningful(value):
            continue
        if key == 'monica.name' and value == row['display_name']:
            continue
        entries = value.items() if key == 'monica.custom_fields' and isinstance(value, dict) else [(key, value)]
        for label, item in entries:
            if meaningful(item) and target_values.get(ALIASES.get(label, label)) != item:
                blockers.append('independent-field')
                break
    result['blockers'] = sorted(set(blockers))
    return result


def reconcile(db, profile_ids=None, *, apply=False, actor=None):
    if apply:
        # Same ownership barrier as account moves/imports; classifiers never race a relink.
        db.execute(text('SELECT pg_advisory_xact_lock(11803295)'))
    ids = profile_ids if profile_ids is not None else db.execute(text('''SELECT DISTINCT old_profile_id
        FROM source_account_link_revisions WHERE old_profile_id IS NOT NULL''')).scalars().all()
    reports = []
    for pid in sorted({str(p) for p in ids if p}):
        if apply:
            db.execute(text('SELECT id FROM profiles WHERE id=CAST(:id AS uuid) FOR UPDATE'), {'id': pid})
        retired = db.execute(text("SELECT evidence->>'kind' FROM profile_redirects WHERE old_profile_id=CAST(:id AS uuid)"), {'id': pid}).scalar_one_or_none()
        archived = db.execute(text('SELECT archived_at FROM profiles WHERE id=CAST(:id AS uuid)'), {'id': pid}).scalar_one_or_none()
        if archived and retired != KIND:
            continue
        report = candidate(db, UUID(pid))
        reports.append(report)
        if not apply:
            continue
        if report['blockers']:
            if retired == KIND:
                restore(db, UUID(pid), explicit=False)
            continue
        evidence = {'kind': KIND, 'version': 1, 'accounts': report['accounts']}
        db.execute(text('''INSERT INTO profile_redirects(old_profile_id,profile_id,evidence)
            VALUES(CAST(:id AS uuid),CAST(:target AS uuid),CAST(:proof AS jsonb))
            ON CONFLICT(old_profile_id) DO UPDATE SET profile_id=excluded.profile_id,evidence=excluded.evidence'''),
            {'id': pid, 'target': report['target_id'], 'proof': json.dumps(evidence)})
        if not archived:
            db.execute(text('UPDATE profiles SET archived_at=now() WHERE id=CAST(:id AS uuid)'), {'id': pid})
            audit(db, UUID(pid), 'profile.auto_archived', actor, evidence)
    return reports


@router.get('/api/v1/archived-profiles')
def archived_profiles(db=Depends(get_db), _user=Depends(current_user)):
    return [dict(row) for row in db.execute(text('''SELECT p.id,p.display_name,p.archived_at,r.profile_id AS target_id
        FROM profiles p JOIN profile_redirects r ON r.old_profile_id=p.id
        WHERE p.archived_at IS NOT NULL AND r.evidence->>'kind'=:kind ORDER BY p.archived_at DESC'''), {'kind': KIND}).mappings()]


@router.post('/api/v1/profiles/{profile_id}/restore')
def restore_profile(profile_id: UUID, db=Depends(get_db), user=Depends(current_user)):
    if not restore(db, profile_id, user['id']):
        raise HTTPException(404, 'Automatically archived profile not found')
    db.commit()
    return {'id': str(profile_id), 'restored': True}


if __name__ == '__main__':
    from sqlalchemy.orm import Session

    from .db import engine
    parser = argparse.ArgumentParser()
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    with Session(engine) as db:
        reports = reconcile(db, apply=args.apply)
        if args.apply:
            db.commit()
        print(json.dumps({'apply': args.apply, 'eligible': sum(not r['blockers'] for r in reports), 'profiles': reports}))
