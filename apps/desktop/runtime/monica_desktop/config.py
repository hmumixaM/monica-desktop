"""Versioned local settings. Secret values never cross the UI status boundary."""

import copy
import json
import os
import secrets
import tempfile
from pathlib import Path
from urllib.parse import urlparse


def write_private(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix=".write-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as output:
            os.fchmod(output.fileno(), 0o600)
            json.dump(value, output, ensure_ascii=False, indent=2)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def defaults() -> dict:
    return {
        "version": 1,
        "web_port": 17394,
        "autostart": False,
        "timezone": "America/Chicago",
        "wechat": {"data_dir": "", "config_file": "", "data_key": "", "img_key": "", "connected": False},
        "ai": {"enabled": False, "base_url": "", "api_key": "", "batch_model": "", "vision_model": ""},
        "sources": {"instagram": {"enabled": False, "username": ""}, "linkedin": {"enabled": False}, "faces": {"enabled": False}},
        "secrets": {"database": secrets.token_urlsafe(32), "session": secrets.token_urlsafe(48),
                    "import": secrets.token_urlsafe(48), "faces_token": secrets.token_urlsafe(32),
                    "linkedin": secrets.token_urlsafe(32)},
    }


class Config:
    def __init__(self, root: Path):
        self.path = root / "settings.json"
        self.value = defaults()
        if self.path.exists():
            saved = json.loads(self.path.read_text())
            if saved.get("version") != 1:
                raise ValueError("不支持此设置版本；请使用更新的 Monica 应用")
            self._merge(self.value, saved)
        else:
            self.save()
        os.chmod(self.path, 0o600)

    @staticmethod
    def _merge(target, incoming):
        for key, value in incoming.items():
            if key not in target:
                continue
            if isinstance(target[key], dict) and isinstance(value, dict):
                Config._merge(target[key], value)
            else:
                target[key] = value

    def update(self, incoming: dict) -> None:
        if not isinstance(incoming, dict):
            raise ValueError("设置必须是对象")
        permitted = {key: value for key, value in incoming.items()
                     if key in {"web_port", "autostart", "timezone", "wechat", "ai", "sources"}}
        candidate = copy.deepcopy(self.value)
        for section, keys in (("ai", ("api_key",)), ("wechat", ("data_key", "img_key"))):
            if isinstance(permitted.get(section), dict):
                permitted[section] = {k: v for k, v in permitted[section].items()
                                      if k not in keys or v}
                # Only a successful connection test can mark WeChat connected.
                permitted[section].pop("connected", None)
        self._merge(candidate, permitted)
        if not isinstance(candidate["web_port"], int) or not 1024 <= candidate["web_port"] <= 65535:
            raise ValueError("本地端口必须在 1024 到 65535 之间")
        from zoneinfo import ZoneInfo
        ZoneInfo(candidate["timezone"])
        ai = candidate["ai"]
        if ai["base_url"]:
            url = urlparse(ai["base_url"])
            if url.scheme not in {"https", "http"} or not url.hostname or url.username or url.password:
                raise ValueError("模型地址需要有效的 HTTP(S) URL，凭据请填入 API Key")
        if ai["enabled"] and not all(ai[key] for key in ("base_url", "api_key", "batch_model", "vision_model")):
            raise ValueError("请先填写模型地址、API Key、文本模型和图片模型")
        if candidate["wechat"]["data_dir"] != self.value["wechat"]["data_dir"]:
            candidate["wechat"]["connected"] = False
        self.value = candidate
        self.save()

    def save(self):
        write_private(self.path, self.value)

    def public(self):
        value = copy.deepcopy(self.value)
        value.pop("secrets")
        for section, keys in (("ai", ("api_key",)), ("wechat", ("data_key", "img_key"))):
            for key in keys:
                value[section][key + "_set"] = bool(value[section].pop(key, ""))
        return value


def discover_wechat() -> list[str]:
    base = Path.home() / "Library/Containers/com.tencent.xinWeChat/Data/Documents/xwechat_files"
    try:
        return [str(path) for path in sorted(base.iterdir()) if (path / "db_storage").is_dir()]
    except (FileNotFoundError, PermissionError):
        return []
