from uuid import uuid4

from sqlalchemy import text
from test_account_links import source_engine as source_engine  # noqa: PLC0414
from test_media_integration import db as db  # noqa: PLC0414

from app.account_links import set_link
from app.platform_migrate import register_accounts
from app.profile_retirement import candidate, reconcile, resolve_profile, restore
from app.profile_summary import build_summary
from app.repositories import append_field_revision


def seed(db):
    a, b, c = [uuid4() for _ in range(3)]
    for pid in (a, b, c):
        db.execute(text("INSERT INTO profiles(id,profile_type,display_name) VALUES(:id,'person','Person')"), {'id': pid})
    db.execute(text("INSERT INTO identities(profile_id,provider,external_id) VALUES(:id,'linkedin','account')"), {'id': a})
    register_accounts(db, 'linkedin')
    db.execute(text('UPDATE source_accounts SET migrated_at=now()'))
    db.execute(text("INSERT INTO platform_migration_state(provider,enabled) VALUES('linkedin',true)"))
    account = db.execute(text('SELECT id FROM source_accounts')).scalar_one()
    return a, b, c, account


def test_retirement_follows_current_owner_and_unlink_restores(db, source_engine):
    a, b, c, account = seed(db)
    set_link(db, account, b, expected_profile_id=a)
    assert resolve_profile(db, a) == b
    assert build_summary(db, a)['profile']['id'] == b
    set_link(db, account, c, expected_profile_id=b)
    assert resolve_profile(db, a) == c
    assert resolve_profile(db, b) == c
    set_link(db, account, None, expected_profile_id=c)
    assert resolve_profile(db, a) == a
    assert resolve_profile(db, b) == b
    assert db.execute(text('SELECT count(*) FROM profiles WHERE archived_at IS NOT NULL')).scalar_one() == 0


def test_explicit_restore_remains_visible_and_preserves_source_history(db, source_engine):
    a, b, _c, account = seed(db)
    set_link(db, account, b, expected_profile_id=a)
    assert restore(db, a)
    assert candidate(db, a)['blockers'] == ['manual-profile']
    reconcile(db, apply=True)
    assert resolve_profile(db, a) == a
    assert db.execute(text('SELECT count(*) FROM source_account_link_revisions')).scalar_one() > 0
    assert db.execute(text('SELECT count(*) FROM source_accounts')).scalar_one() == 1


def test_manual_fact_and_dry_run_are_protected(db, source_engine):
    a, b, _c, account = seed(db)
    append_field_revision(db, profile_id=a, field_key='note', value='Keep this', source_type='manual', operation='manual_edit')
    set_link(db, account, b, expected_profile_id=a)
    assert 'manual-history' in candidate(db, a)['blockers']
    assert resolve_profile(db, a) == a
    before = db.execute(text('SELECT count(*) FROM audit_events')).scalar_one()
    reconcile(db)
    assert db.execute(text('SELECT count(*) FROM audit_events')).scalar_one() == before
