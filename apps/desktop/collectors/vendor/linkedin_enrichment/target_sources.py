"""Reconcile one target list without cancelling accounts retained by another."""

import json


def reconcile_source(connection, targets, key, timestamp):
    incoming = {target.url for target in targets}
    for row in connection.execute(
        "SELECT id,profile_url,metadata_json FROM targets"
    ).fetchall():
        metadata = json.loads(row["metadata_json"])
        if key not in metadata or row["profile_url"] in incoming:
            continue
        metadata.pop(key)
        retained = any(
            metadata.get(source) is not None and metadata.get(source) is not False
            for source in ("contact_id", "linkedin_connection", "crm_manual")
        )
        connection.execute(
            "UPDATE targets SET active=?,metadata_json=?,updated_at=? WHERE id=?",
            (int(retained), json.dumps(metadata), timestamp, row["id"]),
        )
