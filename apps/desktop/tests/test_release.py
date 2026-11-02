import hashlib
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('release_desktop', Path(__file__).parents[1] / 'scripts/release_desktop.py')
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)


class ReleaseTests(unittest.TestCase):
    def setUp(self):
        self.work = tempfile.TemporaryDirectory()
        self.addCleanup(self.work.cleanup)
        self.folder = Path(self.work.name)
        self.sha = 'a' * 40
        assets = []
        for name in (f'Monica-0.1.0-macos-arm64-{self.sha[:12]}.zip', 'runtime-verification.json', 'smoke-verification.json'):
            payload = b'fixture'
            (self.folder / name).write_bytes(payload)
            assets.append({'name': name, 'bytes': len(payload), 'sha256': hashlib.sha256(payload).hexdigest()})
        self.metadata = {'schema': 1, 'source_commit': self.sha, 'version': '0.1.0', 'assets': assets}
        self.write_manifest()

    def write_manifest(self):
        (self.folder / 'release.json').write_text(json.dumps(self.metadata))
        (self.folder / 'SHA256SUMS').write_text(''.join(f'{release.digest(p)}  {p.name}\n'
            for p in sorted(self.folder.iterdir()) if p.name != 'SHA256SUMS'))

    def test_commit_and_complete_asset_checksums_are_required(self):
        self.assertEqual(release.verify(self.folder, self.sha)['source_commit'], self.sha)
        with self.assertRaisesRegex(ValueError, 'source commit'):
            release.verify(self.folder, 'b' * 40)
        (self.folder / self.metadata['assets'][0]['name']).write_bytes(b'changed')
        with self.assertRaises(ValueError):
            release.verify(self.folder, self.sha)

    def test_private_extra_file_or_missing_archive_refuses_publication(self):
        extra = self.folder / 'settings.json'
        extra.write_text('{}')
        with self.assertRaisesRegex(ValueError, 'Unexpected files'):
            release.verify(self.folder, self.sha)
        extra.unlink()
        self.metadata['assets'] = self.metadata['assets'][1:]
        self.write_manifest()
        with self.assertRaisesRegex(ValueError, 'Incomplete'):
            release.verify(self.folder, self.sha)

    def test_version_tag_and_feature_branch_are_rejected_without_remote_writes(self):
        with patch.object(release, 'gh') as gh:
            for ref in ('refs/heads/feature', 'refs/tags/v0.2.0'):
                with self.assertRaises(ValueError):
                    release.publish(self.folder, self.sha, 'example/monica', ref)
            gh.assert_not_called()

    def test_newer_main_prevents_stale_preview_upload(self):
        with patch.object(release, 'gh', return_value='b' * 40) as gh:
            release.publish(self.folder, self.sha, 'example/monica', 'refs/heads/main')
        self.assertEqual(gh.call_count, 1)
