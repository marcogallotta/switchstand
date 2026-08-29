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
        legacy = root / "src" / "switchstand" / "legacy.py"
        legacy.parent.mkdir(parents=True)
        legacy.write_text("line = 1\n" * 501, encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=root, check=True)
        subprocess.run(["git", "commit", "-qm", "base"], cwd=root, check=True)
        base = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=root, check=True, text=True, capture_output=True
        ).stdout.strip()
        return temporary, root, base

    def _engine_repo(self) -> tuple[tempfile.TemporaryDirectory[str], Path, str, str, Path]:
        temporary, root, _ = self._repo()
        engine = root / "src" / "switchstand" / "engine.py"
        engine.write_text("line = 1\n" * 557, encoding="utf-8")
        subprocess.run(["git", "add", str(engine)], cwd=root, check=True)
        subprocess.run(["git", "commit", "-qm", "legacy engine"], cwd=root, check=True)
        (root / "unrelated.md").write_text("unrelated main advance\n", encoding="utf-8")
        subprocess.run(["git", "add", "unrelated.md"], cwd=root, check=True)
        subprocess.run(["git", "commit", "-qm", "unrelated main advance"], cwd=root, check=True)
        base = self._revision(root, "HEAD")
        blob = self._revision(root, "HEAD:src/switchstand/engine.py")
        return temporary, root, base, blob, engine

    @staticmethod
    def _revision(root: Path, revision: str) -> str:
        return subprocess.run(
            ["git", "rev-parse", revision],
            cwd=root,
            check=True,
            text=True,
            capture_output=True,
        ).stdout.strip()

    def _size_result(
        self, root: Path, base: str, allowances: dict[tuple[str, str], int]
    ) -> tuple[int, str]:
        stderr = io.StringIO()
        with (
            patch.object(check_sizes, "ROOT", root),
            patch.dict(os.environ, {"QUALITY_BASE_REF": base}),
            patch.dict(check_sizes.ONE_TIME_PYTHON_REFLOW_ALLOWANCES, allowances, clear=True),
            redirect_stderr(stderr),
        ):
            result = check_sizes.main()
        return result, stderr.getvalue()

    def test_pinned_analyzers_reject_known_violations(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
            fixture = Path(directory)
            bad_python = fixture / "bad.py"
            bad_python.write_text(
                'value: int = "wrong"\nprint(missing_name)\nlong_value = '
                + " + ".join("1" for _ in range(50))
                + "\n",
                encoding="utf-8",
            )
            ruff = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "ruff",
                    "check",
                    "--select",
                    "E9,E501,F,B",
                    "--line-length",
                    "120",
                    bad_python,
                ],
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
        self.assertIn(b"E501", ruff.stdout)
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
        legacy = root / "src" / "switchstand" / "legacy.py"
        legacy.write_text(legacy.read_text(encoding="utf-8") + "line = 2\n", encoding="utf-8")
        stderr = io.StringIO()
        with patch.object(check_sizes, "ROOT", root), patch.dict(os.environ, {"QUALITY_BASE_REF": base}):
            with redirect_stderr(stderr):
                result = check_sizes.main()
        self.assertEqual(result, 1)
        self.assertIn("new Python file exceeds 500", stderr.getvalue())
        self.assertIn("legacy Python file grew", stderr.getvalue())

    def test_source_and_non_source_byte_limits(self):
        temporary, root, base = self._repo()
        self.addCleanup(temporary.cleanup)
        source = root / "app.js"
        artifact = root / "artifact.bin"
        stderr = io.StringIO()
        source.write_bytes(b"x" * check_sizes.SOURCE_FILE_LIMIT)
        artifact.write_bytes(b"x" * check_sizes.NON_SOURCE_FILE_LIMIT)
        with patch.object(check_sizes, "ROOT", root), patch.dict(os.environ, {"QUALITY_BASE_REF": base}):
            with redirect_stderr(stderr):
                self.assertEqual(check_sizes.main(), 0)
                source.write_bytes(b"x" * (check_sizes.SOURCE_FILE_LIMIT + 1))
                artifact.write_bytes(b"x" * (check_sizes.NON_SOURCE_FILE_LIMIT + 1))
                self.assertEqual(check_sizes.main(), 1)
        self.assertIn("source file exceeds 61440 bytes: app.js (61441)", stderr.getvalue())
        self.assertIn("non-source file exceeds 65536 bytes: artifact.bin (65537)", stderr.getvalue())

    def test_source_classification_is_explicit(self):
        for relative in ("src/app.py", "tests/app.test.js", "scripts/quality", "schema.sql"):
            with self.subTest(relative=relative):
                self.assertTrue(check_sizes.is_source_file(relative))
        for relative in ("README.md", "package-lock.json", "tests/fixtures/data.json"):
            with self.subTest(relative=relative):
                self.assertFalse(check_sizes.is_source_file(relative))

    def test_blob_allowance_accepts_unchanged_legacy_baseline(self):
        temporary, root, base, blob, engine = self._engine_repo()
        self.addCleanup(temporary.cleanup)
        path = "src/switchstand/engine.py"
        engine.write_text("line = 1\n" * 589, encoding="utf-8")
        result, stderr = self._size_result(root, base, {(blob, path): 589})
        self.assertEqual(result, 0, stderr)

    def test_blob_allowance_rejects_changed_or_overgrown_legacy_source(self):
        path = "src/switchstand/engine.py"
        with self.subTest(case="changed baseline"):
            temporary, root, _base, blob, engine = self._engine_repo()
            self.addCleanup(temporary.cleanup)
            engine.write_text("changed = 1\n" + "line = 1\n" * 556, encoding="utf-8")
            subprocess.run(["git", "add", str(engine)], cwd=root, check=True)
            subprocess.run(["git", "commit", "-qm", "changed baseline"], cwd=root, check=True)
            changed_base = self._revision(root, "HEAD")
            engine.write_text("line = 1\n" * 589, encoding="utf-8")
            result, stderr = self._size_result(root, changed_base, {(blob, path): 589})
            self.assertEqual(result, 1)
            self.assertIn("legacy Python file grew", stderr)

        with self.subTest(case="over allowance"):
            temporary, root, base, blob, engine = self._engine_repo()
            self.addCleanup(temporary.cleanup)
            engine.write_text("line = 1\n" * 590, encoding="utf-8")
            result, stderr = self._size_result(root, base, {(blob, path): 589})
            self.assertEqual(result, 1)
            self.assertIn("(589 -> 590)", stderr)

    def test_blob_allowance_disappears_after_reflow_lands(self):
        temporary, root, _base, blob, engine = self._engine_repo()
        self.addCleanup(temporary.cleanup)
        path = "src/switchstand/engine.py"
        engine.write_text("line = 1\n" * 589, encoding="utf-8")
        subprocess.run(["git", "add", str(engine)], cwd=root, check=True)
        subprocess.run(["git", "commit", "-qm", "land reflow"], cwd=root, check=True)
        post_reflow_base = self._revision(root, "HEAD")
        engine.write_text(engine.read_text(encoding="utf-8") + "line = 2\n", encoding="utf-8")
        result, stderr = self._size_result(root, post_reflow_base, {(blob, path): 589})
        self.assertEqual(result, 1)
        self.assertIn("(589 -> 590)", stderr)

    def test_separately_owned_gpt_actions_lane_is_excluded(self):
        temporary, root, base = self._repo()
        self.addCleanup(temporary.cleanup)
        action = root / "experiments" / "gpt-actions-github" / "github-action.mjs"
        action.parent.mkdir(parents=True)
        action.write_bytes(b"x" * (check_sizes.SOURCE_FILE_LIMIT + 1))
        with patch.object(check_sizes, "ROOT", root), patch.dict(os.environ, {"QUALITY_BASE_REF": base}):
            self.assertEqual(check_sizes.main(), 0)
        self.assertTrue(check_sizes.is_excluded_path(action.relative_to(root).as_posix()))

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
