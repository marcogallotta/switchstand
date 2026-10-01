"""Conservative, repository-owned affected-test planning."""

from __future__ import annotations

import argparse
import ast
import fnmatch
import hashlib
import json
import re
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from typing import Literal

PLANNER_REVISION = "affected-tests-v1"
FULL_FALLBACK_PATTERNS = (
    "pyproject.toml",
    "uv.lock",
    "alembic.ini",
    "alembic/**",
    "migrations/**",
    "Dockerfile*",
    "compose*.y*ml",
    ".github/workflows/**",
    "scripts/check",
    "scripts/bootstrap",
    "scripts/switchstand",
    "scripts/switchstand-*",
    "tests/conftest.py",
    "docs/assets/**",
)
EXPLICIT_RULES: tuple[tuple[str, tuple[str, ...] | None], ...] = (
    ("src/switchstand/provider.py", ("tests/test_provider.py", "tests/test_relations_provider.py")),
    ("src/switchstand/*_runtime.py", None),
    ("src/switchstand/chatgpt_edge.py", None),
    ("src/switchstand/mcp.py", None),
)
_SHA = re.compile(r"[0-9a-fA-F]{40}\Z")


@dataclass(frozen=True)
class Plan:
    mode: Literal["SELECTED", "FULL_FALLBACK", "NO_PLAN"]
    basis_kind: Literal["exact", "local"]
    base: str
    head: str
    changed_paths: tuple[str, ...]
    selected_tests: tuple[str, ...]
    reasons_by_test: dict[str, tuple[str, ...]]
    fallback_reasons: tuple[str, ...]
    planner_revision: str = PLANNER_REVISION


def _git(repo: Path, *args: str) -> bytes:
    result = subprocess.run(
        ["git", "-c", "core.hooksPath=/dev/null", *args],
        cwd=repo,
        capture_output=True,
        check=False,
    )
    if result.returncode:
        detail = (result.stderr or result.stdout).decode(errors="replace").strip()
        raise RuntimeError(detail or f"git {' '.join(args)} failed")
    return result.stdout


def _exact_sha(repo: Path, value: str) -> str:
    if not _SHA.fullmatch(value):
        raise RuntimeError("exact mode requires full 40-character Git identities")
    resolved = _git(repo, "rev-parse", f"{value}^{{commit}}").decode().strip()
    if resolved.lower() != value.lower():
        raise RuntimeError(f"Git identity did not resolve exactly: {value}")
    return resolved


def _changed(repo: Path, base: str, head: str | None) -> tuple[tuple[str, ...], bool]:
    args = ["diff", "--name-status", "-z", "--find-renames", base]
    if head is not None:
        args.append(head)
    values = _git(repo, *args).decode(errors="surrogateescape").split("\0")
    paths: list[str] = []
    destructive = False
    index = 0
    while index < len(values) and values[index]:
        status = values[index]
        index += 1
        count = 2 if status.startswith(("R", "C")) else 1
        names = values[index : index + count]
        index += count
        paths.append(names[-1])
        destructive |= status.startswith(("D", "R"))
    return tuple(sorted(set(paths))), destructive


def _module(path: str) -> str | None:
    pure = PurePosixPath(path)
    if pure.suffix != ".py" or pure.parts[0] not in {"src", "tests"}:
        return None
    parts = list(pure.with_suffix("").parts)
    if parts[0] == "src":
        parts.pop(0)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _imports(path: str, source: str) -> set[str]:
    module = _module(path)
    if module is None:
        return set()
    package = module.split(".")[:-1]
    found: set[str] = set()
    for node in ast.walk(ast.parse(source, filename=path)):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            prefix = package[: max(0, len(package) - node.level + 1)] if node.level else []
            target = ".".join([*prefix, *(node.module or "").split(".")]).strip(".")
            if target:
                found.add(target)
            for alias in node.names:
                if alias.name != "*" and target:
                    found.add(f"{target}.{alias.name}")
        elif isinstance(node, ast.Call):
            dynamic = (
                isinstance(node.func, ast.Name) and node.func.id in {"__import__", "import_module"}
            ) or (isinstance(node.func, ast.Attribute) and node.func.attr == "import_module")
            if dynamic:
                if (
                    not node.args
                    or not isinstance(node.args[0], ast.Constant)
                    or not isinstance(node.args[0].value, str)
                ):
                    raise ValueError("dynamic import target is not statically known")
                found.add(node.args[0].value)
    return found


