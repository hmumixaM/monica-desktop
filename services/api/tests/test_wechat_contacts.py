import sqlite3
from hashlib import md5
from pathlib import Path

from app.wechat_contacts import cached_media, contact_content, read_friends


def varint(value: int) -> bytes:
    output = bytearray()
    while value >= 0x80:
        output.append((value & 0x7F) | 0x80)
        value >>= 7
    output.append(value)
    return bytes(output)


def bytes_field(number: int, value: str | bytes) -> bytes:
    payload = value.encode() if isinstance(value, str) else value
    return varint(number << 3 | 2) + varint(len(payload)) + payload


def int_field(number: int, value: int) -> bytes:
    return varint(number << 3) + varint(value)


def contact_database(path: Path) -> None:
    with sqlite3.connect(path) as database:
        database.execute(
            """
            CREATE TABLE contact (
                username TEXT, local_type INTEGER, delete_flag INTEGER,
                alias TEXT, remark TEXT, nick_name TEXT, big_head_url TEXT,
                small_head_url TEXT, description TEXT, extra_buffer BLOB
            )
            """
        )
        database.execute("CREATE TABLE contact_label (label_id_ INTEGER, label_name_ TEXT)")
        database.executemany(
            "INSERT INTO contact_label VALUES (?,?)",
            [(1, "芝加哥"), (2, "大学")],
        )
        profile = b"".join(
            (
                bytes_field(4, "个人签名"),
                int_field(8, 14),
                bytes_field(27, bytes_field(2, "http://example.test/cover.jpg")),
                bytes_field(30, "1,2"),
            )
        )
        rows = [
            (
                "wxid_friend",
                1,
                0,
                "wechat_alias",
                "好友备注",
                "好友昵称",
                "http://example.test/big.jpg",
                "http://example.test/small.jpg",
                "联系人说明",
                profile,
            ),
            ("wxid_stranger", 3, 0, "", "", "陌生人", "", "", "", None),
            ("room@chatroom", 0, 0, "", "", "群", "", "", "", None),
            ("gh_official", 1, 0, "", "", "公众号", "", "", "", None),
            ("person@openim", 1, 0, "", "", "企业微信", "", "", "", None),
            ("filehelper", 1, 0, "", "", "系统", "", "", "", None),
            ("wxid_deleted", 1, 1, "", "", "已删除", "", "", "", None),
        ]
        database.executemany(
            "INSERT INTO contact VALUES (?,?,?,?,?,?,?,?,?,?)",
            rows,
        )


def test_read_friends_filters_nonfriends_and_decodes_profile(tmp_path: Path) -> None:
    path = tmp_path / "contact.db"
    contact_database(path)

    friends, excluded = read_friends(path)

    assert len(friends) == 1
    friend = friends[0]
    assert friend["username"] == "wxid_friend"
    assert friend["signature"] == "个人签名"
    assert friend["add_source_label"] == "群聊"
    assert friend["moments_cover_url"] == "http://example.test/cover.jpg"
    assert friend["labels"] == ["芝加哥", "大学"]
    assert excluded == {
        "stranger": 1,
        "chatroom": 1,
        "official_account": 1,
        "openim": 1,
        "system": 1,
        "deleted": 1,
    }


def test_contact_content_preserves_exact_source_fields(tmp_path: Path) -> None:
    path = tmp_path / "contact.db"
    contact_database(path)
    friend = read_friends(path)[0][0]

    content = contact_content(friend)

    assert content["display_name"] == "好友备注"
    assert content["summary"] == "个人签名"
    assert content["username"] == "wxid_friend"
    assert content["fields"] == {
        "wechat.alias": "wechat_alias",
        "wechat.remark": "好友备注",
        "wechat.nickname": "好友昵称",
        "wechat.signature": "个人签名",
        "wechat.add_source": "群聊",
        "wechat.labels": ["芝加哥", "大学"],
        "wechat.description": "联系人说明",
        "wechat.avatar_url": "http://example.test/big.jpg",
        "wechat.moments_cover_url": "http://example.test/cover.jpg",
    }


def test_cached_media_reuses_original_and_legacy_https_keys(tmp_path: Path) -> None:
    url = "http://example.test/avatar.jpg"
    legacy_url = url.replace("http://", "https://", 1)
    legacy = tmp_path / f"{md5(f'{legacy_url}|'.encode()).hexdigest()}.bin"
    legacy.write_bytes(b"image")

    assert cached_media(tmp_path, url) == legacy
