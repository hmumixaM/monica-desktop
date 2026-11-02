import os
from collections.abc import Generator

from fastapi import HTTPException
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import OperationalError
from sqlalchemy.exc import TimeoutError as PoolTimeout
from sqlalchemy.orm import Session, sessionmaker

from .config import get_settings

settings = get_settings()
_url_options = make_url(settings.database_url).query.get("options", "")
engine = create_engine(settings.database_url, pool_pre_ping=True,
                       pool_size=int(os.environ.get("DB_POOL_SIZE", "2")),
                       max_overflow=int(os.environ.get("DB_MAX_OVERFLOW", "1")), pool_timeout=5,
                       connect_args={"options": _url_options + " -c lock_timeout=5000"})
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
# Interactive requests must fail fast instead of queueing behind a disk-bound
# import; batch work keeps the unbounded engine above.
api_engine = create_engine(
    settings.database_url,
    pool_pre_ping=True,
    pool_size=int(os.environ.get("API_DB_POOL_SIZE", "5")), max_overflow=2, pool_timeout=5,
    connect_args={"options": _url_options + f" -c statement_timeout={settings.api_statement_timeout_ms} -c lock_timeout=3000"},
)
ApiSessionLocal = sessionmaker(bind=api_engine, autoflush=False, expire_on_commit=False)


def get_db() -> Generator[Session, None, None]:
    db = ApiSessionLocal()
    try:
        yield db
    except OperationalError as exc:
        code = getattr(exc.orig, 'sqlstate', '') or ''
        if code in ('55P03', '57014', '40P01', '40001') or code.startswith('08') or exc.connection_invalidated:
            raise HTTPException(503, 'Database is busy; retry this request', headers={'Retry-After': '2'}) from None
        raise
    except PoolTimeout:
        raise HTTPException(503, 'Database capacity is busy; retry this request', headers={'Retry-After': '2'}) from None
    finally:
        db.close()


def migrate() -> None:
    """Run ordered, idempotent SQL migrations."""
    from pathlib import Path

    if settings.migrations_root is not None:
        if not settings.migrations_root.is_dir():
            raise FileNotFoundError("Configured MIGRATIONS_ROOT directory not found")
        with engine.connect() as connection:
            run_migrations(connection, settings.migrations_root)
        return
    candidates = [Path("/migrations")]
    source_path = Path(__file__).resolve()
    if len(source_path.parents) > 3:
        candidates.append(source_path.parents[3] / "migrations")
    migrations_dir = next((path for path in candidates if path.exists()), None)
    if migrations_dir is None:
        raise FileNotFoundError("migrations directory not found")
    with engine.connect() as connection:
        run_migrations(connection, migrations_dir)


def run_migrations(connection, migrations_dir):
    """Pin one physical session for the migration lock, commit each file.

    A file beginning ``-- monica: autocommit`` must contain one idempotent
    statement (e.g. CREATE INDEX CONCURRENTLY). It is recorded only after success.
    """
    connection.execute(text("SET lock_timeout = '5s'"))
    connection.execute(text("SET statement_timeout = '120s'"))
    locked = connection.execute(text('SELECT pg_try_advisory_lock(11803351)')).scalar_one()
    connection.commit()
    try:
        if not locked:
            raise RuntimeError('Another deployment is running migrations')
        with connection.begin():
            connection.execute(text("""CREATE TABLE IF NOT EXISTS schema_migrations (
                version TEXT PRIMARY KEY,applied_at TIMESTAMPTZ NOT NULL DEFAULT now())"""))
            applied = set(connection.execute(text('SELECT version FROM schema_migrations')).scalars())
        for path in sorted(migrations_dir.glob('*.sql')):
            if path.name in applied:
                continue
            sql = path.read_text(encoding='utf-8')
            if sql.startswith('-- monica: autocommit'):
                connection.execution_options(isolation_level='AUTOCOMMIT')
                try:
                    connection.execute(text(sql))
                    connection.commit()
                finally:
                    connection.rollback()
                    connection.execution_options(isolation_level='READ COMMITTED')
                with connection.begin():
                    connection.execute(text('INSERT INTO schema_migrations(version) VALUES(:version)'), {'version': path.name})
            else:
                with connection.begin():
                    connection.execute(text(sql))
                    connection.execute(text('INSERT INTO schema_migrations(version) VALUES(:version)'), {'version': path.name})
    finally:
        connection.rollback()
        if locked and not connection.execute(text('SELECT pg_advisory_unlock(11803351)')).scalar_one():
            raise RuntimeError('Migration lock ownership lost')
        connection.execute(text('RESET lock_timeout'))
        connection.execute(text('RESET statement_timeout'))
        connection.commit()
