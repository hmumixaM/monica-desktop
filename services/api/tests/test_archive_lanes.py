import json
import sqlite3
from uuid import uuid4

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from app import runtime_status, sync_archives


def test_moments_lane_uses_committed_mapping_and_never_runs_contact_or_media_work(crm_db, tmp_path, monkeypatch):
    source = tmp_path / 'source/sns'
    source.mkdir(parents=True)
    with sqlite3.connect(source / 'sns.db') as db:
        db.execute('CREATE TABLE SnsTimeLine(tid INTEGER,user_name TEXT,content TEXT)')
        db.execute('INSERT INTO SnsTimeLine VALUES(1,?,?)', ('wx_fixture',
            ('<root><TimelineObject><id>123</id><username>wx_fixture</username><createTime>1700000000</createTime>'
             '<contentDesc>Fixture</contentDesc></TimelineObject></root>')))
    schema = crm_db.execute(text('SELECT current_schema()')).scalar_one()
    crm_db.commit()
    engine = create_engine(crm_db.get_bind().engine.url.update_query_dict({'options': '-csearch_path=' + schema + ',public'}))
    monkeypatch.setattr(sync_archives, 'engine', engine)
    monkeypatch.setattr(sync_archives, 'SessionLocal', sessionmaker(engine))
    monkeypatch.setattr(runtime_status, 'record', lambda *_args: None)
    monkeypatch.setenv('MONICA_ARCHIVE_SEPARATED', '1')
    monkeypatch.setenv('CHATLOG_DB_ROOT', str(source.parent))
    monkeypatch.setenv('STAGING_ROOT', str(tmp_path / 'staging'))
    def unexpected(*_args, **_kwargs):
        raise AssertionError('Unrelated contact/media/maintenance work blocked the Moments lane')
    for name in ('sync_contacts', 'consume_media_import_queue', 'refresh_projections', 'moments_cache'):
        monkeypatch.setattr(sync_archives, name, unexpected)
    state_path = tmp_path / 'staging/continuous-snapshots/state-moments.json'
    try:
        assert sync_archives.cycle(kinds={'moments'}, phase_names=set(), state_name=state_path.name) == []
        before = json.loads(state_path.read_text())
        person = uuid4()
        crm_db.execute(text("INSERT INTO profiles(id,profile_type,display_name) VALUES(:id,'person','Fixture')"), {'id': person})
        crm_db.execute(text("INSERT INTO identities(provider,external_id,profile_id) VALUES('wechat','wx_fixture',:id)"), {'id': person})
        # The cache-version sequence has advanced, but these rows are not
        # committed. This iteration must not acknowledge their future mapping.
        assert sync_archives.cycle(kinds={'moments'}, phase_names=set(), state_name=state_path.name) == []
        assert json.loads(state_path.read_text()) == before
        crm_db.commit()
        assert sync_archives.cycle(kinds={'moments'}, phase_names=set(), state_name=state_path.name) == []
        assert json.loads(state_path.read_text())['moments_links_version'] != before['moments_links_version']
        assert crm_db.execute(text("SELECT profile_id FROM social_posts WHERE external_id='123'")).scalar_one() == person
    finally:
        engine.dispose()
