import argparse
import json
import sqlite3
from pathlib import Path

parser = argparse.ArgumentParser(
    description="Compare every frozen source message with collected NAS projection metadata."
)
parser.add_argument("--database", type=Path, required=True)
parser.add_argument("--source-report", type=Path, required=True)
parser.add_argument("--report", type=Path, required=True)
args = parser.parse_args()
db = sqlite3.connect("file:" + str(args.database) + "?mode=ro", uri=True)
assert db.execute(
    "SELECT value FROM audit_state WHERE key='projection_complete'"
).fetchone() == ("true",), "Projection collection did not finish"
db.execute("PRAGMA temp_store=MEMORY")
db.execute("PRAGMA cache_size=-262144")
query = """FROM expected e LEFT JOIN actual_rooms c ON c.external=e.room
 LEFT JOIN actual_events m ON m.room_id=c.id AND m.record=e.record AND m.at=e.at
 LEFT JOIN actual_content o ON o.id=m.content_id
 LEFT JOIN actual_search s ON s.id=m.id AND s.at=m.at"""
report = dict(
    zip(
        (
            "expected",
            "missing",
            "body_hash_different",
            "search_missing",
            "search_hash_different",
            "search_room_different",
            "type_different",
        ),
        db.execute(
            """SELECT count(*),
 sum(m.id IS NULL),sum(m.id IS NOT NULL AND (o.hash IS NULL OR e.hash<>o.hash)),
 sum(m.id IS NOT NULL AND s.id IS NULL),sum(s.id IS NOT NULL AND s.hash<>e.search_hash),
 sum(s.id IS NOT NULL AND s.room_id<>m.room_id),sum(m.id IS NOT NULL AND m.type<>e.type) """
            + query
        ).fetchone(),
    )
)
report["projection_rows"] = db.execute("SELECT count(*) FROM actual_events").fetchone()[
    0
]
report["source_files"] = json.loads(args.source_report.read_text())["files"]
report["missing_by_file"] = [
    dict(zip(("file", "missing"), r))
    for r in db.execute(
        "SELECT e.source_file,count(*) "
        + query
        + " WHERE m.id IS NULL GROUP BY e.source_file"
    )
]
report["difference_examples"] = [
    dict(zip(("file", "table", "local_id", "body_hash", "search_hash"), r))
    for r in db.execute(
        "SELECT e.source_file,e.source_table,e.local_id,o.hash<>e.hash,s.hash<>e.search_hash "
        + query
        + " WHERE m.id IS NOT NULL AND (o.hash IS NULL OR e.hash<>o.hash OR s.id IS NULL OR s.hash<>e.search_hash OR s.room_id<>m.room_id OR m.type<>e.type) LIMIT 20"
    )
]
report["sender_projection_differences"] = (
    db.execute("""SELECT count(*) FROM expected e JOIN actual_rooms c ON c.external=e.room
 JOIN actual_events m ON m.room_id=c.id AND m.record=e.record AND m.at=e.at
 LEFT JOIN actual_accounts a ON a.id=m.sender_account
 WHERE (CASE WHEN m.sender_account IS NOT NULL THEN a.profile ELSE m.sender_profile END) IS NOT
 (SELECT profile FROM actual_senders i WHERE i.external=e.sender LIMIT 1)""").fetchone()[
        0
    ]
)
args.report.write_text(json.dumps(report, indent=2))
print(json.dumps(report), flush=True)
