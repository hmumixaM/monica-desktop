"""Independent source databases, keyed by real accounts and immutable events.

The CRM owns links to these accounts. Source databases never store a Profile ID,
so unlinking or relinking cannot alter an account's data, media, or revisions.
"""
import argparse
import hashlib
import json
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from uuid import UUID, uuid5

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from .config import get_settings
from .content import canonical_json

PROVIDERS=('wechat','instagram','linkedin')
NAMESPACE=UUID('534b6268-97a1-4d99-a086-f8c9e3955815')


def account_id(provider,external_id):
    if provider not in PROVIDERS:raise ValueError('Unknown platform')
    if not str(external_id).strip():raise ValueError('An actual source account ID is required')
    return uuid5(NAMESPACE,f'{provider}:account:{external_id}')


def object_id(account,kind,external_id):
    return uuid5(UUID(str(account)),f'{kind}:{external_id}')


def database_url(provider):
    if provider not in PROVIDERS:raise ValueError('Unknown platform')
    settings=get_settings()
    explicit=getattr(settings,f'{provider}_database_url')
    source=make_url(explicit) if explicit else make_url(settings.database_url).set(database=make_url(settings.database_url).database+'_'+provider)
    crm=make_url(settings.database_url)
    if (source.host,source.port,source.database)==(crm.host,crm.port,crm.database):
        raise ValueError('Platform data must use a database separate from CRM')
    return source


@lru_cache(maxsize=3)
def platform_engine(provider):
    return create_engine(database_url(provider),pool_pre_ping=True,pool_size=2,max_overflow=1,pool_timeout=5,
                         connect_args={"options": "-c lock_timeout=5000"})


def initialize(provider):
    """Provision explicitly from the CLI; ordinary requests never create databases."""
    url=database_url(provider)
    admin=create_engine(url.set(database='postgres'),isolation_level='AUTOCOMMIT')
    try:
        with admin.connect() as connection:
            exists=connection.execute(text('SELECT 1 FROM pg_database WHERE datname=:name'),{'name':url.database}).scalar_one_or_none()
            if not exists:
                quoted=connection.dialect.identifier_preparer.quote_identifier(url.database)
                connection.exec_driver_sql(f'CREATE DATABASE {quoted}')
    finally:admin.dispose()
    apply_schema(provider)


def apply_schema(provider):
    """Bring an existing platform database to the current schema; every statement is idempotent."""
    with platform_engine(provider).begin() as connection:
        connection.execute(text(Path(__file__).with_name('platform_schema.sql').read_text()))


def ensure_account(connection,provider,external_id,*,kind='person',display_name=None,username=None,profile_url=None,identifiers=()):
    aid=account_id(provider,external_id)
    if kind=='group' and provider!='wechat':raise ValueError('Only WeChat has group accounts')
    if provider=='wechat' and (str(external_id).endswith('@chatroom')!=(kind=='group')):
        raise ValueError('WeChat account kind must match the source ID')
    existing = connection.execute(text('SELECT display_name,username,profile_url FROM accounts WHERE id=:id'), {'id': aid}).mappings().one_or_none()
    name = display_name or str(external_id)
    same = (existing and existing['display_name'] == name
            and existing['username'] == (username if username is not None else existing['username'])
            and existing['profile_url'] == (profile_url if profile_url is not None else existing['profile_url']))
    if not same:
        connection.execute(text("""INSERT INTO accounts(id,external_id,object_kind,display_name,username,profile_url)
        VALUES(:id,:external,:kind,:name,:username,:url) ON CONFLICT(id) DO UPDATE SET
        display_name=EXCLUDED.display_name,username=COALESCE(EXCLUDED.username,accounts.username),
        profile_url=COALESCE(EXCLUDED.profile_url,accounts.profile_url)
        WHERE (accounts.display_name,accounts.username,accounts.profile_url)
          IS DISTINCT FROM (EXCLUDED.display_name,COALESCE(EXCLUDED.username,accounts.username),COALESCE(EXCLUDED.profile_url,accounts.profile_url))"""),
        {'id':aid,'external':external_id,'kind':kind,'name':display_name or str(external_id),'username':username,'url':profile_url})
    for identifier in sorted({str(external_id),*(str(i) for i in identifiers)}):
        existing=connection.execute(text('SELECT account_id FROM account_identifiers WHERE identifier=:key'),{'key':identifier}).scalar_one_or_none()
        if existing and existing!=aid:raise ValueError('Conflicting source identifier; human review required')
        if existing == aid:
            continue
        inserted = connection.execute(text('INSERT INTO account_identifiers(identifier,account_id) VALUES(:key,:id) ON CONFLICT DO NOTHING RETURNING account_id'),{'key':identifier,'id':aid}).scalar_one_or_none()
        if inserted is None and connection.execute(text('SELECT account_id FROM account_identifiers WHERE identifier=:key'), {'key': identifier}).scalar_one() != aid:
            raise ValueError('Conflicting source identifier; human review required')
    return aid


