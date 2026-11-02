import os
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from app.platform_store import ensure_account, observe, observe_many


def test_independent_databases_preserve_account_history_without_profiles():
    url=os.environ.get('TEST_DATABASE_URL')
    if not url:pytest.skip('TEST_DATABASE_URL is required')
    admin=create_engine(make_url(url).set(database='postgres'),isolation_level='AUTOCOMMIT')
    databases=[];engines=[]
    try:
        for provider in ('wechat','instagram','linkedin'):
            name='platform_test_'+uuid4().hex;databases.append(name)
            with admin.connect() as connection:connection.exec_driver_sql(f'CREATE DATABASE "{name}"')
            engine=create_engine(make_url(url).set(database=name));engines.append(engine)
            with engine.begin() as connection:
                connection.execute(text((Path(__file__).parents[1]/'app/platform_schema.sql').read_text()))
                aid=ensure_account(connection,provider,'account_1',display_name='Same name')
                for event,value in [('one','A'),('two','B'),('three','A'),('four','A')]:
                    observe(connection,aid,'field','biography',value,source_event=event)
                assert observe(connection,aid,'field','biography','A',source_event='three')['state']=='replayed'
                assert connection.execute(text('SELECT count(*) FROM payloads')).scalar_one()==2
                assert connection.execute(text('SELECT count(*) FROM revisions')).scalar_one()==3
                assert connection.execute(text('SELECT count(*) FROM observations')).scalar_one()==4
                assert connection.execute(text("SELECT count(*) FROM information_schema.columns WHERE table_schema='public' AND column_name='profile_id'")).scalar_one()==0
                other=ensure_account(connection,provider,'account_2',display_name='Same name')
                assert other!=aid
                with pytest.raises(ValueError,match='different content'):
                    observe(connection,aid,'field','biography','C',source_event='three')
                batch=[{'account':aid,'kind':'post','external_id':'p1','value':v,'source_event':str(n)}
                       for n,v in enumerate(['A','B','A','A'])]
                observe_many(connection,batch)
                observe_many(connection,batch)
                assert connection.execute(text("SELECT revision_number FROM objects WHERE object_kind='post'")).scalar_one()==3
                observe_many(connection,[{**batch[0],'value':'B','source_event':'five'}])
                assert connection.execute(text("SELECT revision_number FROM objects WHERE object_kind='post'")).scalar_one()==4
        assert len({engine.url.database for engine in engines})==3
        for engine in engines:
            with engine.connect() as connection:
                assert connection.execute(text('SELECT count(*) FROM accounts')).scalar_one()==2
    finally:
        for engine in engines:engine.dispose()
        with admin.connect() as connection:
            for name in databases:connection.exec_driver_sql(f'DROP DATABASE "{name}" WITH (FORCE)')
        admin.dispose()
