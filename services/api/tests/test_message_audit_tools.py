import hashlib
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

TOOLS = Path(__file__).resolve().parents[3] / 'infra' / 'audits'


def test_source_audit_classifies_duplicates_invalid_dates_and_search_text(tmp_path):
    source = tmp_path / 'source'
    source.mkdir()
    table = 'Msg_' + hashlib.md5(b'wx_audit').hexdigest()
    for number, rows in enumerate([
        [(1, 11, 1767225600, '你好'), (2, 22, 1767225601, '<msg><title>上海</title></msg>'), (3, 33, 0, 'invalid')],
        [(1, 11, 1767225600, '你好'), (2, 22, 1767225601, 'conflicting duplicate'), (3, 0, 1767225602, 'fallback ID')],
    ]):
        with sqlite3.connect(source / f'message_{number}.db') as db:
            db.execute('CREATE TABLE Name2Id(user_name TEXT)')
            db.execute("INSERT INTO Name2Id VALUES('wx_audit')")
            db.execute(f'CREATE TABLE {table}(local_id INTEGER,server_id INTEGER,create_time INTEGER,local_type INTEGER,real_sender_id INTEGER,message_content TEXT,compress_content TEXT)')
            db.executemany(f'INSERT INTO {table} VALUES(?,?,?,1,1,?,NULL)', rows)
    database, report = tmp_path / 'audit.sqlite3', tmp_path / 'report.json'
    command = [sys.executable, str(TOOLS / 'message-source.py'), '--root', str(source), '--database', str(database), '--report', str(report)]
    subprocess.run(command, check=True, capture_output=True, text=True)
    counts = json.loads(report.read_text())
    assert (counts['source_rows'], counts['valid_rows'], counts['unique_messages']) == (6, 5, 3)
    assert (counts['duplicate_source_rows'], counts['conflicting_duplicate_rows'], counts['invalid_dates']) == (2, 1, 1)
    with sqlite3.connect(database) as db:
        assert db.execute("SELECT search_hash FROM expected WHERE record='server:22'").fetchone()[0] == hashlib.sha256('上海'.encode()).hexdigest()
        assert db.execute("SELECT count(*) FROM expected WHERE record LIKE 'message_1.db:%:3'").fetchone()[0] == 1
    rerun = subprocess.run(command, capture_output=True, text=True, check=False)
    assert rerun.returncode != 0
    assert 'Refusing to overwrite' in rerun.stderr
