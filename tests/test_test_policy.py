from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from scripts.check_test_policy import scan


class TestPolicyTests(unittest.TestCase):
    def test_ordinary_tests_and_zero_retries_pass(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            files = {
                "test_ok.py": "import unittest\nclass T(unittest.TestCase):\n def test_ok(self): self.assertTrue(True)\n",
                "ok.test.js": 'test("works", () => {});\n',
                "playwright.config.js": "module.exports = { retries: 0 };\n",
            }
            paths = []
            for name, content in files.items():
                path = root / name
                path.write_text(content, encoding="utf-8")
                paths.append(path)
            self.assertEqual(scan(paths), [])

    def test_skip_focus_and_retry_forms_fail(self):
        cases = {
            "skip.py": "@unittest.skip('later')\ndef test_later(): pass\n",
            "skip_test.py": "def test_later(self): self.skipTest('later')\n",
            "expected.py": "@unittest.expectedFailure\ndef test_later(): pass\n",
            "only.test.js": 'test.only("focused", () => {});\n',
            "fixme.spec.js": 'test.fixme("disabled", () => {});\n',
            "retry.js": "module.exports = { retries: 2 };\n",
            "rerun.yml": "run: pytest --reruns=2\n",
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name, content in cases.items():
                with self.subTest(name=name):
                    path = root / name
                    path.write_text(content, encoding="utf-8")
                    self.assertTrue(scan([path]))


if __name__ == "__main__":
    unittest.main()
