"""Deterministically ordered locks for source ownership and shared projections."""
from sqlalchemy import text


def lock_accounts(db, exclusive, shared=()):
    exclusive = {str(value) for value in exclusive if value is not None}
    for account in sorted(exclusive | {str(value) for value in shared if value is not None}):
        mode = 'UPDATE' if account in exclusive else 'KEY SHARE'
        db.execute(text(f'SELECT id FROM source_accounts WHERE id=CAST(:id AS uuid) FOR {mode}'), {'id': account})


def lock_profiles(db, profiles, *, shared=False):
    for profile in sorted({str(value) for value in profiles if value is not None}):
        function = 'pg_advisory_xact_lock_shared' if shared else 'pg_advisory_xact_lock'
        db.execute(text(f"SELECT {function}(hashtextextended('profile-projection:' || :id, 0))"),
                   {'id': profile})


def related_accounts(db, account):
    return db.execute(text("""SELECT person_account_id FROM source_group_memberships WHERE group_account_id=:id
        UNION SELECT group_account_id FROM source_group_memberships WHERE person_account_id=:id
        UNION SELECT to_account_id FROM source_relationship_edges WHERE from_account_id=:id
        UNION SELECT from_account_id FROM source_relationship_edges WHERE to_account_id=:id"""),
                      {'id': account}).scalars().all()
