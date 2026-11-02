"""Locked NAS installation and recovery of a verified deployment bundle."""
import importlib.util
import io
import shutil
import tarfile
import time
from pathlib import Path

from release_common import digest, read_json, validate_v2, write_json


def unpack(data, manifest, destination):
    validate_v2(manifest)
    if digest(data) != manifest['bundle']['sha256']:
        raise ValueError('Deployment archive checksum mismatch')
    destination = Path(destination)
    with tarfile.open(fileobj=io.BytesIO(data)) as archive:
        members = [m for m in archive.getmembers() if not m.isdir()]
        if len({m.name for m in members}) != len(members) or {m.name for m in members} != set(manifest['files']):
            raise ValueError('Archive contains unexpected or duplicate paths')
        for member in members:
            if not member.isfile():
                raise ValueError('Links and special files are forbidden')
            content = archive.extractfile(member).read()
            if digest(content) != manifest['files'][member.name]:
                raise ValueError('Deployment file checksum mismatch')
            target = destination / member.name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
            target.chmod(member.mode & 0o755)
    write_json(destination / 'release.json', manifest)


def load_deployer(stage):
    spec = importlib.util.spec_from_file_location('release_deployer', Path(stage) / 'infra/deploy-release.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def restore_files(root, record):
    for name in record['installed']:
        backup = Path(record['backup']) / name
        target = root / name
        if backup.exists():
            shutil.copy2(backup, target)
        elif target.exists():
            target.unlink()


def install(root, stage, event):
    """The caller holds .deploy.lock from before this call through completion."""
    root, stage = Path(root), Path(stage)
    manifest = validate_v2(read_json(stage / 'release.json'))
    deploy = load_deployer(stage)
    deploy.validate_manifest(manifest, stage / 'infra')
    record_path = root / '.cd-install.json'
    record = read_json(record_path)
    live = read_json(root / '.release-state.json', {})
    if (live.get('release') == manifest['commit']
            and live.get('manifest') == manifest and live.get('observed_seconds', 0) >= 300
            and not (root / '.deploy-journal.json').exists()):
        if record:
            record.update(status='complete', completed_at=time.time())
            write_json(Path(record['history']) / 'install.json', record)
            record_path.unlink()
        return {'release': manifest['commit'], 'already_verified': True,
                'observed_seconds': live['observed_seconds'], 'runtime': live.get('verification')}
    if record and record['release'] != manifest['commit']:
        raise RuntimeError('An interrupted installation must recover its pinned release first')
    if record is None:
        if (root / '.deploy-journal.json').exists():
            raise RuntimeError('A manual deployment journal must be recovered before installation')
        stamp = manifest['commit'] + '-' + str(time.time_ns())
        history = root / '.release-history' / ('cd-' + stamp)
        history.mkdir(parents=True, mode=0o700)
        names = [p[6:] for p in manifest['files'] if p.startswith('infra/')]
        for name in names:
            source = root / name
            if source.exists():
                backup = history / 'infra' / name
                backup.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, backup)
        record = {'release': manifest['commit'], 'previous_release': live.get('release'), 'backup': str(history / 'infra'),
                  'history': str(history), 'installed': names, 'status': 'installing'}
        write_json(record_path, record)
        for name in names:
            source, target = stage / 'infra' / name, root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary = target.with_suffix(target.suffix + '.cd-tmp')
            shutil.copy2(source, temporary)
            temporary.replace(target)
        record['status'] = 'installed'
        write_json(record_path, record)
    elif record['status'] == 'installing':
        # No deployment was invoked until every file had been installed.
        restore_files(root, record)
        record_path.unlink()
        return install(root, stage, event)
    deploy.validate_manifest(manifest, root)
    release = deploy.Deployment(root, manifest)
    release.event = event
    try:
        deploy.execute_release(release)
    except BaseException:
        current = read_json(root / '.release-state.json', {})
        if current.get('release') != manifest['commit'] and not release.journal_path.exists():
            restore_files(root, record)
            record['status'] = 'rolled_back'
            write_json(Path(record['history']) / 'install.json', record)
            record_path.unlink()
        raise
    record.update(status='complete', completed_at=time.time())
    write_json(Path(record['history']) / 'install.json', record)
    record_path.unlink()
    result = read_json(root / '.release-state.json')
    return {'release': manifest['commit'], 'installation_record': str(Path(record['history']) / 'install.json'),
            'timings': result.get('timings', []), 'runtime': result.get('verification'),
            'observed_seconds': result.get('observed_seconds'), 'cleanup_errors': result.get('cleanup_errors', [])}


def upgrade_runtime(stage, cd_root):
    stage, cd_root = Path(stage), Path(cd_root)
    manifest = read_json(stage / 'release.json')
    version = cd_root / 'versions' / manifest['commit']
    version.mkdir(parents=True, exist_ok=True)
    for name in ('nas-cd.py', 'release_common.py', 'release_installer.py'):
        shutil.copy2(stage / 'infra' / name, version / name)
    link = cd_root / 'current.next'
    if link.is_symlink():
        link.unlink()
    link.symlink_to(version)
    link.replace(cd_root / 'current')
