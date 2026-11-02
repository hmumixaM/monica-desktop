"""Exercise the collector's source-list reconciliation without browser dependencies."""

import importlib.util
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace


def test_target_reconciliation_preserves_other_sources_and_removes_stale_membership():
    path = Path(__file__).parents[3] / "infra/linkedin/target_sources.py"
    spec = importlib.util.spec_from_file_location("target_sources", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    with sqlite3.connect(":memory:") as connection:
        connection.row_factory = sqlite3.Row
        connection.execute(
            "CREATE TABLE targets(id INTEGER PRIMARY KEY,profile_url TEXT,metadata_json TEXT,active INTEGER,updated_at TEXT)"
        )
        for index, metadata in enumerate(
            (
                {"crm_manual": True},
                {"linkedin_connection": True, "crm_manual": True},
                {"contact_id": 1, "crm_manual": True},
                {"linkedin_connection": True},
            )
        ):
            connection.execute(
                "INSERT INTO targets VALUES(?,?,?,1,'before')",
                (index, str(index), json.dumps(metadata)),
            )
        module.reconcile_source(
            connection, [SimpleNamespace(url="3")], "linkedin_connection", "now"
        )
        module.reconcile_source(connection, [], "crm_manual", "now")
        result = list(connection.execute("SELECT active,metadata_json FROM targets ORDER BY id"))
        assert [row["active"] for row in result] == [0, 0, 1, 1]
        assert [json.loads(row["metadata_json"]) for row in result] == [
            {},
            {},
            {"contact_id": 1},
            {"linkedin_connection": True},
        ]
