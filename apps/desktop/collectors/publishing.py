"""Local publication of complete collector artifacts, using the CRM snapshot protocol."""
import hashlib
import importlib.util
import json
import os
import re
import sqlite3
import tempfile
from pathlib import Path


def atomic_json(path, document):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix="." + path.name, dir=path.parent)
    try:
        with os.fdopen(fd, "w") as output:
            json.dump(document, output, ensure_ascii=False, sort_keys=True)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def notify_importers(data_root):
    path = data_root / "run/wake"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch()


def publisher():
    here = Path(__file__).resolve()
    candidates = [Path(os.environ["MONICA_SNAPSHOT_PUBLISHER"])] if os.environ.get("MONICA_SNAPSHOT_PUBLISHER") else [
        here.parents[1] / "infra/snapshot_publish.py", here.parents[3] / "infra/snapshot_publish.py"]
    path = next((item for item in candidates if item.is_file()), None)
    if path is None:
        raise FileNotFoundError("Bundled snapshot publisher is missing")
    spec = importlib.util.spec_from_file_location("monica_snapshot_publish", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.publish_local


def complete_manifests(root, pattern, *, highlight=False):
    files = {}
    incomplete = 0
    for manifest in sorted(root.glob(pattern)):
        if manifest.is_symlink() or any(parent.is_symlink() for parent in manifest.parents if parent.is_relative_to(root)):
            continue
        try:
            document = json.loads(manifest.read_text())
            if (document.get("schema_version") != 1 or not str(document.get("mediaid", "")).isdigit()
                    or not str(document.get("owner_id", "")).isdigit()):
                raise ValueError("Invalid manifest identity")
            names = document.get("files", [])
            if not names or any(not isinstance(n, str) or Path(n).name != n or n.startswith('.') for n in names):
                raise ValueError("Invalid manifest")
            if highlight and (document.get("archive_kind") != "highlight" or not str(document.get("highlight_id", "")).isdigit()):
                raise ValueError("Invalid highlight")
            paths = [manifest, *(manifest.parent / name for name in names)]
            if any(p.is_symlink() or not p.is_file() for p in paths):
                raise ValueError("Incomplete manifest")
            media = [p for p in paths[1:] if p.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp", ".mp4"}]
            if not media or (document.get("is_video") and not any(p.suffix.lower() == ".mp4" for p in media)):
                raise ValueError("Incomplete manifest media")
            for path in paths:
                info = path.stat()
                files[path.relative_to(root).as_posix()] = [info.st_size, info.st_mtime_ns]
        except (ValueError, OSError):
            incomplete += 1
    return files, incomplete


def publish_inventory(source, destination, state, after, prefix=""):
    before = json.loads(state.read_text()) if state.exists() and snapshot_ready(destination) else {}
    changed = [key for key, value in after.items() if before.get(key) != value]
    if changed:
        result = publisher()(destination, [(source / key, prefix + key) for key in changed])
        for key in changed:
            info = (source / key).stat()
            if after[key] != [info.st_size, info.st_mtime_ns]:
                raise RuntimeError("Archive changed during publication; checkpoint retained")
        atomic_json(state, after)
        return result
    return {"files": 0, "unchanged": True}


def snapshot_ready(destination):
    return (destination / "CURRENT").is_symlink() and (destination / "CURRENT/.crm-ready").is_file()


def instagram(root, data_root, *, kinds=("profiles", "stories", "highlights")):
    if not set(kinds) <= {"profiles", "stories", "highlights"}:
        raise ValueError("Unknown Instagram publication kind")
    result = {}
    if "profiles" in kinds:
        source = root / "data/profiles"
        inventory = {}
        for path in sorted(source.glob("*/*")):
            if (path.is_symlink() or path.parent.is_symlink() or not path.is_file()
                    or path.suffix.lower() not in {".json", ".jpg", ".jpeg", ".png", ".webp", ".mp4"}):
                continue
            info = path.stat()
            inventory[path.relative_to(source).as_posix()] = [info.st_size, info.st_mtime_ns]
        result["profiles"] = publish_inventory(source, data_root / "staging/instagram-current",
                  root / "state/published-profiles.json", inventory, "profiles/")
    for kind, pattern in (("stories", "*/*.manifest.json"), ("highlights", "*/*/*.manifest.json")):
        if kind not in kinds:
            continue
        source = root / "data" / kind
        inventory, incomplete = complete_manifests(source, pattern, highlight=kind == "highlights")
        result[kind] = {**publish_inventory(source, data_root / "staging" / ("instagram-" + kind),
                        root / "state" / ("published-" + kind + ".json"), inventory), "incomplete": incomplete}
    if any(value.get("files", 0) for value in result.values()):
        notify_importers(data_root)
    return result


def asset_signature(path):
    info = path.stat()
    return [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns]


def verify_asset(path, expected_hash, byte_count, cached):
    before = asset_signature(path)
    if before[2] != byte_count:
        raise ValueError("LinkedIn asset checksum mismatch")
    if cached == before:
        return before, 0
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for part in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(part)
    if before != asset_signature(path):
        raise RuntimeError("LinkedIn asset changed during verification")
    if digest.hexdigest() != expected_hash:
        raise ValueError("LinkedIn asset checksum mismatch")
    return before, byte_count


def linkedin(root, data_root, store):
    document = store.export_payload(include_unlinked=True)
    catalog, files = [], []
    verified_path = root / "state/verified-linkedin-assets.json"
    previous_verified = json.loads(verified_path.read_text()) if verified_path.exists() else {}
    verified = {}
    hashed_bytes = 0
    with sqlite3.connect(root / "archive.sqlite3") as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute("SELECT source_url,content_hash,media_type,byte_count,local_path,captured_at FROM assets ORDER BY local_path,source_url").fetchall()
    for record in rows:
        item = dict(record)
        name = item["local_path"]
        if not re.fullmatch(r"[a-f0-9]{64}\.[a-z0-9]+", name) or not name.startswith(item["content_hash"] + "."):
            raise ValueError("Invalid asset path")
        path = root / "assets" / name
        if path.is_symlink() or not path.is_file():
            raise ValueError("LinkedIn asset is missing; publication deferred")
        signature, read_bytes = verify_asset(path, item["content_hash"], item["byte_count"],
                                            verified.get(name) or previous_verified.get(name))
        verified[name] = signature
        hashed_bytes += read_bytes
        catalog.append(item)
        files.append((path, "assets/" + name))
    document.update(local_assets=catalog, local_asset_missing_files=0)
    if verified != previous_verified:
        atomic_json(verified_path, verified)
    # Avoid republishing identical exports (generated_at changes every invocation).
    fingerprint = hashlib.sha256(json.dumps({"profiles": document.get("profiles"), "assets": catalog}, sort_keys=True).encode()).hexdigest()
    state = root / "state/published-linkedin.json"
    destination = data_root / "raw/linkedin"
    current = destination / "CURRENT"
    previous = current.resolve() if current.is_symlink() else destination
    if (state.exists() and json.loads(state.read_text()).get("digest") == fingerprint
            and snapshot_ready(destination) and (previous / "linkedin_enrichment.incoming.json").is_file()
            and all((previous / name).is_file() for _, name in files)):
        return {"unchanged": True, "profiles": len(document.get("profiles", [])), "hashed_bytes": hashed_bytes}
    selected = {name: path for path, name in files if not (previous / name).is_file()}
    payload = root / "state/linkedin_enrichment.incoming.json"
    atomic_json(payload, document)
    result = publisher()(destination, [(path, name) for name, path in selected.items()] + [(payload, payload.name)])
    atomic_json(state, {"digest": fingerprint})
    notify_importers(data_root)
    return {**result, "profiles": len(document.get("profiles", [])), "hashed_bytes": hashed_bytes}
