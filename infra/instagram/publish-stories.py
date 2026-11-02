"""Publish complete local Story manifests and files; never access Instagram."""
import argparse
import fcntl
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from snapshot_publish import publish


def inventory(root):
    files = {}
    incomplete = 0
    for manifest in sorted(root.glob('*/*.manifest.json')):
        if manifest.is_symlink() or manifest.parent.is_symlink():
            continue
        document = json.loads(manifest.read_text())
        names = document.get('files', [])
        if not names or any(not isinstance(n, str) or Path(n).name != n for n in names):
            incomplete += 1
            continue
        paths = [manifest.parent / n for n in names] + [manifest]
        if any(p.is_symlink() or not p.is_file() for p in paths):
            incomplete += 1
            continue
        for path in paths:
            stat = path.stat()
            files[path.relative_to(root).as_posix()] = [stat.st_size, stat.st_mtime_ns]
    return files, incomplete


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--host', default='linkedin-nas')
    parser.add_argument('--destination', default='/volume1/docker/monica-next/data/staging/instagram-stories')
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args()
    state = args.root / 'state/crm-published-stories.json'
    state.parent.mkdir(parents=True, exist_ok=True)
    with state.with_suffix('.lock').open('w') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print(json.dumps({'skipped': 'publisher already running'}))
            return
        root = args.root / 'data/stories'
        after, incomplete = inventory(root)
        before = json.loads(state.read_text()) if state.exists() else {}
        changed = [key for key, value in after.items() if before.get(key) != value]
        # Republish the manifest last whenever any of its files changed.
        manifests = {key for key in after if key.endswith('.manifest.json')}
        selected = set(changed)
        for key in manifests:
            doc = json.loads((root / key).read_text())
            if any(str(Path(key).parent / n) in selected for n in doc['files']):
                selected.add(key)
        if selected and not args.dry_run:
            publish(args.host, args.destination, [(root/key,key) for key in sorted(selected)])
            for key in selected:
                stat = (root / key).stat()
                if after[key] != [stat.st_size, stat.st_mtime_ns]:
                    raise RuntimeError('Story changed during publication; retry next cycle')
            temporary = state.with_suffix('.tmp')
            temporary.write_text(json.dumps(after, sort_keys=True))
            temporary.chmod(0o600)
            temporary.replace(state)
        print(json.dumps({'complete_manifests': len([k for k in after if k.endswith('.manifest.json')]),
                          'changed_files': len(selected), 'incomplete_manifests': incomplete, 'published': not args.dry_run}), flush=True)


if __name__ == '__main__':
    main()
