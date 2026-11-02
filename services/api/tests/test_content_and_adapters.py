import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1]))
sys.path.insert(0, str(Path(__file__).parents[3]))

from importers.instagram.adapter import stories_batch
from importers.wechat.adapter import contacts_batch, profile_field_record

from app.content import canonical_json, content_hash


def test_canonical_json_hash_is_order_independent():
    assert canonical_json({"b": 1, "a": "x"}) == b'{"a":"x","b":1}'
    assert content_hash({"b": 1, "a": "x"}) == content_hash({"a": "x", "b": 1})


def test_importers_emit_stable_contract():
    field = profile_field_record("wxid-1", "profile-1", "wechat_signature", "hello")
    batch = stories_batch("batch-1", [field], cursor_before="a", cursor_after="b")
    assert batch["source"] == "instagram"
    assert batch["stream"] == "stories"
    assert batch["records"][0]["external_id"] == "wxid-1"
    assert batch["idempotency_key"] == "instagram:stories:batch-1"


def test_adapters_wrap_source_native_payloads_without_losing_content():
    batch = contacts_batch("contacts-1", [{"u": "wxid-raw", "nick": "Ada", "remark": "A"}])
    record = batch["records"][0]
    assert record["external_id"] == "wxid-raw"
    assert record["content"]["nick"] == "Ada"
    assert record["content"]["remark"] == "A"
