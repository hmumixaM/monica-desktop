#!/usr/bin/env python3
"""Assemble native runtimes. Build tools are required only on the build Mac.

No user's sessions, databases or credentials are copied. Download archives are
checked against publisher checksums; the result records every native input.
"""

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import tarfile
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
DESKTOP = ROOT / "apps/desktop"
OUT = DESKTOP / "resources/runtime"
CACHE = DESKTOP / ".build/downloads"
NODE_VERSION = "22.21.1"
CADDY_VERSION = "2.10.2"
PYTHON_VERSION = "3.12.11"


def run(command, **kwargs):
    print("+", " ".join(map(str, command)), flush=True)
    return subprocess.run(list(map(str, command)), check=True, **kwargs)


def copytree(source, destination):
    if destination.exists():
        shutil.rmtree(destination)
    shutil.copytree(source, destination, symlinks=True,
                    ignore=shutil.ignore_patterns("__pycache__", ".DS_Store", ".venv", "*.pyc", ".pytest_cache", ".git", "tests", "test-results"))


def download(url, name):
    CACHE.mkdir(parents=True, exist_ok=True)
    path = CACHE / name
    if not path.exists():
        temporary = path.with_suffix(path.suffix + ".part")
        with urllib.request.urlopen(url, timeout=120) as response, temporary.open("wb") as output:
            shutil.copyfileobj(response, output)
        temporary.replace(path)
    return path


def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def verify(archive, checksums):
    text = checksums.read_text()
    expected = next((line.split()[0] for line in text.splitlines() if line.split()[-1].lstrip("*") == archive.name), None)
    algorithm = "sha512" if expected and len(expected) == 128 else "sha256"
    actual = hashlib.file_digest(archive.open("rb"), algorithm).hexdigest()
    if expected is None or actual != expected:
        raise RuntimeError(f"Checksum mismatch: {archive.name}")


def extract(archive, destination):
    destination.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive) as source:
        source.extractall(destination, filter="data")


def bundle_python():
    source = Path(subprocess.check_output(["uv", "python", "find", PYTHON_VERSION], text=True).strip()).resolve().parent.parent
    if not (OUT / "python/bin/python3").exists():
        copytree(source, OUT / "python")
    python = OUT / "python/bin/python3"
    actual = subprocess.check_output([python, "-c", "import platform; print(platform.python_version())"], text=True).strip()
    if actual != PYTHON_VERSION:
        copytree(source, OUT / "python")
    lock = DESKTOP / "runtime-requirements.txt"
    if not lock.exists():
        run(["uv", "pip", "compile", ROOT / "services/api/pyproject.toml", ROOT / "importers/faces/pyproject.toml",
             DESKTOP / "collectors/pyproject.toml", "--python-version", "3.12", "--generate-hashes", "--output-file", lock])
    # VCS dependencies are pinned by full commit; package archives by hashes.
    lines = lock.read_text().splitlines()
    vcs = [line for line in lines if " @ git+" in line]
    if any(not re.search(r"@[0-9a-f]{40}$", line) for line in vcs):
        raise RuntimeError("Every VCS dependency must be pinned to a full commit")
    filtered = DESKTOP / ".build/runtime-pypi.txt"
    filtered.parent.mkdir(parents=True, exist_ok=True)
    filtered.write_text("\n".join(line for line in lines if line not in vcs) + "\n")
    # This is a private copy, not uv's managed interpreter installation.
    run(["uv", "pip", "install", "--python", python, "--break-system-packages", "--no-deps", "--require-hashes", "-r", filtered])
    if vcs:
        run(["uv", "pip", "install", "--python", python, "--break-system-packages", "--no-deps", *vcs])
    return {"version": PYTHON_VERSION}


def bundle_node():
    name = f"node-v{NODE_VERSION}-darwin-arm64.tar.gz"
    base = f"https://nodejs.org/dist/v{NODE_VERSION}/"
    archive = download(base + name, name)
    checksum = download(base + "SHASUMS256.txt", f"node-{NODE_VERSION}-checksums.txt")
    verify(archive, checksum)
    binary = OUT / "node/bin/node"
    if not binary.exists() or subprocess.check_output([binary, "--version"], text=True).strip() != f"v{NODE_VERSION}":
        extract(archive, CACHE / "node-extract")
        copytree(CACHE / "node-extract" / name.removesuffix(".tar.gz"), OUT / "node")
    return {"version": NODE_VERSION, "sha256": sha256(archive), "source": base + name}


