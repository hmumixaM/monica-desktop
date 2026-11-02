import sqlite3

import pytest

from app.import_messages import message_names


def test_new_empty_message_shard_waits_for_schema(tmp_path):
    with sqlite3.connect(tmp_path / 'message_9.db') as source:
        assert message_names(source) is None
        source.execute('CREATE TABLE Name2Id(user_name TEXT)')
        source.execute("INSERT INTO Name2Id VALUES('wxid_test')")
        assert message_names(source) == {1: 'wxid_test'}


def test_nonempty_message_shard_without_sender_mapping_is_an_error(tmp_path):
    with sqlite3.connect(tmp_path / 'message_9.db') as source:
        source.execute('CREATE TABLE Msg_abc(local_id INTEGER)')
        with pytest.raises(sqlite3.OperationalError, match='missing Name2Id'):
            message_names(source)