def observe(connection,account,kind,external_id,value,*,source_event,observed_at=None,occurred_at=None):
    """One payload per hash, one revision per transition; A→B→A keeps all three."""
    if not source_event:raise ValueError('A stable observation ID is required')
    oid=object_id(account,kind,external_id);stamp=observed_at or datetime.now(UTC)
    encoded=canonical_json(value);digest=hashlib.sha256(encoded).hexdigest()
    connection.execute(text('SELECT pg_advisory_xact_lock(hashtextextended(:key,0))'),{'key':str(oid)})
    previous=connection.execute(text('SELECT content_hash FROM observations WHERE object_id=:id AND source_event=:event'),{'id':oid,'event':source_event}).scalar_one_or_none()
    if previous:
        if previous!=digest:raise ValueError('Observation ID was reused with different content')
        return {'object_id':oid,'state':'replayed','hash':digest}
    current=connection.execute(text('SELECT current_hash,revision_number FROM objects WHERE id=:id'),{'id':oid}).mappings().one_or_none()
    connection.execute(text('INSERT INTO payloads(sha256,value) VALUES(:hash,CAST(:value AS jsonb)) ON CONFLICT DO NOTHING'),{'hash':digest,'value':encoded.decode()})
    connection.execute(text("""INSERT INTO objects(id,account_id,object_kind,external_id,occurred_at)
        VALUES(:id,:account,:kind,:external,:at) ON CONFLICT DO NOTHING"""),
        {'id':oid,'account':account,'kind':kind,'external':external_id,'at':occurred_at})
    changed=not current or current['current_hash']!=digest
    if changed:
        number=current['revision_number']+1 if current else 1
        connection.execute(text("""INSERT INTO revisions(object_id,revision_number,content_hash,observed_at,source_event)
            VALUES(:id,:number,:hash,:at,:event)"""),{'id':oid,'number':number,'hash':digest,'at':stamp,'event':source_event})
        connection.execute(text("""UPDATE objects SET current_hash=:hash,revision_number=:number,
            occurred_at=COALESCE(:at,occurred_at) WHERE id=:id"""),{'id':oid,'hash':digest,'number':number,'at':occurred_at})
    connection.execute(text("""INSERT INTO observations(object_id,source_event,content_hash,observed_at)
        VALUES(:id,:event,:hash,:at)"""),{'id':oid,'event':source_event,'hash':digest,'at':stamp})
    return {'object_id':oid,'state':'new' if not current else 'changed' if changed else 'unchanged','hash':digest}


