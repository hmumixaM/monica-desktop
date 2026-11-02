"""Archive empty profiles left behind by confirmed account moves (dry run by default)."""

import argparse
import json

from sqlalchemy import text

from .db import SessionLocal

# Only profiles with an actual detach history qualify. Any surviving source,
# personal fact, media, message, activity or relationship protects the profile.
EMPTY_DETACHED = """p.profile_type='person' AND p.archived_at IS NULL
    AND nullif(btrim(p.summary),'') IS NULL AND p.avatar_media_id IS NULL
    AND EXISTS(SELECT 1 FROM source_account_link_revisions r WHERE r.old_profile_id=p.id)
    AND NOT EXISTS(SELECT 1 FROM source_account_links l WHERE l.profile_id=p.id)
""" + "\n".join(
    f"AND NOT EXISTS(SELECT 1 FROM {table} x WHERE x.{column}=p.id)"
    for table, column in (
        ('profile_field_current', 'profile_id'), ('profile_structured_current', 'profile_id'),
        ('profile_dates', 'profile_id'), ('tag_memberships', 'profile_id'),
        ('activity_participants', 'profile_id'), ('social_posts', 'profile_id'),
        ('social_stories', 'profile_id'), ('conversations', 'profile_id'),
        ('group_memberships', 'person_profile_id'), ('message_events', 'sender_profile_id'),
        ('relationship_edges', 'from_profile_id'), ('relationship_edges', 'to_profile_id'),
        ('faces', 'profile_id'),
    )
) + """
    AND NOT EXISTS(SELECT 1 FROM media_links x WHERE x.entity_type='profile' AND x.entity_id=p.id)
    AND NOT EXISTS(SELECT 1 FROM identities x WHERE x.profile_id=p.id AND x.status='active')
"""


def archive_empty(db, *, apply=False):
    # Use the same barrier as reversible account moves. Recheck under the lock
    # before applying so no newly attached account can be hidden by this cleanup.
    if apply:
        db.execute(text('SELECT pg_advisory_xact_lock(11803295)'))
    ids = list(db.execute(text(f'SELECT p.id FROM profiles p WHERE {EMPTY_DETACHED} ORDER BY p.id')).scalars())
    if apply and ids:
        db.execute(text('UPDATE profiles SET archived_at=now() WHERE id=ANY(CAST(:ids AS uuid[]))'),
                   {'ids': [str(pid) for pid in ids]})
        for profile_id in ids:
            db.execute(text("""INSERT INTO audit_events(action,entity_type,entity_id,metadata)
                VALUES ('profile.archived','profile',:id,'{"reason":"empty-after-account-detach"}')"""),
                       {'id': profile_id})
    return [str(pid) for pid in ids]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    with SessionLocal() as db:
        ids = archive_empty(db, apply=args.apply)
        if args.apply:
            db.commit()
        print(json.dumps({'applied': args.apply, 'profiles': ids, 'count': len(ids)}))


if __name__ == '__main__':
    main()
