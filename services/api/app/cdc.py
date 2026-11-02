"""Asynchronous append-only PostgreSQL WAL history; never drives CRM updates.

Capture may lag without delaying source intake, projection, or media joins.
Only mutable business tables are published. Existing append-only revisions and
message archives remain their own history and are not duplicated into this log.
"""
import argparse
import hashlib
import json

from sqlalchemy import text

from .cdc_codec import transactions
from .config import get_settings
from .platform_store import PROVIDERS, platform_engine

AUDIT_PROVIDERS = (*PROVIDERS, 'crm')
SOURCE_TABLES = ('accounts', 'account_identifiers', 'objects', 'media_links')
CRM_TABLES = ('profiles', 'identities', 'source_account_links', 'profile_field_current',
              'profile_structured_current', 'activities', 'media_links')
PUBLICATION = 'monica_cdc'


def audit_engine(provider):
    if provider == 'crm':
        from .db import engine
        return engine
    return platform_engine(provider)


def slot_name(provider, database):
    return 'monica_' + provider + '_' + hashlib.sha256(database.encode()).hexdigest()[:12]


def initialize(crm):
    """Provision all slots BEFORE assigning a CRM transaction ID.

    A missing established slot fails closed. Callers may report this history
    error while leaving the independent application pipeline running.
    """
    recorded = set(crm.execute(text('SELECT provider FROM cdc_consumers WHERE established')).scalars())
    ready = []
    for provider in AUDIT_PROVIDERS:
        with audit_engine(provider).connect().execution_options(isolation_level='AUTOCOMMIT') as source:
            if source.execute(text('SHOW wal_level')).scalar_one() != 'logical':
                raise RuntimeError('CDC history requires PostgreSQL wal_level=logical')
            database = source.execute(text('SELECT current_database()')).scalar_one()
            schema = source.execute(text('SELECT current_schema()')).scalar_one()
            quote = source.dialect.identifier_preparer.quote_identifier
            names = CRM_TABLES if provider == 'crm' else SOURCE_TABLES
            tables = []
            for name in names:
                relation = quote(schema) + '.' + quote(name)
                source.exec_driver_sql(f'ALTER TABLE {relation} REPLICA IDENTITY FULL')
                tables.append(relation + (" WHERE (object_kind <> 'message')" if name == 'objects' else ''))
            joined = ','.join(tables)
            if source.execute(text('SELECT 1 FROM pg_publication WHERE pubname=:name'), {'name': PUBLICATION}).first():
                source.exec_driver_sql(f'ALTER PUBLICATION {PUBLICATION} SET TABLE {joined}')
            else:
                source.exec_driver_sql(f'CREATE PUBLICATION {PUBLICATION} FOR TABLE {joined}')
            slot = slot_name(provider, database)
            existing = source.execute(text('SELECT plugin,database,wal_status FROM pg_replication_slots WHERE slot_name=:slot'), {'slot': slot}).mappings().one_or_none()
            if existing:
                if existing['plugin'] != 'pgoutput' or existing['database'] != database or existing['wal_status'] == 'lost':
                    raise RuntimeError('CDC history slot is incompatible or WAL was lost; explicit recovery required')
            elif provider in recorded:
                raise RuntimeError('CDC history slot disappeared; refusing to silently skip history')
            else:
                source.execute(text("SELECT * FROM pg_create_logical_replication_slot(:slot,'pgoutput')"), {'slot': slot})
            ready.append((provider, slot))
    for provider, slot in ready:
        crm.execute(text("""INSERT INTO cdc_consumers(provider,slot_name,state,established) VALUES(:provider,:slot,'ready',true)
            ON CONFLICT(provider) DO UPDATE SET state='ready',error_code=NULL,established=true"""), {'provider': provider, 'slot': slot})


def persist_transaction(crm, provider, batch):
    rows = []
    for ordinal, change in enumerate(batch['changes']):
        names = CRM_TABLES if provider == 'crm' else SOURCE_TABLES
        if change['relation'] not in names:
            raise ValueError('Unexpected relation in CDC publication')
        before, after = change['before'], change['after']
        # Observation/refresh timestamps alone are not a business change.
        ignored = {'last_seen_at', 'updated_at'}
        if before is not None and after is not None and (
                {k:v for k,v in before.items() if k not in ignored} ==
                {k:v for k,v in after.items() if k not in ignored}):
            continue
        rows.append({'ordinal': ordinal, 'relation': change['relation'], 'operation': change['operation'],
                     'before': before, 'after': after})
    if not rows:
        return 0
    inserted = crm.execute(text("""INSERT INTO cdc_events(provider,commit_lsn,ordinal,relation_name,operation,
        before_row,after_row,committed_at) SELECT :provider,CAST(:lsn AS pg_lsn),r.ordinal,r.relation,r.operation,r.before,r.after,:committed
        FROM jsonb_to_recordset(CAST(:rows AS jsonb)) AS r(ordinal int,relation text,operation text,before jsonb,after jsonb)
        ORDER BY r.ordinal ON CONFLICT(provider,commit_lsn,ordinal) DO NOTHING RETURNING id"""),
        {'provider': provider, 'lsn': batch['commit_lsn'], 'committed': batch['committed_at'], 'rows': json.dumps(rows)}).all()
    return len(inserted)


