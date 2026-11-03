#!/usr/bin/env python3
"""Create a separately signed Mac App Store package; never alter an installed app.

This packages a built app and verifies its signatures. It does not certify
sandbox runtime compatibility and does not execute the app or its collectors.
"""
import argparse
import hashlib
import json
import plistlib
import shutil
import struct
import subprocess
from pathlib import Path


def run(*command):
    subprocess.run([str(part) for part in command], check=True)


def macho_type(path):
    if path.is_symlink() or not path.is_file():
        return None
    with path.open('rb') as handle:
        header = handle.read(16)
    if len(header) < 16:
        return None
    if header[:4] == b'\xcf\xfa\xed\xfe':
        return struct.unpack('<I', header[12:16])[0]
    if header[:4] in (b'\xca\xfe\xba\xbe', b'\xbe\xba\xfe\xca'):
        # Universal Mach-O files in this bundle are libraries. Let codesign
        # inspect them, but never infer executable entitlements from a suffix.
        details = subprocess.check_output(['file', '-b', str(path)], text=True)
        return 2 if 'executable' in details else 6
    return None


def restore_framework_links(app):
    """Restore versioned framework links omitted by Tauri's resource copier.

    Chromium has one canonical version directory. A flattened top-level binary
    makes Apple's validator interpret the version directory as another broken
    framework, even when a local deep signature check succeeds.
    """
    restored = 0
    for framework in app.rglob('*.framework'):
        if framework.is_symlink() or not framework.is_dir():
            continue
        versions = framework / 'Versions'
        if not versions.is_dir():
            continue
        candidates = [path for path in versions.iterdir()
                      if path.name != 'Current' and path.is_dir() and not path.is_symlink()]
        if len(candidates) != 1:
            raise ValueError(f'Expected one canonical framework version: {framework.name}')
        version = candidates[0]
        info = plistlib.loads((version / 'Resources/Info.plist').read_bytes())
        executable = info['CFBundleExecutable']
        if Path(executable).name != executable or not (version / executable).is_file():
            raise ValueError(f'Invalid framework executable: {framework.name}')
        if info.get('CFBundlePackageType') != 'FMWK':
            raise ValueError(f'Invalid framework package type: {framework.name}')

        def replace_link(path, target):
            nonlocal restored
            if path.is_symlink() and path.readlink() == Path(target):
                if not path.resolve().is_relative_to(framework.resolve()):
                    raise ValueError('Framework link escapes its bundle')
                return
            if path.is_symlink() or path.is_file():
                path.unlink()
            elif path.is_dir():
                shutil.rmtree(path)
            path.symlink_to(target)
            restored += 1

        replace_link(versions / 'Current', version.name)
        for child in version.iterdir():
            if child.name != '_CodeSignature':
                replace_link(framework / child.name, Path('Versions/Current') / child.name)
        # A previously flattened framework may have two incompatible seals.
        # All code is signed again below, after the canonical layout is restored.
        for seal in (framework / '_CodeSignature', version / '_CodeSignature'):
            if seal.is_symlink():
                seal.unlink()
            elif seal.exists():
                shutil.rmtree(seal)
    return restored


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--app', type=Path, required=True)
    parser.add_argument('--profile', type=Path, required=True)
    parser.add_argument('--identity', required=True)
    parser.add_argument('--installer-identity', required=True)
    parser.add_argument('--team-id', required=True)
    parser.add_argument('--build', required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--lite', action='store_true', help='Build the reduced App Store edition approved for release')
    parser.add_argument('--node-binary', type=Path, help='Node built by build_appstore_node.py; required for --lite')
    args = parser.parse_args()
    if not args.build.isdecimal() or int(args.build) < 1:
        parser.error('--build must be a positive integer')
    source, output = args.app.resolve(), args.output.resolve()
    if not source.is_dir() or source.suffix != '.app':
        parser.error('--app must be an existing application bundle')
    if output.exists():
        parser.error('--output must be a new directory')
    node_report = None
    if args.lite:
        if not args.node_binary or not args.node_binary.is_file() or not args.node_binary.with_suffix('.json').is_file():
            parser.error('--lite requires --node-binary and its source build report')
        node_report = json.loads(args.node_binary.with_suffix('.json').read_text())
        digest = hashlib.sha256()
        with args.node_binary.open('rb') as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b''):
                digest.update(block)
        if digest.hexdigest() != node_report.get('node_sha256') or not node_report.get('private_launchservices_process_title_removed'):
            parser.error('App Store Node source build report does not match its binary')
    profile = plistlib.loads(subprocess.check_output(['security', 'cms', '-D', '-i', str(args.profile)]))
    identifier = 'org.monica.desktop'
    if profile['Entitlements'].get('com.apple.application-identifier') != f'{args.team_id}.{identifier}':
        parser.error('Provisioning profile does not match the Monica app and team')
    output.mkdir(parents=True)
    app = output / 'Monica.app'
    run('cp', '-cR', source, app)
    edition_report = None
    if args.lite:
        runtime = app / 'Contents/Resources/runtime'
        shutil.copy2(args.node_binary, runtime / 'node/bin/node')
        shutil.copy2(args.node_binary.with_suffix('.json'), runtime / 'node/appstore-build.json')
        from prune_appstore import prune
        edition_report = prune(runtime)
        manifest_path = runtime / 'manifest.json'
        manifest = json.loads(manifest_path.read_text())
        manifest.update(distribution='app-store-lite', node=node_report, face_models={'mode': 'unavailable'},
                        appstore_python_packages=edition_report['python_packages_kept'])
        manifest_path.write_text(json.dumps(manifest, indent=2) + '\n')
        run('python3', Path(__file__).with_name('verify_appstore_apis.py'), '--app', app,
            '--report', output / 'api-scan.json')
    restored_links = restore_framework_links(app)
    info_path = app / 'Contents/Info.plist'
    info = plistlib.loads(info_path.read_bytes())
    if info.get('CFBundleIdentifier') != identifier:
        parser.error('Unexpected app bundle identifier')
    info['CFBundleVersion'] = args.build
    info_path.write_bytes(plistlib.dumps(info))
    shutil.copyfile(args.profile, app / 'Contents/embedded.provisionprofile')
    directory = Path(__file__).resolve().parents[1] / 'src-tauri'
    entitlements = plistlib.loads((directory / 'Entitlements.appstore.plist').read_bytes())
    entitlements.update({
        'com.apple.application-identifier': f'{args.team_id}.{identifier}',
        'com.apple.developer.team-identifier': args.team_id,
    })
    main_entitlements = output / 'main.entitlements.plist'
    main_entitlements.write_bytes(plistlib.dumps(entitlements))
    child_entitlements = directory / 'Entitlements.child.plist'
    main_binary = app / 'Contents/MacOS' / info['CFBundleExecutable']
    binaries = [(path, macho_type(path)) for path in app.rglob('*')]
    signed = 0
    for path, kind in binaries:
        if kind is None or path == main_binary:
            continue
        command = ['codesign', '--force', '--sign', args.identity, '--options', 'runtime', '--timestamp=none']
        if kind == 2:
            command += ['--entitlements', str(child_entitlements)]
        run(*command, path)
        signed += 1
    bundles = sorted((path for path in app.rglob('*') if path.is_dir() and
                      not path.is_symlink() and path.suffix in ('.app', '.framework', '.xpc')),
                     key=lambda path: len(path.parts), reverse=True)
    for path in bundles:
        command = ['codesign', '--force', '--sign', args.identity, '--options', 'runtime', '--timestamp=none']
        if path.suffix in ('.app', '.xpc'):
            command += ['--entitlements', str(child_entitlements)]
        run(*command, path)
    # App Store checks the currently valid distribution certificate and re-signs
    # the app. Developer ID timestamping is a separate distribution workflow.
    run('codesign', '--force', '--sign', args.identity, '--options', 'runtime', '--timestamp=none',
        '--entitlements', main_entitlements, app)
    run('codesign', '--verify', '--deep', '--strict', '--verbose=2', app)
    package = output / f"Monica-{info['CFBundleShortVersionString']}-{args.build}-macos.pkg"
    run('productbuild', '--sign', args.installer_identity, '--component', app, '/Applications', package)
    run('pkgutil', '--check-signature', package)
    (output / 'package-report.json').write_text(json.dumps({
        'bundle_id': identifier, 'version': info['CFBundleShortVersionString'], 'build': args.build,
        'signed_macho_files': signed + 1, 'signed_nested_bundles': len(bundles),
        'framework_links_restored': restored_links,
        'package': package.name, 'signature_verified': True,
        'sandbox_runtime_verified': False,
        'edition': edition_report,
    }, indent=2) + '\n')
    print(package)


if __name__ == '__main__':
    main()
