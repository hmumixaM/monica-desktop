"""Install the CRM target hook while the collector is idle; never change its schedule or budgets."""
import argparse
import shutil
from pathlib import Path

HOOK = '''# CRM manual targets are reconciled after the existing source list.
if [[ -f "$ROOT/data/crm-targets.json" ]]; then
  if ! "$UV" run --no-sync python -m linkedin_enrichment.crm_targets --db "$DB" --config "$ROOT/data/crm-targets.json"; then
    echo 'CRM target refresh failed; retaining the previous manual queue.' >&2
  fi
fi
'''

OLD_RECONCILE = '''                managed_ids = [
                    row["id"]
                    for row in connection.execute(
                        "SELECT id, metadata_json FROM targets WHERE active=1"
                    )
                    if reconcile_metadata_key
                    in json.loads(row["metadata_json"])
                ]
                if managed_ids:
                    placeholders = ",".join("?" for _ in managed_ids)
                    connection.execute(
                        f"""UPDATE targets SET active=0, updated_at=?
                            WHERE id IN ({placeholders})""",
                        (timestamp, *managed_ids),
                    )'''
NEW_RECONCILE = '''                from .target_sources import reconcile_source
                reconcile_source(connection, targets, reconcile_metadata_key, timestamp)'''


def install(root):
    storage = root / "src/linkedin_enrichment/storage.py"
    storage_content = storage.read_text()
    if NEW_RECONCILE not in storage_content and storage_content.count(OLD_RECONCILE) != 1:
        raise ValueError("Collector layout changed: storage.py")
    paths = [(root / "scripts/sync-host.sh", "pilot_pending=1"),
             (root / "scripts/scan-connections-host.sh", '"$UV" run --no-sync linkedin-enrichment --db "$DB" status')]
    # Check both anchors before mutating either script.
    for path, anchor in paths:
        content = path.read_text()
        if HOOK not in content and content.count(anchor) != 1:
            raise ValueError(f"Collector layout changed: {path.name}")
    for path, anchor in paths:
        content = path.read_text()
        if HOOK not in content:
            path.with_suffix(path.suffix + ".before-manual-targets").write_text(content)
            path.write_text(content.replace(anchor, HOOK + "\n" + anchor, 1))
    if NEW_RECONCILE not in storage_content:
        storage.with_suffix(".py.before-manual-targets").write_text(storage_content)
        storage.write_text(storage_content.replace(OLD_RECONCILE, NEW_RECONCILE, 1))
    for name in ("crm_targets.py", "target_sources.py"):
        shutil.copyfile(Path(__file__).with_name(name), root / "src/linkedin_enrichment" / name)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("root", type=Path)
    install(parser.parse_args().root)
