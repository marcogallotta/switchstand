"""Enforce a physical character limit for human-maintained repository text."""
from __future__ import annotations

from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
MAX_CHARACTERS = 120
CHECKED_ROOT_FILES = {
    ".gitignore",
    ".jscpd.json",
    "AGENTS.md",
    "LICENSE",
    "README.md",
    "package.json",
    "playwright.config.js",
    "pyproject.toml",
}
CHECKED_EXTENSIONLESS_FILES = {"scripts/quality"}
EXCLUDED_ROOT_FILES = {"package-lock.json", "requirements-dev.lock"}
CHECKED_SUFFIXES = {
    ".c",
    ".cc",
    ".cpp",
    ".css",
    ".go",
    ".h",
    ".html",
    ".java",
    ".js",
    ".json",
    ".jsx",
    ".md",
    ".mjs",
    ".py",
    ".rs",
    ".sh",
    ".sql",
    ".toml",
    ".ts",
    ".tsx",
    ".yaml",
    ".yml",
}
EXCLUDED_PARTS = {"fixtures"}


def current_paths() -> list[str]:
    output = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=ROOT,
        check=True,
        stdout=subprocess.PIPE,
    ).stdout
    return sorted(item.decode("utf-8") for item in output.split(b"\0") if item)


def is_checked_text(relative: str) -> bool:
    path = Path(relative)
    if any(part in EXCLUDED_PARTS for part in path.parts):
        return False
    if len(path.parts) == 1 and relative in EXCLUDED_ROOT_FILES:
        return False
    if relative in CHECKED_ROOT_FILES or relative in CHECKED_EXTENSIONLESS_FILES:
        return True
    return path.suffix.lower() in CHECKED_SUFFIXES


def main() -> int:
    failures: list[str] = []
    for relative in current_paths():
        if not is_checked_text(relative):
            continue
        path = ROOT / relative
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except UnicodeDecodeError:
            failures.append(f"maintained text is not UTF-8: {relative}")
            continue
        for line_number, line in enumerate(lines, start=1):
            if len(line) > MAX_CHARACTERS:
                failures.append(
                    f"line exceeds {MAX_CHARACTERS} characters: {relative}:{line_number} ({len(line)})"
                )
    for failure in failures:
        print(f"ERROR: {failure}", file=sys.stderr)
    return bool(failures)


if __name__ == "__main__":
    raise SystemExit(main())
