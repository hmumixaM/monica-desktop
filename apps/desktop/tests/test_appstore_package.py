import importlib.util
import plistlib
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts/package_appstore.py'
spec = importlib.util.spec_from_file_location('package_appstore', SCRIPT)
package_appstore = importlib.util.module_from_spec(spec)
spec.loader.exec_module(package_appstore)


class FrameworkLayoutTests(unittest.TestCase):
    def test_flattened_framework_restores_apple_bundle_layout(self):
        with tempfile.TemporaryDirectory() as folder:
            app = Path(folder) / 'Example.app'
            framework = app / 'Contents/Resources/Example.framework'
            version = framework / 'Versions/151.0'
            (version / 'Resources').mkdir(parents=True)
            (version / 'Helpers').mkdir()
            (version / 'Libraries').mkdir()
            (version / 'Example').write_bytes(b'framework binary')
            (framework / 'Example').write_bytes(b'flattened duplicate')
            (version / 'Resources/Info.plist').write_bytes(plistlib.dumps({
                'CFBundleExecutable': 'Example', 'CFBundlePackageType': 'FMWK'}))
            (framework / '_CodeSignature').mkdir()
            (version / '_CodeSignature').mkdir()
            self.assertEqual(package_appstore.restore_framework_links(app), 5)
            self.assertEqual((framework / 'Versions/Current').readlink(), Path('151.0'))
            for name in ('Example', 'Helpers', 'Libraries', 'Resources'):
                self.assertEqual((framework / name).readlink(), Path('Versions/Current') / name)
                self.assertTrue((framework / name).exists())
            self.assertFalse((framework / '_CodeSignature').exists())
            self.assertFalse((version / '_CodeSignature').exists())
            self.assertEqual(package_appstore.restore_framework_links(app), 0)

    def test_ambiguous_framework_versions_are_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            app = Path(folder) / 'Example.app'
            versions = app / 'Example.framework/Versions'
            (versions / 'A').mkdir(parents=True)
            (versions / 'B').mkdir()
            with self.assertRaisesRegex(ValueError, 'one canonical framework version'):
                package_appstore.restore_framework_links(app)
