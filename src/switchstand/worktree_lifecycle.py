"""Evidence-preserving, operator-invoked retirement of terminal linked worktrees."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import subprocess
from pathlib import Path
from typing import Any

EVIDENCE = {".qualification", ".switchstand-incidents"}
DISPOSABLE = {".venv", ".pytest_cache", ".ruff_cache", ".mypy_cache", "friction.md"}


def _run(*arguments: str, cwd: Path | None = None) -> str:
    return subprocess.run(
        arguments, cwd=cwd, check=True, text=True, stdout=subprocess.PIPE
    ).stdout.strip()


def _git(repo: Path, *arguments: str) -> str:
    return _run("git", "-C", str(repo), *arguments)


def _atomic_json(path: Path, value: object) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def _records(repo: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for block in _git(repo, "worktree", "list", "--porcelain").split("\n\n"):
        fields: dict[str, Any] = {}
        for line in block.splitlines():
            key, _, value = line.partition(" ")
            if key in {"prunable", "locked", "worktree", "HEAD", "branch", "detached"}:
                fields[key] = value or True
        if fields:
            records.append(fields)
    return records


def _inside(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def _manifests(coordinator: Path) -> dict[str, list[tuple[Path, dict[str, Any]]]]:
    result: dict[str, list[tuple[Path, dict[str, Any]]]] = {}
    for path in coordinator.glob("start-commit.*.manifest.json"):
        try:
            value = json.loads(path.read_text())
            candidate = Path(value["session"]["writer"]).resolve(strict=False)
        except (OSError, KeyError, TypeError, json.JSONDecodeError):
            continue
        result.setdefault(str(candidate), []).append((path, value))
    return result


def _manifest(
    coordinator: Path,
    writer: Path,
    manifests: dict[str, list[tuple[Path, dict[str, Any]]]] | None = None,
) -> tuple[Path, dict[str, Any]] | None:
    source = manifests if manifests is not None else _manifests(coordinator)
    matches = source.get(str(writer), [])
    return matches[0] if len(matches) == 1 else None


def _last_marker(manifest_path: Path, coordinator: Path) -> str | None:
    suffix = manifest_path.name.removeprefix("start-commit.").removesuffix(".manifest.json")
    telemetry = coordinator / f"continuity-{suffix}.jsonl"
    try:
        lines = telemetry.read_text().splitlines()
        value = json.loads(lines[-1])
    except (OSError, IndexError, TypeError, json.JSONDecodeError):
        return None
    marker = value.get("yield_marker")
    return marker if isinstance(marker, str) else None


def _ignored(writer: Path) -> tuple[list[Path], list[Path], list[str]]:
    output = _git(
        writer, "status", "--porcelain=v1", "--ignored=matching", "--untracked-files=normal"
    )
    evidence: set[Path] = set()
    disposable: set[Path] = set()
    unknown: set[str] = set()
    for line in output.splitlines():
        if not line.startswith("!! "):
            continue
        relative = line[3:].rstrip("/")
        parts = Path(relative).parts
        if not parts:
            continue
        if parts[0] in EVIDENCE:
            evidence.add(writer / parts[0])
        elif parts[0] in DISPOSABLE or "__pycache__" in parts:
            disposable.add(writer / parts[0])
        else:
            unknown.add(relative)
    return sorted(evidence), sorted(disposable), sorted(unknown)


def _fingerprint(path: Path) -> dict[str, Any]:
    value = path.lstat()
    return {
        "path": str(path),
        "device": value.st_dev,
        "inode": value.st_ino,
        "mode": stat.S_IFMT(value.st_mode),
        "size": value.st_size,
        "mtime_ns": value.st_mtime_ns,
    }


def classify(
    repo: Path,
    root: Path,
    coordinator: Path,
    writer: Path,
    *,
    registrations: list[dict[str, Any]] | None = None,
    manifests: dict[str, list[tuple[Path, dict[str, Any]]]] | None = None,
    main_head: str | None = None,
) -> dict[str, Any]:
    repo, root = repo.resolve(strict=True), root.resolve(strict=True)
    writer = writer.resolve(strict=True)
    reasons: list[str] = []
    registered = next(
        (
            item
            for item in (registrations if registrations is not None else _records(repo))
            if item.get("worktree") == str(writer)
        ),
        None,
    )
    if not _inside(writer, root):
        reasons.append("outside_durable_root")
    if registered is None:
        reasons.append("not_registered")
    manifest_match = _manifest(coordinator, writer, manifests)
    marker = None
    if manifest_match is None:
        reasons.append("ownership_unknown")
    else:
        marker = _last_marker(manifest_match[0], coordinator)
        if marker != "ASSIGNMENT_COMPLETE":
            reasons.append("not_assignment_complete")
        if manifest_match[1].get("handoff"):
            reasons.append("handoff_present")
        if not isinstance(manifest_match[1].get("session", {}).get("generation"), str):
            reasons.append("generation_unknown")
    dirty = bool(_git(writer, "status", "--porcelain=v1", "--untracked-files=normal"))
    if dirty:
        reasons.append("dirty")
    head = _git(writer, "rev-parse", "HEAD")
    main = main_head or _git(repo, "rev-parse", "refs/heads/main")
    reachable = subprocess.run(
        ["git", "-C", str(repo), "merge-base", "--is-ancestor", head, main], check=False
    ).returncode == 0
    if not reachable:
        reasons.append("head_not_in_main")
    evidence, disposable, ignored_unknown = _ignored(writer)
    if ignored_unknown:
        reasons.append("ignored_unknown")
    classification = "RETIREMENT_CANDIDATE" if not reasons else "RETAIN_UNKNOWN"
    git_dir = _git(writer, "rev-parse", "--absolute-git-dir")
    return {
        "writer": str(writer),
        "git_dir": git_dir,
        "head": head,
        "main": main,
        "marker": marker,
        "generation": (
            manifest_match[1]["session"]["generation"] if manifest_match is not None else None
        ),
        "classification": classification,
        "reasons": reasons,
        "evidence": [_fingerprint(path) for path in evidence],
        "disposable": [_fingerprint(path) for path in disposable],
        "ignored_unknown": ignored_unknown,
    }


def _plan_retire(arguments: argparse.Namespace) -> None:
    identity = classify(arguments.repo, arguments.root, arguments.coordinator, arguments.writer)
    if identity["classification"] != "RETIREMENT_CANDIDATE":
        raise SystemExit("writer is not a retirement candidate: " + ", ".join(identity["reasons"]))
    if arguments.terminal_generation != identity["generation"]:
        raise SystemExit("exact terminal generation was not confirmed")
    archive_root = arguments.archive_root.resolve(strict=True)
    if _inside(archive_root, arguments.writer.resolve(strict=True)):
        raise SystemExit("archive root must be outside the writer")
    plan = {
        "schema": 1,
        "kind": "retire",
        "repo": str(arguments.repo.resolve(strict=True)),
        "root": str(arguments.root.resolve(strict=True)),
        "coordinator": str(arguments.coordinator.resolve(strict=True)),
        "archive": str(archive_root / f"{Path(identity['writer']).name}-{identity['head'][:12]}"),
        "identity": identity,
    }
    _atomic_json(arguments.plan, plan)


def _apply_retire(arguments: argparse.Namespace) -> None:
    raw = arguments.plan.read_bytes()
    plan = json.loads(raw)
    if plan.get("schema") != 1 or plan.get("kind") != "retire":
        raise SystemExit("invalid retirement plan")
    expected = plan["identity"]
    actual = classify(
        Path(plan["repo"]), Path(plan["root"]), Path(plan["coordinator"]), Path(expected["writer"])
    )
    if expected != actual:
        raise SystemExit("retirement preimage changed")
    archive = Path(plan["archive"])
    if archive.exists():
        raise SystemExit("evidence archive already exists")
    receipt: dict[str, Any] = {
        "kind": "retire",
        "plan_sha256": hashlib.sha256(raw).hexdigest(),
        "state": "STARTED",
        "writer": expected["writer"],
        "evidence": [],
    }
    _atomic_json(arguments.receipt, receipt)
    archive.mkdir(mode=0o700, parents=True)
    for evidence in expected["evidence"]:
        source = Path(evidence["path"])
        destination = archive / source.name
        os.rename(source, destination)
        receipt["evidence"].append({"source": str(source), "archive": str(destination)})
        _atomic_json(arguments.receipt, receipt)
    _git(Path(plan["repo"]), "worktree", "remove", "--force", expected["writer"])
    remaining = {item.get("worktree") for item in _records(Path(plan["repo"]))}
    if Path(expected["writer"]).exists() or expected["writer"] in remaining:
        raise SystemExit("retirement readback failed; evidence remains preserved")
    receipt["state"] = "COMPLETE"
    _atomic_json(arguments.receipt, receipt)


def _prunable(repo: Path) -> list[str]:
    return sorted(str(item["worktree"]) for item in _records(repo) if "prunable" in item)


def _plan_prune(arguments: argparse.Namespace) -> None:
    plan = {
        "schema": 1,
        "kind": "prune-registrations",
        "repo": str(arguments.repo.resolve(strict=True)),
        "prunable": _prunable(arguments.repo),
    }
    _atomic_json(arguments.plan, plan)


def _apply_prune(arguments: argparse.Namespace) -> None:
    raw = arguments.plan.read_bytes()
    plan = json.loads(raw)
    if plan.get("schema") != 1 or plan.get("kind") != "prune-registrations":
        raise SystemExit("invalid registration-prune plan")
    repo = Path(plan["repo"])
    if _prunable(repo) != plan["prunable"]:
        raise SystemExit("prunable registration set changed")
    _git(repo, "worktree", "prune", "--expire", "now")
    remaining = sorted(set(plan["prunable"]) & set(_prunable(repo)))
    receipt = {
        "kind": "prune-registrations",
        "plan_sha256": hashlib.sha256(raw).hexdigest(),
        "pruned": plan["prunable"],
        "remaining": remaining,
        "state": "COMPLETE" if not remaining else "INCOMPLETE",
    }
    _atomic_json(arguments.receipt, receipt)
    if remaining:
        raise SystemExit("registration-prune readback failed")


def _audit(arguments: argparse.Namespace) -> None:
    rows: list[dict[str, Any]] = []
    root = arguments.root.resolve(strict=True)
    registrations = _records(arguments.repo)
    manifests = _manifests(arguments.coordinator)
    main_head = _git(arguments.repo, "rev-parse", "refs/heads/main")
    for item in registrations:
        raw = item.get("worktree")
        if not isinstance(raw, str):
            continue
        path = Path(raw)
        if path.exists() and _inside(path.resolve(), root):
            rows.append(
                classify(
                    arguments.repo,
                    root,
                    arguments.coordinator,
                    path,
                    registrations=registrations,
                    manifests=manifests,
                    main_head=main_head,
                )
            )
    print(json.dumps({"writers": rows, "prunable": _prunable(arguments.repo)}, sort_keys=True))


def main() -> None:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--repo", type=Path, required=True)
    common.add_argument("--root", type=Path, required=True)
    common.add_argument("--coordinator", type=Path, required=True)
    audit = commands.add_parser("audit", parents=[common])
    audit.set_defaults(run=_audit)
    retire = commands.add_parser("plan-retire", parents=[common])
    retire.add_argument("--writer", type=Path, required=True)
    retire.add_argument("--archive-root", type=Path, required=True)
    retire.add_argument("--terminal-generation", required=True)
    retire.add_argument("--plan", type=Path, required=True)
    retire.set_defaults(run=_plan_retire)
    apply_retire = commands.add_parser("apply-retire")
    apply_retire.add_argument("--plan", type=Path, required=True)
    apply_retire.add_argument("--receipt", type=Path, required=True)
    apply_retire.set_defaults(run=_apply_retire)
    prune = commands.add_parser("plan-prune")
    prune.add_argument("--repo", type=Path, required=True)
    prune.add_argument("--plan", type=Path, required=True)
    prune.set_defaults(run=_plan_prune)
    apply_prune = commands.add_parser("apply-prune")
    apply_prune.add_argument("--plan", type=Path, required=True)
    apply_prune.add_argument("--receipt", type=Path, required=True)
    apply_prune.set_defaults(run=_apply_prune)
    arguments = parser.parse_args()
    arguments.run(arguments)


if __name__ == "__main__":
    main()