def capture_provider(provider, crm_engine, *, before_ack=None):
    """Capture one bounded WAL batch. Readers never hold a CRM lock during I/O."""
    with audit_engine(provider).connect().execution_options(isolation_level='AUTOCOMMIT') as source:
        if not source.execute(text('SELECT pg_try_advisory_lock(11803411)')).scalar_one():
            return 0
        try:
            database = source.execute(text('SELECT current_database()')).scalar_one()
            slot = slot_name(provider, database)
            watermark = source.execute(text('SELECT pg_current_wal_flush_lsn()')).scalar_one()
            messages = source.execute(text("""SELECT data FROM pg_logical_slot_peek_binary_changes(
                :slot,CAST(:upto AS pg_lsn),500,'proto_version','1','publication_names',:publication)"""),
                {'slot': slot, 'upto': watermark, 'publication': PUBLICATION}).scalars().all()
            batches = transactions(messages)
            end = batches[-1]['end_lsn'] if batches else watermark
            count = 0
            retained = source.execute(text('SELECT pg_wal_lsn_diff(pg_current_wal_lsn(),restart_lsn) FROM pg_replication_slots WHERE slot_name=:slot'), {'slot': slot}).scalar_one()
            with crm_engine.begin() as crm:
                for batch in batches:
                    count += persist_transaction(crm, provider, batch)
                crm.execute(text("""INSERT INTO cdc_consumers(provider,slot_name,captured_lsn,retained_wal_bytes,state,established)
                    VALUES(:provider,:slot,CAST(:lsn AS pg_lsn),:retained,'ready',true)
                    ON CONFLICT(provider) DO UPDATE SET captured_lsn=EXCLUDED.captured_lsn,heartbeat_at=now(),
                    retained_wal_bytes=EXCLUDED.retained_wal_bytes,state='ready',error_code=NULL,established=true
                    WHERE :active OR cdc_consumers.heartbeat_at<now()-interval '15 seconds' OR cdc_consumers.state<>'ready'"""),
                    {'lsn': end, 'provider': provider, 'slot': slot, 'retained': int(retained or 0), 'active': bool(batches)})
            if before_ack:
                before_ack()
            source.execute(text('SELECT * FROM pg_replication_slot_advance(:slot,CAST(:lsn AS pg_lsn))'), {'slot': slot, 'lsn': end})
            return count
        finally:
            source.execute(text('SELECT pg_advisory_unlock(11803411)'))


def main():
    from .db import engine
    from .runtime_status import STOP, install_shutdown

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('watch', 'initialize'))
    args = parser.parse_args()
    if not get_settings().cdc_enabled:
        raise SystemExit('CDC_ENABLED is required')
    if args.command == 'initialize':
        with engine.begin() as crm:
            initialize(crm)
        return
    install_shutdown()
    while not STOP.is_set():
        active = False
        for provider in AUDIT_PROVIDERS:
            if STOP.is_set():
                break
            try:
                active = bool(capture_provider(provider, engine)) or active
            except Exception as error:  # noqa: BLE001 - history failures must not block intake
                code = getattr(getattr(error, 'orig', None), 'sqlstate', None) or type(error).__name__
                slot = slot_name(provider, audit_engine(provider).url.database)
                with engine.begin() as crm:
                    crm.execute(text("""INSERT INTO cdc_consumers(provider,slot_name,state,error_code)
                        VALUES(:provider,:slot,'error',:code) ON CONFLICT(provider) DO UPDATE SET
                        state='error',error_code=EXCLUDED.error_code,heartbeat_at=now()"""),
                        {'provider': provider, 'slot': slot, 'code': code})
                print(json.dumps({'history_capture': provider, 'error_code': code}), flush=True)
        # Idle capture can lag. During a backfill, drain bounded batches fairly
        # across all databases instead of retaining 30 seconds of WAL per batch.
        STOP.wait(1 if active else 30)


if __name__ == '__main__':
    main()
