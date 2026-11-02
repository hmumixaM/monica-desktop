import json
import sqlite3
import threading
from contextlib import contextmanager, nullcontext

import pytest
from sqlalchemy import text
from test_account_links import source_engine as source_engine  # noqa: PLC0414
from test_wechat_contacts import contact_database

from app import runtime_status, sync_archives, wechat_contacts
from app.account_links import set_link


def configure(db, source_engine, monkeypatch, source_first):
    monkeypatch.setattr(wechat_contacts, 'platform_engine', lambda _: source_engine)
    monkeypatch.setattr(runtime_status, 'STOP', threading.Event())
    if source_first:
        db.execute(text("INSERT INTO platform_migration_state(provider,enabled) VALUES('wechat',true)"))
    db.commit()


@pytest.mark.parametrize('source_first', [False, True])
def test_wal_only_new_friend_imports_and_creates_distinct_profile(crm_db, source_engine, tmp_path, monkeypatch, source_first):
    db = crm_db
    configure(db, source_engine, monkeypatch, source_first)
    path = tmp_path/'contact.db'
    contact_database(path)
    writer = sqlite3.connect(path)
    try:
        writer.execute('PRAGMA journal_mode=WAL')
        writer.execute('PRAGMA wal_autocheckpoint=0')
        writer.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        initial = path.stat()
        assert wechat_contacts.sync_contacts(db, path)['profiles_created'] == 1
        first = db.execute(text("SELECT profile_id FROM source_account_links")).scalar_one()
        db.commit()
        # Same name must never be used to merge two separate source accounts.
        writer.execute("INSERT INTO contact(username,local_type,delete_flag,remark) VALUES('wxid_new',1,0,'好友备注')")
        writer.commit()
        assert (path.stat().st_size, path.stat().st_mtime_ns) == (initial.st_size, initial.st_mtime_ns)
        result = wechat_contacts.sync_contacts(db, path)
        assert result['profiles_created'] == 1
        links = db.execute(text("SELECT a.external_id,l.profile_id FROM source_accounts a JOIN source_account_links l ON l.account_id=a.id")).all()
        assert len(links) == 2 and len({row.profile_id for row in links}) == 2
        assert dict(links)['wxid_friend'] == first
        assert db.execute(text("SELECT count(*) FROM profiles WHERE display_name='好友备注'")).scalar_one() == 2
        batches = db.execute(text("SELECT count(*) FROM import_batches")).scalar_one()
        db.commit()
        assert wechat_contacts.sync_contacts(db, path)['profiles_created'] == 0
        assert db.execute(text("SELECT count(*) FROM import_batches")).scalar_one() == batches
    finally:
        writer.close()


@pytest.mark.parametrize('source_first', [False, True])
def test_explicit_unlink_is_not_undone_by_repair(crm_db, source_engine, tmp_path, monkeypatch, source_first):
    db = crm_db
    configure(db, source_engine, monkeypatch, source_first)
    path = tmp_path/'contact.db'
    contact_database(path)
    wechat_contacts.sync_contacts(db, path)
    account, profile = db.execute(text('SELECT account_id,profile_id FROM source_account_links')).one()
    set_link(db, account, None, expected_profile_id=profile)
    db.commit()
    with sqlite3.connect(path) as source:
        source.execute("UPDATE contact SET remark='changed friend' WHERE username='wxid_friend'")
    assert wechat_contacts.sync_contacts(db, path)['profiles_created'] == 0
    assert db.execute(text('SELECT count(*) FROM source_account_links')).scalar_one() == 0
    assert db.execute(text('SELECT count(*) FROM profiles')).scalar_one() == 1