def bundle_caddy():
    name = f"caddy_{CADDY_VERSION}_mac_arm64.tar.gz"
    base = f"https://github.com/caddyserver/caddy/releases/download/v{CADDY_VERSION}/"
    archive = download(base + name, name)
    checksums = download(base + f"caddy_{CADDY_VERSION}_checksums.txt", f"caddy-{CADDY_VERSION}-checksums.txt")
    verify(archive, checksums)
    binary = OUT / "caddy/caddy"
    if not binary.exists() or subprocess.check_output([binary, "version"], text=True).split()[0] != f"v{CADDY_VERSION}":
        extract(archive, OUT / "caddy")
    return {"version": CADDY_VERSION, "sha256": sha256(archive), "source": base + name}


def mach_o(path):
    if path.is_symlink() or not path.is_file():
        return False
    with path.open("rb") as handle:
        return handle.read(4) in (b"\xcf\xfa\xed\xfe", b"\xca\xfe\xba\xbe", b"\xbe\xba\xfe\xca")


def relocate_postgres(prefix):
    target = OUT / "postgres"
    build_input = sha256(prefix / "bin/postgres")
    if (target / ".relocated").exists():
        cached = json.loads((target / ".relocated").read_text())
        if cached.get("build_input_sha256") == build_input:
            return cached
    if target.exists():
        shutil.rmtree(target)
    (target / "bin").mkdir(parents=True)
    (target / "lib").mkdir()
    for name in ("postgres", "initdb", "pg_ctl", "pg_isready", "createdb", "pg_dump", "pg_restore", "psql"):
        shutil.copy2(prefix / "bin" / name, target / "bin" / name)
    copytree(prefix / "share", target / "share")
    copytree(prefix / "lib", target / "lib")
    # Homebrew packages put pkglibdir under lib/postgresql; retain that layout.
    pending = [path for path in target.rglob("*") if mach_o(path)]
    origins = {path: (prefix / path.relative_to(target)).resolve() for path in pending}
    seen = set()
    inputs = {}
    while pending:
        path = pending.pop()
        if path in seen:
            continue
        seen.add(path)
        lines = subprocess.check_output(["otool", "-L", str(path)], text=True).splitlines()[1:]
        for line in lines:
            dependency = line.strip().split(" (", 1)[0]
            if dependency.startswith("@loader_path/"):
                source = (origins[path].parent / dependency.removeprefix("@loader_path/")).resolve()
            elif dependency.startswith("@rpath/"):
                source = (origins[path].parent / dependency.removeprefix("@rpath/")).resolve()
            elif dependency.startswith(("/opt/homebrew/", "/usr/local/")):
                source = Path(dependency).resolve()
            else:
                continue
            if not source.is_file():
                raise RuntimeError(f"Cannot resolve native dependency {dependency} for {path.name}")
            destination = target / "lib" / source.name
            if source == path.resolve():
                continue
            if not destination.exists():
                shutil.copy2(source, destination)
                pending.append(destination)
                origins[destination] = source
                inputs[source.name] = sha256(source)
            relative = os.path.relpath(destination, path.parent)
            run(["install_name_tool", "-change", dependency, "@loader_path/" + relative, path], stdout=subprocess.DEVNULL)
        # Ensure libraries don't retain a Homebrew install id.
        if path.suffix in {".dylib", ".so"}:
            run(["install_name_tool", "-id", "@rpath/" + path.name, path], stdout=subprocess.DEVNULL)
        run(["codesign", "--force", "--sign", "-", path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for path in seen:
        linked = subprocess.check_output(["otool", "-L", str(path)], text=True)
        if "/opt/homebrew/" in linked or "/usr/local/" in linked:
            raise RuntimeError(f"Nonportable PostgreSQL dependency: {path.name}")
    version = subprocess.check_output([str(target / "bin/postgres"), "--version"], text=True).strip()
    result = {"version": version, "build_input_sha256": build_input, "native_inputs": inputs}
    (target / ".relocated").write_text(json.dumps(result, indent=2))
    return result


def copy_sources():
    shutil.copy2(DESKTOP / "runtime/capabilities.json", OUT / "capabilities.json")
    for source, dest in ((DESKTOP / "runtime/monica_desktop", OUT / "desktop/monica_desktop"),
                         (ROOT / "services/api/app", OUT / "api/app"), (ROOT / "migrations", OUT / "migrations"),
                         (ROOT / "importers/faces/monica_faces", OUT / "faces/monica_faces"),
                         (DESKTOP / "collectors", OUT / "collectors"), (ROOT / "infra/wechat", OUT / "wechat")):
        dest.parent.mkdir(parents=True, exist_ok=True)
        copytree(source, dest)
    shutil.copy2(ROOT / "services/worker/worker.py", OUT / "worker.py")
    (OUT / "infra").mkdir(exist_ok=True)
    shutil.copy2(ROOT / "infra/snapshot_publish.py", OUT / "infra/snapshot_publish.py")


def build_city_reference():
    env = dict(os.environ, PYTHONHOME=str(OUT / "python"), PYTHONPATH=str(OUT / "api"), PYTHONNOUSERSITE="1", PYTHONDONTWRITEBYTECODE="1")
    run([OUT / "python/bin/python3", "-m", "app.city_reference"], env=env)


def bundle_web(skip):
    env = dict(os.environ, PATH=str(OUT / "node/bin") + os.pathsep + os.environ["PATH"], NEXT_TELEMETRY_DISABLED="1")
    if not skip:
        run([OUT / "node/bin/node", ROOT / "node_modules/next/dist/bin/next", "build", ROOT / "apps/web"], cwd=ROOT, env=env)
    source = ROOT / "apps/web/.next/standalone"
    if not source.is_dir():
        raise RuntimeError("Build Next.js before bundling")
    copytree(source, OUT / "web")
    copytree(ROOT / "apps/web/.next/static", OUT / "web/apps/web/.next/static")
    copytree(ROOT / "apps/web/public", OUT / "web/apps/web/public")


def bundle_faces(models):
    source = models / "buffalo_l"
    destination = OUT / "models/insightface/models/buffalo_l"
    destination.mkdir(parents=True, exist_ok=True)
    for name in ("det_10g.onnx", "w600k_r50.onnx"):
        if not (source / name).is_file():
            raise RuntimeError("Provide InsightFace buffalo_l detection/recognition models with --face-models")
        shutil.copy2(source / name, destination / name)
    return {path.name: sha256(path) for path in destination.iterdir() if path.is_file()}


def prune_python_entrypoints():
    # All runtime entrypoints use python -m. Absolute build-machine shebangs
    # aren't relocatable; remove their aliases too, as Tauri follows symlinks.
    directory = OUT / "python/bin"
    if not directory.exists():
        return
    for path in directory.iterdir():
        if path.is_file() and not path.is_symlink() and not mach_o(path) and path.name != "python3-config":
            if path.read_bytes()[:2] == b"#!":
                path.unlink()
    for path in directory.iterdir():
        if path.is_symlink() and not path.exists():
            path.unlink()
    for path in OUT.rglob("__pycache__"):
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
    # Homebrew installs read-only artifacts. Copied resources need owner-write
    # permission so Tauri can replace its own files on an incremental rebuild.
    for path in OUT.rglob("*"):
        if path.is_file() and not path.is_symlink():
            path.chmod(path.stat().st_mode | 0o200)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--postgres-prefix", type=Path, default=Path("/opt/homebrew/opt/postgresql@16"))
    parser.add_argument("--chatlog-binary", type=Path)
    parser.add_argument("--face-models", type=Path, default=Path.home() / ".insightface/models")
    parser.add_argument("--skip-web-build", action="store_true")
    parser.add_argument("--sources-only", action="store_true")
    args = parser.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    copy_sources()
    if args.sources_only:
        if (OUT / "python/bin/python3").exists():
            build_city_reference()
            prune_python_entrypoints()
        return
    manifest = {"schema": 1, "architecture": "arm64", "python": bundle_python(),
                "node": bundle_node(), "caddy": bundle_caddy(), "postgres": relocate_postgres(args.postgres_prefix)}
    manifest["runtime_requirements_sha256"] = sha256(DESKTOP / "runtime-requirements.txt")
    if args.chatlog_binary:
        (OUT / "chatlog").mkdir(exist_ok=True)
        shutil.copy2(args.chatlog_binary, OUT / "chatlog/chatlog")
    if not (OUT / "chatlog/chatlog").is_file():
        raise RuntimeError("Supply --chatlog-binary built from the versioned desktop patch")
    manifest["chatlog"] = {"sha256": sha256(OUT / "chatlog/chatlog")}
    manifest["face_models"] = bundle_faces(args.face_models)
    build_city_reference()
    env = dict(os.environ, PLAYWRIGHT_BROWSERS_PATH=str(OUT / "browsers"), PYTHONNOUSERSITE="1")
    run([OUT / "python/bin/python3", "-m", "playwright", "install", "chromium"], env=env)
    bundle_web(args.skip_web_build)
    (OUT / "manifest.json").write_text(json.dumps(manifest, indent=2))
    prune_python_entrypoints()
    print(f"Runtime ready: {OUT}")


if __name__ == "__main__":
    main()
