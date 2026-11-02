import os
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from app import aggregate_cache


@pytest.fixture(autouse=True)
def isolated_staging_root(tmp_path, monkeypatch):
    monkeypatch.setenv("STAGING_ROOT", str(tmp_path / "staging"))
    aggregate_cache.clear_memory()


@pytest.fixture
def crm_db(monkeypatch, tmp_path):
    """A migrated CRM schema isolated by search_path, dropped afterwards."""
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL is required for isolated PostgreSQL integration tests")
    engine = create_engine(url)
    schema = "test_" + uuid4().hex
    with engine.connect() as connection:
        connection.execute(text(f"CREATE SCHEMA {schema}"))
        connection.execute(text(f"SET search_path TO {schema}, public"))
        for path in sorted((Path(__file__).parents[3] / "migrations").glob("*.sql")):
            connection.exec_driver_sql(path.read_text())
        connection.commit()
        from app.config import get_settings

        monkeypatch.setenv("RAW_ROOT", str(tmp_path / "raw"))
        get_settings.cache_clear()
        try:
            with Session(bind=connection) as session:
                yield session
        finally:
            connection.rollback()
            connection.execute(text("SET search_path TO public"))
            connection.execute(text(f"DROP SCHEMA {schema} CASCADE"))
            connection.commit()
            get_settings.cache_clear()
    engine.dispose()
