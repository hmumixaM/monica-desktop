import argparse
import base64
import hashlib
import json
import re
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "services/api"))
from app.content import canonical_json
from app.import_messages import decode, search_text

parser = argparse.ArgumentParser(
    description="Build an exact, local audit of a frozen Chatlog message snapshot; never writes to the source files."
)
parser.add_argument("--root", type=Path, required=True)
parser.add_argument("--database", type=Path, required=True)
parser.add_argument("--report", type=Path, required=True)
args = parser.parse_args()
root = args.root
target = args.database
if target.exists():
    raise SystemExit("Refusing to overwrite an existing message audit")
audit = sqlite3.connect(target)
audit.execute("PRAGMA journal_mode=WAL")
audit.execute("PRAGMA synchronous=NORMAL")
audit.execute(
    "CREATE TABLE expected(room TEXT,record TEXT,at TEXT,hash TEXT,search_hash TEXT,type TEXT,sender TEXT,source_file TEXT,source_table TEXT,local_id INTEGER,PRIMARY KEY(room,record,at)) WITHOUT ROWID"
)
report = {
    "conflicting_duplicate_rows": 0,
    "source_rows": 0,
    "valid_rows": 0,
    "invalid_dates": 0,
    "unmapped_rows": 0,
    "binary_bodies": 0,
    "files": [],
}


def write_batch(batch):
    inserted = audit.executemany(
        "INSERT OR IGNORE INTO expected VALUES(?,?,?,?,?,?,?,?,?,?)", batch
    ).rowcount
    if inserted < len(batch):
        for row in batch:
            stored = audit.execute(
                "SELECT hash,search_hash,type,sender FROM expected WHERE room=? AND record=? AND at=?",
                row[:3],
            ).fetchone()
            if stored != row[3:7]:
                report["conflicting_duplicate_rows"] += 1


for path in sorted(root.glob("message_*.db")):
    source_stamp = (path.stat().st_size, path.stat().st_mtime_ns)
    counts = {"file": path.name, "rows": 0, "tables": 0}
    with sqlite3.connect("file:" + str(path) + "?mode=ro", uri=True) as source:
        names = {
            r[0]: r[1]
            for r in source.execute("SELECT rowid,user_name FROM Name2Id")
            if r[1]
        }
        talkers = {hashlib.md5(n.encode()).hexdigest(): n for n in names.values()}
        for (table,) in source.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'Msg_%'"
        ):
            if not re.fullmatch("Msg_[0-9a-fA-F]{32}", table):
                continue
            counts["tables"] += 1
            talker = talkers.get(table[4:])
            batch = []
            for (
                local_id,
                server_id,
                epoch,
                typ,
                sender,
                body,
                compressed,
            ) in source.execute(
                f"SELECT local_id,server_id,create_time,local_type,real_sender_id,message_content,compress_content FROM {table} ORDER BY local_id"
            ):
                counts["rows"] += 1
                report["source_rows"] += 1
                if not talker:
                    report["unmapped_rows"] += 1
                    continue
                try:
                    at = datetime.fromtimestamp(epoch, UTC)
                except (ValueError, TypeError, OSError):
                    report["invalid_dates"] += 1
                    continue
                if not 1990 <= at.year <= 2100:
                    report["invalid_dates"] += 1
                    continue
                value = decode(body) or decode(compressed)
                content = {
                    "text": value or "",
                    "sender_external_id": names.get(sender),
                    "message_type": typ,
                }
                if value is None:
                    report["binary_bodies"] += 1
                    if isinstance(body, bytes):
                        content["binary_base64"] = base64.b64encode(body).decode()
                stamp = at.isoformat()
                record = (
                    f"server:{server_id}"
                    if server_id
                    else f"{path.name}:{table}:{local_id}"
                )
                room = (
                    "group:" if talker.endswith("@chatroom") else "direct:"
                ) + talker
                batch.append(
                    (
                        room,
                        record,
                        stamp,
                        hashlib.sha256(canonical_json(content)).hexdigest(),
                        hashlib.sha256(search_text(value).encode()).hexdigest(),
                        str(typ),
                        names.get(sender),
                        path.name,
                        table,
                        local_id,
                    )
                )
                report["valid_rows"] += 1
                if len(batch) >= 2000:
                    write_batch(batch)
                    batch.clear()
            if batch:
                write_batch(batch)
        audit.commit()
    if (path.stat().st_size, path.stat().st_mtime_ns) != source_stamp:
        raise RuntimeError("Source snapshot changed during audit: " + path.name)
    report["files"].append(counts)
    print(json.dumps(counts), flush=True)
report["unique_messages"] = audit.execute("SELECT count(*) FROM expected").fetchone()[0]
report["duplicate_source_rows"] = report["valid_rows"] - report["unique_messages"]
args.report.write_text(json.dumps(report, indent=2))
print(json.dumps(report), flush=True)
audit.close()