def observe_many(connection, records):
    """Bounded source batch, preserving ordered transitions and replay identity.

    Uses one set of SQL statements per batch, including messages. Callers commit
    the source before the rebuildable CRM projection and retain the same event
    IDs on retries. A batch must not contain duplicate object/event pairs.
    """
    rows=[]
    for index,record in enumerate(records):
        encoded=canonical_json(record['value'])
        rows.append({'position':index,'id':str(object_id(record['account'],record['kind'],record['external_id'])),
                     'account':str(record['account']),'kind':record['kind'],'external':str(record['external_id']),
                     'hash':hashlib.sha256(encoded).hexdigest(),'value':json.loads(encoded),
                     'event':record['source_event'],'observed':(record.get('observed_at') or datetime.now(UTC)).isoformat(),
                     'occurred':record.get('occurred_at').isoformat() if record.get('occurred_at') else None})
    if not rows:return []
    if any(not r['event'] for r in rows) or len({(r['id'],r['event']) for r in rows})!=len(rows):
        raise ValueError('Source observations need distinct stable event IDs')
    connection.execute(text('''CREATE TEMP TABLE IF NOT EXISTS source_input (
        position int,id uuid,account uuid,kind text,external text,hash char(64),value jsonb,
        event text,observed timestamptz,occurred timestamptz) ON COMMIT DELETE ROWS'''))
    connection.execute(text('TRUNCATE source_input'))
    connection.execute(text('''INSERT INTO source_input SELECT * FROM jsonb_to_recordset(CAST(:rows AS jsonb)) AS r(
        position int,id uuid,account uuid,kind text,external text,hash char(64),value jsonb,
        event text,observed timestamptz,occurred timestamptz)'''),{'rows':json.dumps(rows,ensure_ascii=False)})
    # Autovacuum cannot analyze a connection's temporary table. Without these
    # batch statistics PostgreSQL may scan the growing observations archive
    # for every small incremental batch on the NAS.
    connection.execute(text('ANALYZE source_input'))
    connection.execute(text('SELECT pg_advisory_xact_lock(hashtextextended(id::text,0)) FROM (SELECT DISTINCT id FROM source_input ORDER BY id) ids'))
    if connection.execute(text('''SELECT 1 FROM source_input i JOIN observations o ON o.object_id=i.id AND o.source_event=i.event
        WHERE o.content_hash<>i.hash LIMIT 1''')).scalar_one_or_none():
        raise ValueError('Observation ID was reused with different content')
    connection.execute(text('''DELETE FROM source_input i USING observations o WHERE o.object_id=i.id AND o.source_event=i.event'''))
    connection.execute(text('''INSERT INTO payloads(sha256,value) SELECT DISTINCT ON(hash) hash,value FROM source_input ORDER BY hash ON CONFLICT DO NOTHING'''))
    connection.execute(text('''INSERT INTO objects(id,account_id,object_kind,external_id)
        SELECT DISTINCT ON(id) id,account,kind,external FROM source_input ORDER BY id ON CONFLICT DO NOTHING'''))
    connection.execute(text('''WITH transitions AS (
        SELECT i.*,o.revision_number,lag(i.hash,1,o.current_hash) OVER(PARTITION BY i.id ORDER BY position) AS previous
        FROM source_input i JOIN objects o ON o.id=i.id
      ), changed AS (
        SELECT *,revision_number+row_number() OVER(PARTITION BY id ORDER BY position) AS number
        FROM transitions WHERE hash IS DISTINCT FROM previous
      ) INSERT INTO revisions(object_id,revision_number,content_hash,observed_at,source_event)
        SELECT id,number,hash,observed,event FROM changed ORDER BY id,number'''))
    connection.execute(text('''UPDATE objects o SET current_hash=r.content_hash,revision_number=r.revision_number,
        occurred_at=COALESCE(i.occurred,o.occurred_at)
        FROM (SELECT DISTINCT ON(id) id,occurred FROM source_input ORDER BY id,position DESC) i,
          LATERAL (SELECT content_hash,revision_number FROM revisions WHERE object_id=i.id ORDER BY revision_number DESC LIMIT 1) r
        WHERE o.id=i.id AND (o.current_hash,o.revision_number,o.occurred_at)
          IS DISTINCT FROM (r.content_hash,r.revision_number,COALESCE(i.occurred,o.occurred_at))'''))
    connection.execute(text('''INSERT INTO observations(object_id,source_event,content_hash,observed_at)
        SELECT id,event,hash,observed FROM source_input ORDER BY id,event'''))
    return [{'object_id':UUID(r['id']),'hash':r['hash']} for r in rows]


CRM_REFERENCED_KINDS = ('message',)


def payload_values(source, crm, hashes):
    """Values by hash; reference-only payloads resolve from CRM content_objects."""
    rows=source.execute(text('SELECT sha256,value,crm_content_kind FROM payloads WHERE sha256=ANY(:hashes)'),
                        {'hashes':list(hashes)}).all()
    values={r.sha256:r.value for r in rows if r.value is not None}
    by_kind={}
    for r in rows:
        if r.value is None:by_kind.setdefault(r.crm_content_kind,[]).append(r.sha256)
    for kind,missing in by_kind.items():
        values.update(crm.execute(text("""SELECT sha256,payload_json FROM content_objects
            WHERE content_kind=:kind AND sha256=ANY(:hashes)"""),{'kind':kind,'hashes':missing}).all())
    return values


