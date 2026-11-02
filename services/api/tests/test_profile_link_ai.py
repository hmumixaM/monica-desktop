from uuid import uuid4

from sqlalchemy import text

from app.ai.link_changes import refresh_after_link
from app.archive_detached_profiles import archive_empty


def person(db):
    return db.execute(text("INSERT INTO profiles(profile_type,display_name) VALUES('person','Person') RETURNING id")).scalar_one()


def account(db, pid):
    aid = uuid4()
    db.execute(text("INSERT INTO source_accounts(id,provider,external_id,object_kind,display_name,migrated_at) VALUES(:id,'instagram',:ext,'person','Name',now())"), {'id': aid, 'ext': str(aid)})
    db.execute(text("INSERT INTO source_account_links(account_id,profile_id,evidence) VALUES(:id,:p,'{}')"), {'id': aid, 'p': pid})
    return aid


def output(db, pid, kind='profile_memory', suffix=''):
    db.execute(text("INSERT INTO ai_outputs(kind,subject,input_hash,model,output) VALUES(:kind,:subject,:hash,'test','{\"summary\":\"Old identity\",\"facts\":[],\"gaps\":[]}')"), {'kind': kind, 'subject': f'{pid}{suffix}', 'hash': 'a'*64})


def week(db, pid):
    db.execute(text("INSERT INTO person_weekly_updates(profile_id,week_start,input_hash,model,output) VALUES(:id,'2026-10-05',:hash,'test','{\"summary\":\"Old week\",\"sources\":[]}')"), {'id': pid, 'hash': 'b'*64})


def test_move_rebuilds_both_people_without_copying_old_identity(crm_db, monkeypatch):
    from app.account_links import set_link
    monkeypatch.setenv('AI_ENABLED', 'true')
    db = crm_db
    old, target = person(db), person(db)
    aid = account(db, old)
    account(db, old)
    for pid in (old, target):
        output(db, pid)
        output(db, pid, 'profile_memory_chunk', ':source:1')
    week(db, old)
    set_link(db, aid, target, expected_profile_id=old)
    assert db.execute(text('SELECT count(*) FROM ai_outputs')).scalar_one() == 0
    assert db.execute(text('SELECT count(*) FROM person_weekly_updates')).scalar_one() == 0
    jobs = db.execute(text('SELECT job_type,payload FROM jobs')).all()
    assert {p['profile_id'] for kind, p in jobs if kind == 'ai.profile'} == {str(old), str(target)}
    assert {(p['profile_id'], p['week_start']) for kind, p in jobs if kind == 'ai.person_week'} == {(str(old), '2026-10-05'), (str(target), '2026-10-05')}
    output(db, target)
    assert not set_link(db, aid, target, expected_profile_id=target)['changed']
    assert db.execute(text('SELECT count(*) FROM ai_outputs')).scalar_one() == 1


def test_empty_or_archived_people_skip_model(crm_db, monkeypatch):
    from app.ai import person_weekly, profiles
    db = crm_db
    pid = person(db)
    monkeypatch.setattr(profiles.person_memory, 'refresh', lambda *_: (_ for _ in ()).throw(AssertionError('model should not run')))
    assert profiles.refresh_one(db, {'profile_id': str(pid)})['skipped']
    db.execute(text('UPDATE profiles SET archived_at=now() WHERE id=:id'), {'id': pid})
    assert person_weekly.build_one(db, {'profile_id': str(pid), 'week_start': '2026-10-05'})['skipped'] == 'inactive person'


def test_archive_protects_manual_live_and_never_linked_people(crm_db):
    db = crm_db
    empty, manual, live, _bare = (person(db) for _ in range(4))
    aid = account(db, live)
    for pid in (empty, manual, live):
        db.execute(text("INSERT INTO source_account_link_revisions(account_id,old_profile_id,new_profile_id,evidence) VALUES(:id,:old,:new,'{}')"), {'id': aid, 'old': pid, 'new': live})
    db.execute(text("UPDATE profiles SET summary='My own note' WHERE id=:id"), {'id': manual})
    output(db, empty)
    assert archive_empty(db) == [str(empty)]
    assert db.execute(text('SELECT archived_at FROM profiles WHERE id=:id'), {'id': empty}).scalar_one() is None
    assert archive_empty(db, apply=True) == [str(empty)]
    assert archive_empty(db) == []
    assert db.execute(text('SELECT count(*) FROM ai_outputs')).scalar_one() == 1
    assert db.execute(text("SELECT count(*) FROM audit_events WHERE action='profile.archived'")).scalar_one() == 1


def test_merged_source_week_is_rebuilt_for_target(crm_db, monkeypatch):
    monkeypatch.setenv('AI_ENABLED', 'true')
    db = crm_db
    old, target = person(db), person(db)
    account(db, target)
    output(db, old)
    db.execute(text('UPDATE profiles SET archived_at=now() WHERE id=:id'), {'id': old})
    week(db, old)
    refresh_after_link(db, [old, target])
    assert {p['profile_id'] for p in db.execute(text('SELECT payload FROM jobs')).scalars()} == {str(target)}
