"""Stream read-only projection metadata to the local, byte-matched source audit."""

import argparse
import csv
import json
import os
import shlex
import sqlite3
import subprocess
import time
from pathlib import Path

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--database", type=Path, required=True)
parser.add_argument("--host", default="nas")
parser.add_argument("--container", default="infra-postgres-1")
parser.add_argument("--db-user", default="crm")
parser.add_argument("--db-name", default="crm")
args = parser.parse_args()
if not args.database.is_file():
    raise SystemExit("Build the source audit first")
db = sqlite3.connect(args.database)
db.execute("CREATE TABLE IF NOT EXISTS audit_state(key TEXT PRIMARY KEY,value TEXT)")
db.execute("INSERT OR REPLACE INTO audit_state VALUES('projection_complete','false')")
db.commit()
queries = {
    "actual_events": (
        "id TEXT,at TEXT,room_id TEXT,record TEXT,content_id TEXT,type TEXT,sender_profile TEXT,sender_account TEXT,PRIMARY KEY(id,at)",
        "SELECT id,"
        + "to_char(occurred_at AT TIME ZONE 'UTC','YYYY-MM-DD\"T\"HH24:MI:SS')||CASE WHEN extract(microseconds FROM occurred_at)::int%1000000=0 THEN '' ELSE '.'||to_char(occurred_at AT TIME ZONE 'UTC','US') END||'+00:00'"
        + ",conversation_id,source_record_id,content_object_id,message_type,sender_profile_id,sender_account_id FROM message_events",
    ),
    "actual_content": (
        "id TEXT PRIMARY KEY,hash TEXT",
        "SELECT id,sha256 FROM content_objects WHERE content_kind='message'",
    ),
    "actual_search": (
        "id TEXT,at TEXT,room_id TEXT,hash TEXT,PRIMARY KEY(id,at)",
        "SELECT message_id,"
        + "to_char(occurred_at AT TIME ZONE 'UTC','YYYY-MM-DD\"T\"HH24:MI:SS')||CASE WHEN extract(microseconds FROM occurred_at)::int%1000000=0 THEN '' ELSE '.'||to_char(occurred_at AT TIME ZONE 'UTC','US') END||'+00:00'"
        + ",conversation_id,encode(digest(body,'sha256'),'hex') FROM message_search",
    ),
    "actual_rooms": (
        "id TEXT PRIMARY KEY,external TEXT,profile TEXT,account TEXT",
        "SELECT id,external_id,profile_id,source_account_id FROM conversations",
    ),
    "actual_senders": (
        "external TEXT,profile TEXT",
        "SELECT external_id,profile_id FROM identities WHERE provider IN ('wechat','wechat_group') AND profile_id IS NOT NULL AND status NOT IN ('invalid','merged')",
    ),
    "actual_accounts": (
        "id TEXT PRIMARY KEY,external TEXT,profile TEXT",
        "SELECT a.id,a.external_id,l.profile_id FROM source_accounts a LEFT JOIN source_account_links l ON l.account_id=a.id WHERE a.provider='wechat'",
    ),
}
for name, (schema, sql) in queries.items():
    db.execute("DROP TABLE IF EXISTS " + name)
    db.execute("CREATE TABLE " + name + "(" + schema + ")")
    db.commit()
    command = [
        "sudo",
        "-S",
        "-p",
        "",
        "/var/packages/ContainerManager/target/usr/bin/docker",
        "exec",
        "-e",
        "PGOPTIONS=-cdefault_transaction_read_only=on",
        args.container,
        "psql",
        "-Xq",
        "-U",
        args.db_user,
        "-d",
        args.db_name,
        "-c",
        "COPY (" + sql + ") TO STDOUT WITH (FORMAT CSV)",
    ]
    p = subprocess.Popen(
        ["ssh", "-o", "BatchMode=yes", args.host, shlex.join(command)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    p.stdin.write(os.environ["NAS_PASSWORD"] + "\n")
    p.stdin.close()
    batch = []
    n = 0
    start = time.monotonic()
    for row in csv.reader(p.stdout):
        batch.append(tuple(None if value == "" else value for value in row))
        n += 1
        if len(batch) == 5000:
            db.executemany(
                "INSERT INTO " + name + " VALUES(" + ",".join("?" for _ in row) + ")",
                batch,
            )
            db.commit()
            batch.clear()
            if n % 100000 == 0:
                print(
                    json.dumps(
                        {
                            "table": name,
                            "rows": n,
                            "seconds": round(time.monotonic() - start, 1),
                        }
                    ),
                    flush=True,
                )
    if batch:
        db.executemany(
            "INSERT INTO " + name + " VALUES(" + ",".join("?" for _ in batch[0]) + ")",
            batch,
        )
    db.commit()
    err = p.stderr.read()
    code = p.wait()
    if code:
        raise RuntimeError("Metadata stream failed: " + name + " " + err)
    print(json.dumps({"table": name, "rows": n, "complete": True}), flush=True)
db.execute(
    "CREATE INDEX IF NOT EXISTS actual_events_source ON actual_events(room_id,record,at)"
)
db.execute("CREATE INDEX IF NOT EXISTS actual_rooms_external ON actual_rooms(external)")
db.execute(
    "CREATE INDEX IF NOT EXISTS actual_senders_external ON actual_senders(external)"
)
db.commit()
db.execute("INSERT OR REPLACE INTO audit_state VALUES('projection_complete','true')")
db.commit()
print(
    "Projection metadata captured; compare against expected snapshot after checking import watermark.",
    flush=True,
)
