"""Fail on unmanageable repository files without penalizing legacy Python debt."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
SOURCE_FILE_LIMIT = 60 * 1024
NON_SOURCE_FILE_LIMIT = 64 * 1024
SOURCE_SUFFIXES = {
    ".c",
    ".cc",
    ".cpp",
    ".css",
    ".go",
    ".h",
    ".html",
    ".java",
    ".js",
    ".jsx",
    ".mjs",
    ".py",
    ".rs",
    ".sh",
    ".sql",
    ".ts",
    ".tsx",
}
SOURCE_PATHS_WITHOUT_SUFFIX = {"scripts/quality"}
EXCLUDED_PREFIXES: set[str] = set()
# Blob identity lets an unchanged legacy baseline survive unrelated main commits. Once the
# reflow lands, its new blob is governed by the ordinary no-growth rule.
ONE_TIME_PYTHON_REFLOW_ALLOWANCES = {
    ("a5a93e04f17361dc0f99d5d8bcac7655fb57f243", "src/switchstand/engine.py"): 589,
}


def git(*args: str, check: bool = True) -> bytes:
    return subprocess.run(
        ["git", *args], cwd=ROOT, check=check, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
    ).stdout


def base_revision() -> str:
    requested = os.environ.get("QUALITY_BASE_REF")
    if requested:
        return git("rev-parse", "--verify", f"{requested}^{{commit}}").decode().strip()
    for candidate in ("main", "origin/main"):
        resolved = git("rev-parse", "--verify", f"{candidate}^{{commit}}", check=False)
        if resolved:
            return resolved.decode().strip()
    return git("rev-parse", "HEAD").decode().strip()


def nonblank_lines(data: bytes, path: str) -> int:
    try:
        return sum(bool(line.strip()) for line in data.decode("utf-8").splitlines())
    except UnicodeDecodeError as exc:
        raise ValueError(f"Python file is not UTF-8: {path}") from exc


def current_paths() -> list[str]:
    output = git("ls-files", "--cached", "--others", "--exclude-standard", "-z")
    return sorted(item.decode("utf-8") for item in output.split(b"\0") if item)


def is_source_file(relative: str) -> bool:
    return relative in SOURCE_PATHS_WITHOUT_SUFFIX or Path(relative).suffix.lower() in SOURCE_SUFFIXES


def is_excluded_path(relative: str) -> bool:
    return any(relative == prefix or relative.startswith(f"{prefix}/") for prefix in EXCLUDED_PREFIXES)


def legacy_line_limit(base_blob: str, relative: str, base_lines: int) -> int:
    return max(base_lines, ONE_TIME_PYTHON_REFLOW_ALLOWANCES.get((base_blob, relative), base_lines))


def main() -> int:
    base = base_revision()
    changed = git("diff", "--name-status", "-z", "-M", "-C", base, "--").split(b"\0")
    renamed: dict[str, str] = {}
    index = 0
    while index < len(changed) and changed[index]:
        status = changed[index].decode("ascii")
        index += 1
        if status.startswith(("R", "C")):
            old, new = changed[index].decode(), changed[index + 1].decode()
            index += 2
            if status.startswith("R"):
                renamed[new] = old
        else:
            index += 1

    failures: list[str] = []
    for relative in current_paths():
        if is_excluded_path(relative):
            continue
        path = ROOT / relative
        size = path.lstat().st_size
        source = is_source_file(relative)
        limit = SOURCE_FILE_LIMIT if source else NON_SOURCE_FILE_LIMIT
        kind = "source" if source else "non-source"
        if size > limit:
            failures.append(f"{kind} file exceeds {limit} bytes: {relative} ({size})")
        if path.suffix != ".py":
            continue
        head_lines = nonblank_lines(path.read_bytes(), relative)
        predecessor = renamed.get(relative, relative)
        exists = subprocess.run(
            ["git", "cat-file", "-e", f"{base}:{predecessor}"],
            cwd=ROOT,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        ).returncode == 0
        base_data = git("show", f"{base}:{predecessor}") if exists else None
        base_blob = git("rev-parse", f"{base}:{predecessor}").decode().strip() if exists else None
        base_lines = nonblank_lines(base_data, predecessor) if base_data is not None else None
        if base_lines is None and head_lines > 500:
            failures.append(f"new Python file exceeds 500 nonblank lines: {relative} ({head_lines})")
        elif base_lines is not None and base_lines <= 500 < head_lines:
            failures.append(f"Python file crossed 500 nonblank lines: {relative} ({head_lines})")
        elif base_lines is not None and base_lines > 500:
            assert base_blob is not None
            allowed_lines = legacy_line_limit(base_blob, relative, base_lines)
            if head_lines > allowed_lines:
                failures.append(f"legacy Python file grew: {relative} ({allowed_lines} -> {head_lines})")
    for failure in failures:
        print(f"ERROR: {failure}", file=sys.stderr)
    return bool(failures)


if __name__ == "__main__":
    raise SystemExit(main())
