from __future__ import annotations

from contextlib import redirect_stderr
import importlib.util
import io
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "check_line_length", ROOT / "scripts" / "check_line_length.py"
)
assert SPEC and SPEC.loader
check_line_length = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(check_line_length)


class LineLengthGateTests(unittest.TestCase):
    def _repo(self) -> tuple[tempfile.TemporaryDirectory[str], Path]:
        temporary = tempfile.TemporaryDirectory()
        root = Path(temporary.name)
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=root, check=True)
        return temporary, root

    def test_rejects_long_maintained_lines_and_accepts_exact_limit(self):
        temporary, root = self._repo()
        self.addCleanup(temporary.cleanup)
        (root / "src").mkdir()
        (root / "docs").mkdir()
        (root / "src" / "exact.js").write_text("x" * 120 + "\n", encoding="utf-8")
        (root / "docs" / "long.md").write_text("x" * 121 + "\n", encoding="utf-8")
        (root / "app.js").write_text("x " * 61 + "\n", encoding="utf-8")
        stderr = io.StringIO()
        with patch.object(check_line_length, "ROOT", root), redirect_stderr(stderr):
            self.assertEqual(check_line_length.main(), 1)
        self.assertIn("docs/long.md:1 (121)", stderr.getvalue())
        self.assertIn("app.js:1 (122)", stderr.getvalue())
        self.assertNotIn("src/exact.js", stderr.getvalue())

    def test_classification_excludes_generated_data(self):
        checked = (
            "src/app.py",
            "app.js",
            "tools/app.js",
            "tests/app.test.js",
            "scripts/check.sh",
            "scripts/quality",
            "src/schema.sql",
            "docs/guide.md",
            "package.json",
        )
        excluded = (
            "package-lock.json",
            "requirements-dev.lock",
            "tests/fixtures/data.json",
        )
        for relative in checked:
            with self.subTest(relative=relative):
                self.assertTrue(check_line_length.is_checked_text(relative))
        for relative in excluded:
            with self.subTest(relative=relative):
                self.assertFalse(check_line_length.is_checked_text(relative))

    def test_rejects_non_utf8_maintained_text(self):
        temporary, root = self._repo()
        self.addCleanup(temporary.cleanup)
        (root / "scripts").mkdir()
        (root / "scripts" / "broken.py").write_bytes(b"\xff")
        stderr = io.StringIO()
        with patch.object(check_line_length, "ROOT", root), redirect_stderr(stderr):
            self.assertEqual(check_line_length.main(), 1)
        self.assertIn("maintained text is not UTF-8", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
