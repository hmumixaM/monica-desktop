import json

import psycopg
from sqlalchemy import text


def test_desktop_status_counts_committed_cursors_and_excludes_private_details(crm_db, monkeypatch):
    from monica_desktop import ops
    schema = crm_db.execute(text('SELECT current_schema()')).scalar_one()
    crm_db.execute(text("INSERT INTO profiles(profile_type,display_name) VALUES('person','Fixture')"))
    crm_db.execute(text("""INSERT INTO message_source_cursors(source_file,source_table,last_local_id,source_count,projected_count)
        VALUES('message_0.db','Msg_fixture',20,25,17)"""))
    crm_db.execute(text("""INSERT INTO source_runtime(source,phase,status,details) VALUES('archives','messages','running',
        CAST(:details AS jsonb))"""), {'details': json.dumps({'messages_inserted':17,'body':'private message','path':'private/source'})})
    crm_db.commit()
    url = str(crm_db.get_bind().engine.url.render_as_string(hide_password=False)).replace('postgresql+psycopg:', 'postgresql:')
    monkeypatch.setattr(ops, 'connection', lambda: psycopg.connect(url, options=f'-csearch_path={schema},public'))
    result = ops.status()
    assert result['profiles'] == 1
    assert result['posts_imported'] == 0
    assert result['messages_imported'] == 17, 'use committed insert counts, not highest local ID or scanned source count'
    assert result['import_phases'][0]['progress'] == {'messages_inserted':17}
    assert 'private' not in json.dumps(result)
