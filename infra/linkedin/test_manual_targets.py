"""Run with the installed collector, alongside its complete test suite."""

import importlib.util
import json
from pathlib import Path

import pytest
from linkedin_enrichment.crm_targets import NoRedirect, apply_targets, fetch_targets
from linkedin_enrichment.models import Target
from linkedin_enrichment.storage import Store


def feed(*slugs):
    return {
        "version": 1,
        "targets": [
            {
                "profile_url": f"https://www.linkedin.com/in/{slug}",
                "external_id": slug,
                "metadata": {"provider_user_id": slug, "crm_manual": True},
            }
            for slug in slugs
        ],
    }


def rows(store):
    with store.connect() as connection:
        return [
            dict(row) for row in connection.execute("SELECT * FROM targets ORDER BY id")
        ]


def test_manual_nonconnection_is_queued_once_and_removed_without_erasing_history(
    tmp_path,
):
    store = Store(tmp_path / "collector.sqlite3")
    store.initialize()
    assert apply_targets(store, feed("jane")) == 1
    assert apply_targets(store, feed("jane")) == 1
    assert len(rows(store)) == 1
    assert rows(store)[0]["active"] == 1
    apply_targets(store, feed())
    assert rows(store)[0]["active"] == 0
    with store.connect() as connection:
        assert connection.execute("SELECT count(*) FROM jobs").fetchone()[0] == 1


def test_overlapping_sources_survive_either_list_removing_the_account(tmp_path):
    store = Store(tmp_path / "collector.sqlite3")
    store.initialize()
    connection = Target(
        profile_url="https://www.linkedin.com/in/jane/",
        external_id="jane",
        metadata={"linkedin_connection": True, "provider_user_id": "jane"},
    )
    store.enqueue([connection], reconcile_metadata_key="linkedin_connection")
    apply_targets(store, feed("jane"))
    assert len(rows(store)) == 1
    store.enqueue([], reconcile_metadata_key="linkedin_connection")
    assert rows(store)[0]["active"] == 1
    assert "linkedin_connection" not in json.loads(rows(store)[0]["metadata_json"])
    apply_targets(store, feed())
    assert rows(store)[0]["active"] == 0
    store.enqueue([connection], reconcile_metadata_key="linkedin_connection")
    apply_targets(store, feed("jane"))
    apply_targets(store, feed())
    assert rows(store)[0]["active"] == 1
    assert "crm_manual" not in json.loads(rows(store)[0]["metadata_json"])


def test_invalid_feed_cannot_clear_queue_and_failed_download_cannot_reconcile(tmp_path):
    store = Store(tmp_path / "collector.sqlite3")
    store.initialize()
    apply_targets(store, feed("jane"))
    for document in (
        {},
        {"version": 2, "targets": []},
        {"version": 1, "targets": None},
        feed("jane/other"),
    ):
        with pytest.raises(ValueError):
            apply_targets(store, document)
        assert rows(store)[0]["active"] == 1
    with pytest.raises(ValueError, match="HTTPS"):
        fetch_targets("http://example.test", tmp_path / "missing-token")
    with pytest.raises(ValueError, match="redirect"):
        NoRedirect().redirect_request(
            None, None, 302, "Moved", {}, "https://elsewhere.test"
        )


def test_installer_is_repeatable_and_checks_anchors_before_changes(tmp_path):
    spec = importlib.util.spec_from_file_location(
        "manual_installer", Path(__file__).with_name("configure-manual-targets.py")
    )
    installer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(installer)
    (tmp_path / "src/linkedin_enrichment").mkdir(parents=True)
    (tmp_path / "scripts").mkdir()
    storage = tmp_path / "src/linkedin_enrichment/storage.py"
    sync = tmp_path / "scripts/sync-host.sh"
    scan = tmp_path / "scripts/scan-connections-host.sh"
    storage.write_text(installer.OLD_RECONCILE)
    sync.write_text("pilot_pending=1\n")
    scan.write_text("unexpected layout")
    with pytest.raises(ValueError, match="layout changed"):
        installer.install(tmp_path)
    assert storage.read_text() == installer.OLD_RECONCILE
    assert sync.read_text() == "pilot_pending=1\n"
    scan.write_text('"$UV" run --no-sync linkedin-enrichment --db "$DB" status')
    installer.install(tmp_path)
    before = sync.read_text(), storage.read_text()
    installer.install(tmp_path)
    assert before == (sync.read_text(), storage.read_text())
