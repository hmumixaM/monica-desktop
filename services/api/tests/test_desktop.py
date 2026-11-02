from types import SimpleNamespace
from uuid import uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import text
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient
from test_account_links import source_engine as source_engine  # noqa: PLC0414

from app.config import Settings
from app.desktop_http import DesktopBoundaryMiddleware
from app.desktop_init import account_profile


def test_desktop_settings_require_secrets_and_loopback_origin():
    with pytest.raises(ValidationError, match="generated secret"):
        Settings(_env_file=None, app_env="desktop")
    options = {"_env_file": None, "app_env": "desktop", "session_secret": "s" * 48, "import_hmac_secret": "i" * 48}
    settings = Settings(**options, desktop_origin="http://127.0.0.1:48215")
    assert settings.secure_cookies is False
    assert settings.cors_origin_list == ["http://127.0.0.1:48215"]
    assert Settings(_env_file=None, app_env="production").secure_cookies is True
    for origin in ("http://localhost:48215", "https://127.0.0.1", "http://127.0.0.1/x", "http://user@127.0.0.1", "http://127.0.0.1:bad"):
        with pytest.raises(ValidationError):
            Settings(**options, desktop_origin=origin)


def test_desktop_http_rejects_foreign_hosts_origins_and_peers():
    app = Starlette(routes=[Route("/", lambda request: JSONResponse({"ok": True}), methods=["GET", "POST"])])
    app.add_middleware(DesktopBoundaryMiddleware, origin="http://127.0.0.1:48215")
    with TestClient(app, base_url="http://127.0.0.1:48215", client=("127.0.0.1", 50000)) as client:
        assert client.get("/").status_code == 200
        assert client.post("/", headers={"Origin": "http://127.0.0.1:48215"}).status_code == 200
        for headers in ({"Host": "evil.example"}, {"Host": "127.0.0.1@evil.example"},
                        {"Origin": "null"}, {"Origin": "http://127.0.0.1:48216"},
                        {"Origin": "https://evil.example"}, {"Sec-Fetch-Site": "cross-site"}):
            assert client.post("/", headers=headers).status_code == 403
    with TestClient(app, base_url="http://127.0.0.1", client=("192.168.1.2", 50000)) as client:
        assert client.get("/").status_code == 403


def test_collected_profiles_are_independent_and_unlink_survives_replay(crm_db, monkeypatch):
    from app import desktop_init
    monkeypatch.setattr(desktop_init, "get_settings", lambda: SimpleNamespace(app_env="desktop"))
    ids = [uuid4(), uuid4()]
    for account, provider in zip(ids, ("instagram", "linkedin")):
        crm_db.execute(text("""INSERT INTO source_accounts(id,provider,external_id,object_kind,display_name)
            VALUES(:id,:provider,'same-name','person','Same Name')"""), {"id": account, "provider": provider})
    first = account_profile(crm_db, ids[0], "instagram", "same-name", "person", {"display_name": "Same Name"})
    second = account_profile(crm_db, ids[1], "linkedin", "same-name", "person", {"display_name": "Same Name"})
    assert first != second
    assert account_profile(crm_db, ids[0], "instagram", "same-name", "person", {}) == first
    crm_db.execute(text("DELETE FROM source_account_links WHERE account_id=:id"), {"id": ids[0]})
    assert account_profile(crm_db, ids[0], "instagram", "same-name", "person", {}) is None
    assert crm_db.execute(text("SELECT count(*) FROM profiles")).scalar_one() == 2


def test_existing_server_does_not_create_collected_profiles(crm_db, monkeypatch):
    from app import desktop_init
    monkeypatch.setattr(desktop_init, "get_settings", lambda: SimpleNamespace(app_env="production"))
    assert account_profile(crm_db, uuid4(), "instagram", "someone", "person", {}) is None
    assert crm_db.execute(text("SELECT count(*) FROM profiles")).scalar_one() == 0


def desktop_settings(monkeypatch):
    from app.config import get_settings
    monkeypatch.setenv("APP_ENV", "desktop")
    monkeypatch.setenv("SESSION_SECRET", "s" * 48)
    monkeypatch.setenv("IMPORT_HMAC_SECRET", "i" * 48)
    get_settings.cache_clear()


@pytest.fixture
def desktop_stores(crm_db, monkeypatch):
    import os
    from pathlib import Path

    from sqlalchemy import create_engine
    from sqlalchemy.engine import make_url

    url = make_url(os.environ["TEST_DATABASE_URL"])
    admin = create_engine(url.set(database="postgres"), isolation_level="AUTOCOMMIT")
    stores = {}
    try:
        for provider in ("wechat", "instagram", "linkedin"):
            name = "desktop_test_" + uuid4().hex
            with admin.connect() as connection:
                connection.exec_driver_sql(f'CREATE DATABASE "{name}"')
            engine = create_engine(url.set(database=name))
            stores[provider] = engine
            with engine.begin() as source:
                source.execute(text((Path(__file__).parents[1] / "app/platform_schema.sql").read_text()))
        for module in ("platform_store", "platform_migrate", "source_projection", "source_verification"):
            monkeypatch.setattr(f"app.{module}.platform_engine", stores.__getitem__)
        yield stores
    finally:
        for engine in stores.values():
            engine.dispose()
            with admin.connect() as connection:
                connection.exec_driver_sql(f'DROP DATABASE "{engine.url.database}" WITH (FORCE)')
        admin.dispose()


