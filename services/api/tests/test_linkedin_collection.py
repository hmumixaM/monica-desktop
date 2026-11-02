from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from app import linkedin_collection as collection
from app.account_links import set_link
from app.config import get_settings
from app.imports import project_batch
from app.schemas import ImportBatchRequest


@pytest.mark.parametrize(
    "value,slug",
    [
        (" Jane-Doe ", "jane-doe"),
        ("https://www.linkedin.com/in/Jane-Doe/?trk=test#about", "jane-doe"),
        ("linkedin.com/in/jane-doe", "jane-doe"),
        ("https://uk.linkedin.com/in/jane-doe", "jane-doe"),
        ("https://www.linkedin.com/in/%E5%BC%A0%E4%B8%89", "张三"),
    ],
)
def test_canonical_account(value, slug):
    identifier, url = collection.canonical_account(value)
    assert identifier == slug
    assert collection.canonical_account(url) == (identifier, url)


@pytest.mark.parametrize(
    "value",
    [
        "",
        "a b",
        "..",
        "https://evil.com/in/jane",
        "https://linkedin.com.evil.com/in/jane",
        "https://evil@linkedin.com/in/jane",
        "https://www.linkedin.com/company/acme",
        "https://www.linkedin.com/in/jane/posts",
        "https://www.linkedin.com/in/a%2Fb",
        "https://www.linkedin.com:bad/in/jane",
        "file://linkedin.com/in/jane",
    ],
)
def test_rejects_non_profile_accounts(value):
    with pytest.raises(HTTPException) as error:
        collection.canonical_account(value)
    assert error.value.status_code == 422


@pytest.fixture
def source(crm_db, monkeypatch):
    url = make_url(__import__("os").environ["TEST_DATABASE_URL"])
    name = "linkedin_target_" + uuid4().hex
    admin = create_engine(url.set(database="postgres"), isolation_level="AUTOCOMMIT")
    with admin.connect() as connection:
        connection.exec_driver_sql(f'CREATE DATABASE "{name}"')
    engine = create_engine(url.set(database=name))
    with engine.begin() as connection:
        connection.execute(
            text((Path(__file__).parents[1] / "app/platform_schema.sql").read_text())
        )
    monkeypatch.setattr(collection, "platform_engine", lambda provider: engine)
    monkeypatch.setattr("app.source_projection.platform_engine", lambda provider: engine)
    crm_db.execute(
        text("INSERT INTO platform_migration_state(provider,enabled) VALUES('linkedin',true)")
    )
    crm_db.commit()
    yield engine
    engine.dispose()
    with admin.connect() as connection:
        connection.exec_driver_sql(f'DROP DATABASE "{name}" WITH (FORCE)')
    admin.dispose()


def person(db, kind="person"):
    return db.execute(
        text(
            "INSERT INTO profiles(profile_type,display_name) VALUES(:kind,'Manual person') RETURNING id"
        ),
        {"kind": kind},
    ).scalar_one()


def save(db, pid, value="jane-doe"):
    return collection.save_target(
        pid, collection.LinkedInTargetSave(account=value), db, {"id": None}
    )


def test_new_nonconnection_imports_into_confirmed_profile_and_retains_history(crm_db, source):
    pid = person(crm_db)
    result = save(crm_db, pid)
    aid = result["account_id"]
    assert save(crm_db, pid, "https://uk.linkedin.com/in/Jane-Doe/?x=1")["account_id"] == aid
    assert len(collection.profile_targets(pid, crm_db, {})) == 1
    assert collection.collection_targets(crm_db)["targets"] == [
        {
            "profile_url": "https://www.linkedin.com/in/jane-doe",
            "external_id": "jane-doe",
            "metadata": {"provider_user_id": "jane-doe", "crm_manual": True},
        }
    ]
    # A regular collector profile batch uses the pre-confirmed account; it does
    # not need a Monica contact_id or LinkedIn connection field.
    batch = ImportBatchRequest(
        source="linkedin",
        stream="profiles",
        idempotency_key="manual-first",
        batch_id="manual-first",
        observed_at=datetime.now(UTC),
        records=[
            {
                "external_id": "jane-doe",
                "content": {"fields": {"linkedin.headline": "Engineer"}},
            }
        ],
    )
    project_batch(crm_db, batch)
    crm_db.commit()
    assert (
        crm_db.execute(
            text("SELECT profile_id FROM identities WHERE source_account_id=:id"), {"id": aid}
        ).scalar_one()
        == pid
    )
    assert (
        crm_db.execute(
            text(
                "SELECT current_source_type FROM profile_field_current WHERE profile_id=:id AND field_key='linkedin.headline'"
            ),
            {"id": pid},
        ).scalar_one()
        == "linkedin"
    )
    with source.connect() as connection:
        assert connection.execute(text("SELECT count(*) FROM accounts")).scalar_one() == 1
        assert connection.execute(text("SELECT count(*) FROM observations")).scalar_one() > 0
    collection.remove_target(pid, aid, crm_db, {"id": None})
    assert collection.collection_targets(crm_db)["targets"] == []
    assert (
        crm_db.execute(
            text("SELECT profile_id FROM source_account_links WHERE account_id=:id"), {"id": aid}
        ).scalar_one()
        == pid
    )
    assert (
        crm_db.execute(
            text("SELECT count(*) FROM profile_field_revisions WHERE source_account_id=:id"),
            {"id": aid},
        ).scalar_one()
        > 0
    )


def test_refuses_moving_an_owned_account_and_groups(crm_db, source):
    owner, other, group = person(crm_db), person(crm_db), person(crm_db, "group")
    aid = save(crm_db, owner)["account_id"]
    with pytest.raises(HTTPException) as error:
        save(crm_db, other)
    assert error.value.status_code == 409
    crm_db.rollback()
    with pytest.raises(HTTPException) as error:
        save(crm_db, group)
    assert error.value.status_code == 422
    crm_db.rollback()
    with pytest.raises(HTTPException) as error:
        collection.remove_target(other, aid, crm_db, {"id": None})
    assert error.value.status_code == 404


def test_unlinked_archived_and_quarantined_accounts_leave_the_feed(crm_db, source):
    pid = person(crm_db)
    aid = save(crm_db, pid)["account_id"]
    set_link(crm_db, aid, None, expected_profile_id=pid)
    assert collection.collection_targets(crm_db)["targets"] == []
    set_link(crm_db, aid, pid)
    assert len(collection.collection_targets(crm_db)["targets"]) == 1
    crm_db.execute(text("UPDATE profiles SET archived_at=now() WHERE id=:id"), {"id": pid})
    assert collection.collection_targets(crm_db)["targets"] == []
    crm_db.execute(text("UPDATE profiles SET archived_at=NULL WHERE id=:id"), {"id": pid})
    crm_db.execute(
        text("UPDATE identities SET status='invalid' WHERE source_account_id=:id"), {"id": aid}
    )
    assert collection.collection_targets(crm_db)["targets"] == []


def test_collector_feed_requires_its_dedicated_token(crm_db, monkeypatch):
    for configured in ("", "only-for-targets"):
        monkeypatch.setenv("LINKEDIN_COLLECTION_TOKEN", configured)
        get_settings.cache_clear()
        for supplied in (None, "wrong"):
            with pytest.raises(HTTPException) as error:
                collection.collector_targets(crm_db, supplied)
            assert error.value.status_code == 401
    assert collection.collector_targets(crm_db, "only-for-targets") == {"version": 1, "targets": []}
