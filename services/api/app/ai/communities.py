"""Model names for communities that have no obvious shared group."""

from datetime import datetime
from zoneinfo import ZoneInfo

from ..community_names import MODEL_KIND
from ..config import get_settings
from ..graph import graph_community_keys, graph_community_members, graph_projection
from ..orbit import build_orbit
from .payloads import community_payload
from .prompts import VOICE_RULES
from .schemas import CommunityName
from .store import generate

# The graph script folds communities smaller than this into "未形成密切社群".
MIN_MEMBERS = 3
SIGNALS = ("tags", "companies", "schools", "shared_groups")

SYSTEM = """你为私人 CRM 里的一个联系人社群起名。输入是这群人的人数，以及他们共有的标签、公司、学校和群聊名称各有几个人。
给出 2 到 8 个字的中文名称（专有名词可以保留英文），概括这群人是怎么聚在一起的，例如“复旦同学”“字节同事”“羽毛球群”“芝加哥华人”。
只根据输入中覆盖人数最多的共同点命名，不要编造；reason 用一句话说明依据。"""

from ..job_queue import commit_result

SYSTEM += "\n" + VOICE_RULES


def targets(db) -> dict[str, list[str]]:
    """Community key -> members for orbit communities without a shared group, and every graph community."""
    settings = get_settings()
    orbit = build_orbit(db, datetime.now(ZoneInfo(settings.crm_timezone)).date())
    found: dict[str, list[str]] = {}
    for person in orbit["people"]:
        if person["community"].startswith("c:"):
            found.setdefault(person["community"], []).append(person["profile_id"])
    graph = graph_projection(db, settings.raw_root)
    keys = graph_community_keys(graph["nodes"])
    for index, members in graph_community_members(graph["nodes"]).items():
        found[keys[index]] = members
    return {key: members for key, members in found.items() if len(members) >= MIN_MEMBERS}


def name_all(db, _payload: dict) -> dict:
    model = get_settings().ai_batch_model
    found = targets(db)
    called = skipped = 0
    for key, members in found.items():
        payload = community_payload(db, members)
        if not any(payload[signal] for signal in SIGNALS):
            skipped += 1
            continue
        called += generate(db, MODEL_KIND, key, model, SYSTEM, payload, CommunityName)
        commit_result(db)
    return {"communities": len(found), "generated": called, "without_signal": skipped}