def test_empty_desktop_initializes_and_imports_without_legacy_ids(crm_db, desktop_stores, tmp_path, monkeypatch):
    import json

    from app.desktop_init import initialize
    from app.linkedin_import import load_linkedin
    from app.reconcile_sources import instagram_profiles

    desktop_settings(monkeypatch)
    assert initialize(crm_db)["changed"] is True
    assert initialize(crm_db)["changed"] is False
    path = tmp_path / "profiles/fresh-person"
    path.mkdir(parents=True)
    (path / "profile.json").write_text(json.dumps({"userid": "12345", "username": "fresh-person", "full_name": "Same Name", "biography": "Fixture"}))
    instagram_profiles(crm_db, path.parent, apply=True)
    snapshot = {"profiles": [{"provider_user_id": "fresh-person", "profile_url": "https://www.linkedin.com/in/fresh-person/",
                              "profile": {"name": "Same Name", "headline": "Fixture"},
                              "snapshots": [{"hash": "a" * 64, "payload": {"name": "Same Name"}}],
                              "posts": [{"provider_post_id": "urn:li:activity:12345", "text": "Fixture post"}]}]}
    load_linkedin(crm_db, tmp_path, {}, export_snapshot=snapshot, incremental=True)
    names = crm_db.execute(text("SELECT display_name FROM profiles")).scalars().all()
    assert names == ["Same Name", "Same Name"]
    assert crm_db.execute(text("SELECT count(*) FROM source_accounts")).scalar_one() == 2
    assert crm_db.execute(text("SELECT count(DISTINCT profile_id) FROM source_account_links")).scalar_one() == 2
    assert crm_db.execute(text("SELECT count(*) FROM social_posts WHERE profile_id IS NOT NULL")).scalar_one() == 1
    assert crm_db.execute(text("SELECT count(*) FROM identities WHERE provider='monica'")).scalar_one() == 0
    assert crm_db.execute(text("SELECT display_name FROM source_accounts WHERE provider='linkedin'")).scalar_one() == "Same Name"
    with desktop_stores["linkedin"].connect() as source:
        revisions = source.execute(text("SELECT count(*) FROM revisions")).scalar_one()
    assert instagram_profiles(crm_db, path.parent, apply=True)["changed_profiles"] == 0
    assert load_linkedin(crm_db, tmp_path, {}, export_snapshot=snapshot, incremental=True) == []
    with desktop_stores["linkedin"].connect() as source:
        assert source.execute(text("SELECT count(*) FROM revisions")).scalar_one() == revisions


def test_desktop_initialization_refuses_existing_unmigrated_data(crm_db, source_engine, monkeypatch):
    from app.desktop_init import initialize
    desktop_settings(monkeypatch)
    crm_db.execute(text("INSERT INTO profiles(profile_type,display_name) VALUES('person','Existing')"))
    with pytest.raises(ValueError, match="Existing data"):
        initialize(crm_db)


def test_late_profile_name_fills_collected_placeholder_but_preserves_manual_name(crm_db, source_engine, monkeypatch):
    from datetime import UTC, datetime

    from app.desktop_init import initialize
    from app.imports import project_batch
    from app.schemas import ImportBatchRequest

    desktop_settings(monkeypatch)
    initialize(crm_db)
    def ingest(key, records):
        project_batch(crm_db, ImportBatchRequest(source="instagram", stream="profiles", batch_id=key,
                      idempotency_key=key, observed_at=datetime.now(UTC), records=records))
        crm_db.commit()
    ingest("post-first", [{"external_id": "post-first", "entity_type": "post", "source_account_external_id": "12345", "content": {"text": "Fixture"}}])
    assert crm_db.execute(text("SELECT display_name FROM profiles")).scalar_one() == "12345"
    ingest("profile-later", [{"external_id": "12345", "content": {"display_name": "Collected Name", "username": "collected"}}])
    assert crm_db.execute(text("SELECT display_name FROM profiles")).scalar_one() == "Collected Name"
    crm_db.execute(text("UPDATE profiles SET display_name='My preferred name'"))
    ingest("profile-refresh", [{"external_id": "12345", "content": {"display_name": "Updated Name", "username": "collected"}}])
    assert crm_db.execute(text("SELECT display_name FROM profiles")).scalar_one() == "My preferred name"
    assert crm_db.execute(text("SELECT count(*) FROM source_accounts")).scalar_one() == 1


def test_wake_notification_received_during_import_ends_idle_once(tmp_path, monkeypatch):
    import threading

    from app import runtime_status
    wake = tmp_path / "wake"
    monkeypatch.setenv("MONICA_DESKTOP_WAKE_FILE", str(wake))
    monkeypatch.setattr(runtime_status, "STOP", threading.Event())
    idle = runtime_status.IdleWait()
    wake.touch()
    assert idle.wait(3600) is False
    assert idle.observed == wake.stat().st_mtime_ns
    runtime_status.STOP.set()
    assert idle.wait(3600) is True