def _plan(
    *,
    basis_kind: Literal["exact", "local"],
    base: str,
    head: str,
    changed: tuple[str, ...],
    destructive: bool,
    files: tuple[str, ...],
    read: Callable[[str], str],
) -> Plan:
    tests = tuple(path for path in files if path.startswith("tests/test_") and path.endswith(".py"))
    fallback: list[str] = []
    if destructive:
        fallback.append("deleted or renamed Python/path input")
    for path in changed:
        if any(fnmatch.fnmatchcase(path, pattern) for pattern in FULL_FALLBACK_PATTERNS):
            fallback.append(f"fallback-class:{path}")
        if path.startswith(("src/", "tests/")) and not path.endswith(".py"):
            fallback.append(f"unknown behavioral non-Python input:{path}")
    modules: dict[str, str] = {}
    dependencies: dict[str, set[str]] = {}
    try:
        for path in files:
            name = _module(path)
            if name is not None:
                modules[name] = path
                dependencies[path] = _imports(path, read(path))
    except (RuntimeError, OSError, SyntaxError, UnicodeError, ValueError) as error:
        fallback.append(f"unreadable dependency graph:{type(error).__name__}")

    reasons: dict[str, set[str]] = {}

    def select(test: str, reason: str) -> None:
        if test in tests:
            reasons.setdefault(test, set()).add(reason)

    for path in changed:
        if path in tests:
            select(path, f"changed test:{path}")
        rules = [rule for pattern, rule in EXPLICIT_RULES if fnmatch.fnmatchcase(path, pattern)]
        if len(rules) > 1:
            fallback.append(f"conflicting explicit rules:{path}")
        elif rules == [None]:
            fallback.append(f"explicit fallback:{path}")
        elif rules:
            for test in rules[0] or ():
                select(test, f"explicit rule:{path}")

    changed_modules = {name for path in changed if (name := _module(path)) is not None}
    for test in tests:
        pending = [test]
        seen: set[str] = set()
        while pending:
            current = pending.pop()
            if current in seen:
                continue
            seen.add(current)
            for dependency in dependencies.get(current, set()):
                if dependency in changed_modules:
                    select(test, f"imports changed module:{dependency}")
                if dependency in modules:
                    pending.append(modules[dependency])
    for path in changed:
        if path.startswith("src/switchstand/") and path.endswith(".py"):
            same_name = f"tests/test_{PurePosixPath(path).stem}.py"
            select(same_name, f"same-name edge:{path}")
    for path in changed:
        if path.endswith(".py") and path.startswith(("src/", "tests/")):
            name = _module(path)
            covered = path in tests or any(
                any(
                    reason in {f"imports changed module:{name}", f"explicit rule:{path}"}
                    for reason in entries
                )
                for entries in reasons.values()
            )
            if not covered:
                fallback.append(f"changed Python has no resolved tests:{path}")

    selected = tuple(sorted(reasons))
    if fallback or not selected:
        if not fallback:
            fallback.append("no safe selected set")
        mode: Literal["SELECTED", "FULL_FALLBACK", "NO_PLAN"] = (
            "FULL_FALLBACK" if tests else "NO_PLAN"
        )
        selected = tests if mode == "FULL_FALLBACK" else ()
    else:
        mode = "SELECTED"
    return Plan(
        mode,
        basis_kind,
        base,
        head,
        changed,
        selected,
        {test: tuple(sorted(entries)) for test, entries in sorted(reasons.items())},
        tuple(sorted(set(fallback))),
    )


def plan_exact(repo: Path, base: str, head: str) -> Plan:
    try:
        base_sha = _exact_sha(repo, base)
        head_sha = _exact_sha(repo, head)
        files = tuple(
            path
            for path in _git(repo, "ls-tree", "-r", "--name-only", "-z", head_sha)
            .decode(errors="surrogateescape")
            .split("\0")
            if path
        )
    except RuntimeError:
        return Plan("NO_PLAN", "exact", base, head, (), (), {}, ("exact basis unavailable",))
    try:
        changed, destructive = _changed(repo, base_sha, head_sha)
    except RuntimeError:
        changed, destructive = (), False
        tests = tuple(
            path for path in files if path.startswith("tests/test_") and path.endswith(".py")
        )
        return Plan(
            "FULL_FALLBACK",
            "exact",
            base_sha,
            head_sha,
            changed,
            tests,
            {},
            ("exact diff unavailable",),
        )
    return _plan(
        basis_kind="exact",
        base=base_sha,
        head=head_sha,
        changed=changed,
        destructive=destructive,
        files=files,
        read=lambda path: _git(repo, "show", f"{head_sha}:{path}").decode(),
    )


def plan_local(repo: Path, base: str | None = None) -> Plan:
    current = _git(repo, "rev-parse", "HEAD").decode().strip()
    base_sha = _exact_sha(repo, base) if base else current
    changed, destructive = _changed(repo, base_sha, None)
    untracked = tuple(
        path
        for path in _git(repo, "ls-files", "--others", "--exclude-standard", "-z")
        .decode(errors="surrogateescape")
        .split("\0")
        if path
    )
    changed = tuple(sorted({*changed, *untracked}))
    files = tuple(
        path
        for path in _git(repo, "ls-files", "-co", "--exclude-standard", "-z")
        .decode(errors="surrogateescape")
        .split("\0")
        if path
    )
    digest = hashlib.sha256(_git(repo, "diff", "--binary", base_sha))
    for path in untracked:
        digest.update(path.encode())
        digest.update((repo / path).read_bytes())
    identity = f"worktree:{current}:{digest.hexdigest()}"
    return _plan(
        basis_kind="local",
        base=base_sha,
        head=identity,
        changed=changed,
        destructive=destructive,
        files=files,
        read=lambda path: (repo / path).read_text(),
    )


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--base")
    parser.add_argument("--head")
    parser.add_argument("--local", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    if args.local:
        if args.head:
            parser.error("--local does not accept --head")
        plan = plan_local(args.repo.resolve(), args.base)
    else:
        if not args.base or not args.head:
            parser.error("exact mode requires --base and --head")
        plan = plan_exact(args.repo.resolve(), args.base, args.head)
    if args.json:
        print(json.dumps(asdict(plan), sort_keys=True, separators=(",", ":")))
    else:
        print(f"{plan.mode} {plan.basis_kind} {plan.base}..{plan.head}")
        for test in plan.selected_tests:
            print(test)
        for reason in plan.fallback_reasons:
            print(f"fallback: {reason}")
