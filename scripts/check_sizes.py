"""Fail on unmanageable repository files without penalizing legacy Python debt."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]


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
        path = ROOT / relative
        size = path.lstat().st_size
        if size > 200_000:
            failures.append(f"file exceeds 200000 bytes: {relative} ({size})")
        elif size > 100_000:
            print(f"WARNING: file exceeds 100000 bytes: {relative} ({size})", file=sys.stderr)
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
        base_lines = nonblank_lines(base_data, predecessor) if base_data is not None else None
        if base_lines is None and head_lines > 500:
            failures.append(f"new Python file exceeds 500 nonblank lines: {relative} ({head_lines})")
        elif base_lines is not None and base_lines <= 500 < head_lines:
            failures.append(f"Python file crossed 500 nonblank lines: {relative} ({head_lines})")
        elif base_lines is not None and base_lines > 500 and head_lines > base_lines:
            failures.append(f"legacy Python file grew: {relative} ({base_lines} -> {head_lines})")
    for failure in failures:
        print(f"ERROR: {failure}", file=sys.stderr)
    return bool(failures)


if __name__ == "__main__":
    raise SystemExit(main())
