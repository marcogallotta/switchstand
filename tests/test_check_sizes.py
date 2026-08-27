from __future__ import annotations

from contextlib import redirect_stderr
import importlib.util
import io
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("check_sizes", ROOT / "scripts" / "check_sizes.py")
assert SPEC and SPEC.loader
check_sizes = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(check_sizes)


class QualityGateTests(unittest.TestCase):
    def _repo(self) -> tuple[tempfile.TemporaryDirectory[str], Path, str]:
        temporary = tempfile.TemporaryDirectory()
        root = Path(temporary.name)
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=root, check=True)
        subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=root, check=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=root, check=True)
        (root / ".gitignore").write_text("ignored/\n", encoding="utf-8")
        engine = root / "src" / "switchstand" / "engine.py"
        engine.parent.mkdir(parents=True)
        engine.write_text("line = 1\n" * 501, encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=root, check=True)
        subprocess.run(["git", "commit", "-qm", "base"], cwd=root, check=True)
        base = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=root, check=True, text=True, capture_output=True
        ).stdout.strip()
        return temporary, root, base

    def test_pinned_analyzers_reject_known_violations(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
            fixture = Path(directory)
            bad_python = fixture / "bad.py"
            bad_python.write_text('value: int = "wrong"\nprint(missing_name)\n', encoding="utf-8")
            ruff = subprocess.run(
                [sys.executable, "-m", "ruff", "check", "--select", "E9,F,B", bad_python],
                capture_output=True,
            )
            pyright = subprocess.run(
                [str(ROOT / "node_modules/.bin/pyright"), bad_python], capture_output=True
            )
            duplicate = "\n".join(f"value_{i} = ({i} + 1) * ({i} + 2)" for i in range(20)) + "\n"
            (fixture / "copy_a.py").write_text(duplicate, encoding="utf-8")
            (fixture / "copy_b.py").write_text(duplicate, encoding="utf-8")
            jscpd = subprocess.run(
                [
                    str(ROOT / "node_modules/.bin/jscpd"),
                    fixture,
                    "--min-lines", "10",
                    "--min-tokens", "80",
                    "--mode", "mild",
                    "--threshold", "0",
                    "--silent",
                    "--noTips",
                ],
                capture_output=True,
            )
        self.assertEqual(ruff.returncode, 1)
        self.assertIn(b"F821", ruff.stdout)
        self.assertEqual(pyright.returncode, 1)
        self.assertIn(b"reportAssignmentType", pyright.stdout)
        self.assertIn(b"reportUndefinedVariable", pyright.stdout)
        self.assertEqual(jscpd.returncode, 1)
        self.assertIn(b"exact clones", jscpd.stdout)
        self.assertIn(b"threshold (0%)", jscpd.stderr)

    def test_python_ratchet_rejects_new_501_lines_and_legacy_growth(self):
        temporary, root, base = self._repo()
        self.addCleanup(temporary.cleanup)
        (root / "new.py").write_text("line = 1\n" * 501, encoding="utf-8")
        engine = root / "src" / "switchstand" / "engine.py"
        engine.write_text(engine.read_text(encoding="utf-8") + "line = 2\n", encoding="utf-8")
        stderr = io.StringIO()
        with patch.object(check_sizes, "ROOT", root), patch.dict(os.environ, {"QUALITY_BASE_REF": base}):
            with redirect_stderr(stderr):
                result = check_sizes.main()
        self.assertEqual(result, 1)
        self.assertIn("new Python file exceeds 500", stderr.getvalue())
        self.assertIn("legacy Python file grew", stderr.getvalue())

    def test_byte_threshold_warns_then_fails(self):
        temporary, root, base = self._repo()
        self.addCleanup(temporary.cleanup)
        artifact = root / "artifact.bin"
        stderr = io.StringIO()
        artifact.write_bytes(b"x" * 100_001)
        with patch.object(check_sizes, "ROOT", root), patch.dict(os.environ, {"QUALITY_BASE_REF": base}):
            with redirect_stderr(stderr):
                self.assertEqual(check_sizes.main(), 0)
                artifact.write_bytes(b"x" * 200_001)
                self.assertEqual(check_sizes.main(), 1)
        self.assertIn("WARNING: file exceeds 100000 bytes", stderr.getvalue())
        self.assertIn("ERROR: file exceeds 200000 bytes", stderr.getvalue())

    def test_ignored_untracked_output_is_excluded(self):
        temporary, root, base = self._repo()
        self.addCleanup(temporary.cleanup)
        ignored = root / "ignored" / "generated.py"
        ignored.parent.mkdir()
        ignored.write_text("line = 1\n" * 25_000, encoding="utf-8")
        with patch.object(check_sizes, "ROOT", root), patch.dict(os.environ, {"QUALITY_BASE_REF": base}):
            self.assertEqual(check_sizes.main(), 0)


if __name__ == "__main__":
    unittest.main()
