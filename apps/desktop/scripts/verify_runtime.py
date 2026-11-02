#!/usr/bin/env python3
"""Fail packaging if collectors, importer modules, pins or bundled Chromium are missing."""
import argparse
import hashlib
import json
import os
import subprocess
import tempfile
from pathlib import Path


def verify_files(root):
    root = root.resolve()
    contract = json.loads((root / "capabilities.json").read_text())
    if contract.get('face_models', {}).get('mode') == 'external':
        # ONNX itself ships small operator-test fixtures. Only application
        # models and the face weight filenames are excluded from this build.
        for path in root.rglob('*.onnx'):
            if path.is_relative_to(root / 'models') or path.name in ('det_10g.onnx', 'w600k_r50.onnx'):
                raise ValueError('External-model release unexpectedly includes model weights')
    for capability, paths in contract["capabilities"].items():
        for name in paths:
            path = root / name
            if not path.is_file() or not path.resolve().is_relative_to(root):
                raise ValueError(f"Missing or external component: {capability}/{name}")
    provenance = json.loads((root / "collectors/VENDORED_SOURCES.json").read_text())
    for item in provenance["sources"]:
        path = root / "collectors" / item["file"]
        if not path.is_file() or not path.resolve().is_relative_to(root):
            raise ValueError("Missing vendored collector: " + item["file"])
        if hashlib.sha256(path.read_bytes()).hexdigest() != item["bundled_sha256"]:
            raise ValueError("Collector hash mismatch: " + item["file"])
    return contract, len(provenance["sources"])


PROBE = r'''
import hashlib,importlib,importlib.metadata as metadata,json,pathlib,sys
root=pathlib.Path(sys.argv[1]);contract=json.loads((root/'capabilities.json').read_text())
assert pathlib.Path(sys.executable).resolve().is_relative_to(root)
for name in ('collector','linkedin_enrichment.cli','app.wechat_contacts','app.import_messages',
             'app.reconcile_sources','app.sync_instagram_stories','app.sync_linkedin','app.archive_linkedin',
             'app.cdc','app.cdc_codec','app.projection_feed','app.media_dependencies','monica_faces.run'):
    module=importlib.import_module(name)
    assert pathlib.Path(module.__file__).resolve().is_relative_to(root), name
versions={name:metadata.version(name) for name in contract['packages']}
assert versions==contract['packages'], versions
commits={}
for name,expected in contract['vcs_commits'].items():
    distribution=metadata.distribution(name)
    actual=json.loads(distribution.read_text('direct_url.json'))['vcs_info']['commit_id']
    assert actual==expected, name
    commits[name]=actual
vendored={}
for name,spec in contract.get('vendored_distributions',{}).items():
    manifest=root/spec['manifest']
    assert hashlib.sha256(manifest.read_bytes()).hexdigest()==spec['manifest_sha256'], name
    source=json.loads(manifest.read_text())
    assert source['commit']==spec['commit'] and metadata.version(name)==spec['version'], name
    module=importlib.import_module(spec['module'])
    directory=pathlib.Path(module.__file__).resolve().parent
    assert directory.is_relative_to(root), name
    for entry in source['package_files']:
        path,expected=entry['path'],entry['sha256']
        assert hashlib.sha256((directory/path).read_bytes()).hexdigest()==expected, name+':'+path
    vendored[name]=source['commit']
from playwright.sync_api import sync_playwright
with sync_playwright() as p:
    assert pathlib.Path(p.chromium.executable_path).resolve().is_relative_to(root)
    browser=p.chromium.launch(headless=True)
    page=browser.new_page();page.set_content('<title>Monica bundled runtime</title>')
    assert page.title()=='Monica bundled runtime'
    browser.close()
print(json.dumps({'packages':versions,'vcs_commits':commits,'vendored_distribution_commits':vendored,'chromium':'passed','importer_modules':'passed'}))
'''


def verify(root):
    contract, count = verify_files(root)
    python = root / "python/bin/python3"
    with tempfile.TemporaryDirectory(prefix="monica-bundle-check-") as folder:
        private = Path(folder)
        env = {key: os.environ[key] for key in ("HOME", "USER", "TMPDIR", "LANG") if key in os.environ}
        env.update({
            "PATH": str(root / "python/bin") + os.pathsep + "/usr/bin:/bin:/usr/sbin:/sbin",
            "PYTHONHOME": str(root / "python"), "PYTHONNOUSERSITE": "1", "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONPATH": os.pathsep.join(str(root / name) for name in ("api", "faces", "collectors")),
            "PLAYWRIGHT_BROWSERS_PATH": str(root / "browsers"),
            "MONICA_INSTAGRAM_ROOT": str(private / "instagram"), "APP_ENV": "desktop",
            "RAW_ROOT": str(private / "raw"), "MEDIA_ROOT": str(private / "media"),
            "STAGING_ROOT": str(private / "staging"), "SESSION_SECRET": "s" * 48,
            "IMPORT_HMAC_SECRET": "i" * 48, "AI_ENABLED": "false",
            "DATABASE_URL": "postgresql+psycopg://unused:unused@127.0.0.1:9/unused",
        })
        result = subprocess.run([str(python), "-c", PROBE, str(root)], env=env, cwd=folder,
                                capture_output=True, text=True, timeout=90, check=True)
        report = json.loads(result.stdout.splitlines()[-1])
        entries = [
            [str(python), '-m', 'app.cdc', '--help'],
            [str(python), '-m', 'app.projection_feed', '--help'],
            [str(python), '-m', 'app.sync_archives', '--help'],
            [str(root / "chatlog/chatlog"), "serve", "--help"],
            [str(python), str(root / "collectors/run_linkedin.py"), "--help"],
            *[[str(python), str(root / "collectors/vendor/instagram" / name), "--help"] for name in (
                "archive_profile.py", "archive_followees.py", "archive_stories.py", "archive_highlights.py", "followee_refresh.py")],
            *[[str(python), str(root / "collectors/collector.py"), "--provider", provider,
               "--data-root", str(private), "status"] for provider in ("instagram", "linkedin")],
        ]
        for command in entries:
            subprocess.run(command, env=env, cwd=folder, capture_output=True, text=True, timeout=30, check=True)
    return {"capabilities": list(contract["capabilities"]), "face_models": contract.get('face_models', {}).get('mode', 'bundled'), "vendored_files_verified": count,
            "entrypoint_checks": len(entries), **report}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle-root", required=True, type=Path)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    try:
        result = verify(args.bundle_root.resolve())
    except subprocess.CalledProcessError as error:
        raise SystemExit("Bundled runtime probe failed:\n" + error.stderr[-4000:]) from None
    if args.report:
        args.report.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
