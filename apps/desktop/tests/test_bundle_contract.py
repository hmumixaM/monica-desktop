import hashlib
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

spec = importlib.util.spec_from_file_location("verify_runtime", Path(__file__).parents[1] / "scripts/verify_runtime.py")
verifier = importlib.util.module_from_spec(spec)
spec.loader.exec_module(verifier)


class BundleContractTests(unittest.TestCase):
    def test_missing_crawler_or_external_symlink_fails_packaging(self):
        with tempfile.TemporaryDirectory() as folder, tempfile.TemporaryDirectory() as other:
            root = Path(folder)
            (root / "capabilities.json").write_text(json.dumps({"capabilities":{"instagram":["crawler.py"]}}))
            with self.assertRaisesRegex(ValueError, "Missing or external"):
                verifier.verify_files(root)
            outside = Path(other) / "outside.py"
            outside.write_text("not bundled")
            (root / "crawler.py").symlink_to(outside)
            with self.assertRaisesRegex(ValueError, "Missing or external"):
                verifier.verify_files(root)

    def test_adapted_source_pin_is_checked_and_modified_collector_is_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "collectors").mkdir()
            script = root / "collectors/crawler.py"
            script.write_text("bundled adaptation")
            (root / "capabilities.json").write_text(json.dumps({"capabilities":{"crawler":["collectors/crawler.py"]}}))
            (root / "collectors/VENDORED_SOURCES.json").write_text(json.dumps({"sources":[{
                "file":"crawler.py", "sha256":"original source hash", "bundled_sha256":hashlib.sha256(script.read_bytes()).hexdigest()}]}))
            self.assertEqual(verifier.verify_files(root)[1], 1)
            script.write_text("modified after review")
            with self.assertRaisesRegex(ValueError, "hash mismatch"):
                verifier.verify_files(root)