@pytest.mark.parametrize('source_first', [False, True])
def test_preexisting_unlinked_friend_account_is_repaired(crm_db, source_engine, tmp_path, monkeypatch, source_first):
    from app.platform_store import account_id
    db = crm_db
    configure(db, source_engine, monkeypatch, source_first)
    path = tmp_path/'contact.db'
    contact_database(path)
    db.execute(text("INSERT INTO source_accounts(id,provider,external_id,object_kind,display_name) VALUES(:id,'wechat','wxid_friend','person','old')"),
               {'id': account_id('wechat', 'wxid_friend')})
    db.commit()
    result = wechat_contacts.sync_contacts(db, path)
    assert result['new_source_accounts'] == 0 and result['profiles_created'] == 1
    assert db.execute(text("SELECT display_name FROM profiles")).scalar_one() == '好友备注'
    assert db.execute(text("SELECT count(*) FROM profile_field_current WHERE field_key='wechat.remark'")).scalar_one() == 1


def test_contact_budget_resumes_committed_batch_without_rewriting_source(crm_db, source_engine, tmp_path, monkeypatch):
    from types import SimpleNamespace

    configure(crm_db, source_engine, monkeypatch, False)
    path = tmp_path / 'contact.db'
    contact_database(path)
    clock = [0]
    monkeypatch.setattr(wechat_contacts, 'time', SimpleNamespace(monotonic=lambda: clock[0]))
    real_create = wechat_contacts.create_friend_profiles

    def create(*args, **kwargs):
        result = real_create(*args, **kwargs)
        clock[0] = 20
        return result

    monkeypatch.setattr(wechat_contacts, 'create_friend_profiles', create)
    result = wechat_contacts.sync_contacts(crm_db, path, work_budget_seconds=10)
    assert result['stopped_at_checkpoint'] and result['profiles_created'] == 1

    def unexpected_write(*args):
        raise AssertionError('Committed source batch was replayed')

    monkeypatch.setattr(wechat_contacts, 'platform_engine', unexpected_write)
    result = wechat_contacts.sync_contacts(crm_db, path, work_budget_seconds=10)
    assert not result.get('stopped_at_checkpoint') and result['profiles_created'] == 0
    assert crm_db.execute(text('SELECT count(*) FROM profiles')).scalar_one() == 1


def test_upgrade_repairs_unchanged_contact_source_once_and_retries_failure(tmp_path, monkeypatch):
    source, staging = tmp_path/'source', tmp_path/'staging'
    (source/'contact').mkdir(parents=True)
    path = source/'contact/contact.db'
    contact_database(path)
    destination = staging/'continuous-snapshots/contacts'
    destination.mkdir(parents=True)
    sync_archives.snapshot_sqlite([path], destination)
    state_file = destination.parent/'state.json'
    state_file.write_text(json.dumps({'contacts': sync_archives.signature([path])}))
    stop = threading.Event()
    monkeypatch.setattr(runtime_status, 'STOP', stop)
    monkeypatch.setenv('CHATLOG_DB_ROOT', str(source))
    monkeypatch.setenv('STAGING_ROOT', str(staging))
    monkeypatch.setattr('app.jobs.heavy_io', lambda _: nullcontext())
    monkeypatch.setattr(sync_archives, 'SessionLocal', lambda: nullcontext(object()))

    @contextmanager
    def phase(*args):
        yield {}

    monkeypatch.setattr(runtime_status, 'phase', phase)
    monkeypatch.setattr(runtime_status, 'record', lambda source, phase, *args: stop.set() if phase == 'contacts' else None)
    calls = []

    def repair(db, snapshot):
        calls.append(snapshot)
        stop.set()
        if len(calls) == 1:
            raise RuntimeError('temporary failure')
        return {'profiles_created': 1}

    monkeypatch.setattr(sync_archives, 'sync_contacts', repair)
    assert sync_archives.cycle() == ['contacts']
    assert 'contacts_import_version' not in json.loads(state_file.read_text())
    stop.clear()
    assert sync_archives.cycle() == []
    assert json.loads(state_file.read_text())['contacts_import_version'] == wechat_contacts.CONTACT_IMPORT_VERSION
    stop.clear()
    sync_archives.cycle()
    assert len(calls) == 2