def resolve_message_records(connection, records):
    """Keep a proven legacy message UUID; refuse ambiguous timezone aliases.

    A match requires the same account, stable source ID and exact UTC instant.
    Hash equality alone is deliberately insufficient to establish identity.
    """
    from .source_time import message_identity
    requests = []
    identities = {}
    for index, record in enumerate(records):
        identity = message_identity(record['external_id']) if record['kind'] == 'message' else None
        if identity:
            identities[index] = identity
            requests.append({'position': index, 'account': str(record['account']), 'at': identity[1].isoformat()})
    if not requests:
        return records
    candidates = connection.execute(text("""SELECT r.position,o.external_id FROM
        jsonb_to_recordset(CAST(:requests AS jsonb)) r(position int,account uuid,at timestamptz)
        JOIN objects o ON o.account_id=r.account AND o.object_kind='message' AND o.occurred_at=r.at"""),
        {'requests': json.dumps(requests)}).all()
    matches = {}
    for index, external in candidates:
        if message_identity(external) == identities[index]:
            matches.setdefault(index, []).append(external)
    resolved = list(records)
    for index, aliases in matches.items():
        if len(aliases) != 1:
            raise ValueError('Ambiguous legacy message keys require audit: ' + str(aliases))
        resolved[index] = {**records[index], 'external_id': aliases[0]}
    return resolved


def observe_immutable_many(connection, records):
    """Store immutable events without synthetic revision or observation history."""
    records = resolve_message_records(connection, records)
    rows=[]
    for index,record in enumerate(records):
        encoded=canonical_json(record['value'])
        rows.append({'position':index,'id':str(object_id(record['account'],record['kind'],record['external_id'])),
                     'account':str(record['account']),'kind':record['kind'],'external':str(record['external_id']),
                     'hash':hashlib.sha256(encoded).hexdigest(),'value':json.loads(encoded),
                     'occurred':record.get('occurred_at').isoformat() if record.get('occurred_at') else None})
    if not rows:return []
    if len({r['id'] for r in rows})!=len(rows):
        raise ValueError('Immutable source batch contains duplicate object IDs')
    connection.execute(text('''CREATE TEMP TABLE IF NOT EXISTS immutable_source_input (
        position int,id uuid,account uuid,kind text,external text,hash char(64),value jsonb,
        occurred timestamptz) ON COMMIT DELETE ROWS'''))
    connection.execute(text('TRUNCATE immutable_source_input'))
    connection.execute(text('''INSERT INTO immutable_source_input
        SELECT * FROM jsonb_to_recordset(CAST(:rows AS jsonb)) AS r(
        position int,id uuid,account uuid,kind text,external text,hash char(64),value jsonb,
        occurred timestamptz)'''),{'rows':json.dumps(rows,ensure_ascii=False)})
    connection.execute(text('ANALYZE immutable_source_input'))
    mismatch=connection.execute(text('''SELECT 1 FROM immutable_source_input i JOIN objects o ON o.id=i.id
        WHERE (o.account_id,o.object_kind,o.external_id,o.current_hash)
          IS DISTINCT FROM (i.account,i.kind,i.external,i.hash) LIMIT 1''')).scalar_one_or_none()
    if mismatch:
        raise ValueError('Immutable source object changed or collided')
    # Message bodies live in CRM content_objects under the same hash.
    connection.execute(text('''INSERT INTO payloads(sha256,value,crm_content_kind)
        SELECT DISTINCT ON(hash) hash,
          CASE WHEN kind=ANY(CAST(:referenced AS text[])) THEN NULL ELSE value END,
          CASE WHEN kind=ANY(CAST(:referenced AS text[])) THEN kind END
        FROM immutable_source_input ORDER BY hash ON CONFLICT DO NOTHING'''),{'referenced':list(CRM_REFERENCED_KINDS)})
    connection.execute(text('''INSERT INTO objects(
        id,account_id,object_kind,external_id,current_hash,revision_number,occurred_at)
        SELECT id,account,kind,external,hash,0,occurred FROM immutable_source_input ORDER BY id
        ON CONFLICT(id) DO NOTHING'''))
    if connection.execute(text('''SELECT 1 FROM immutable_source_input i JOIN objects o ON o.id=i.id
        WHERE (o.account_id,o.object_kind,o.external_id,o.current_hash)
          IS DISTINCT FROM (i.account,i.kind,i.external,i.hash) LIMIT 1''')).scalar_one_or_none():
        raise ValueError('Concurrent immutable source object changed or collided')
    return [{'object_id':UUID(r['id']),'hash':r['hash']} for r in rows]


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command',choices=['initialize','counts'])
    parser.add_argument('--provider',choices=PROVIDERS)
    args=parser.parse_args()
    for provider in (args.provider,) if args.provider else PROVIDERS:
        if args.command=='initialize':initialize(provider)
        with platform_engine(provider).connect() as connection:
            counts={table:connection.execute(text(f'SELECT count(*) FROM {table}')).scalar_one()
                    for table in ('accounts','objects','payloads','revisions','observations')}
            print(json.dumps({'provider':provider,'database':database_url(provider).database,**counts}))
