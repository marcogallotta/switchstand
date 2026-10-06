from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from argparse import Namespace
from pathlib import Path

import pytest

from switchstand import worktree_lifecycle as lifecycle


def git(repo: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *arguments],
        check=True,
        text=True,
        stdout=subprocess.PIPE,
    ).stdout.strip()

def fixture(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    repo = tmp_path / "repo"
    root = tmp_path / "writers"
    coordinator = tmp_path / "coordinator"
    archive = tmp_path / "archive"
    repo.mkdir()
    root.mkdir()
    coordinator.mkdir()
    archive.mkdir()
    git(repo, "init", "-b", "main")
    git(repo, "config", "user.email", "test@example.com")
    git(repo, "config", "user.name", "Test")
    (repo / ".gitignore").write_text(
        ".qualification/\n.venv/\n.pytest_cache/\n.ruff_cache/\nfriction.md\n"
    )
    (repo / "tracked").write_text("base\n")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "base")
    return repo, root, coordinator, archive

def terminal_writer(repo: Path, root: Path, coordinator: Path, name: str = "writer") -> Path:
    writer = root / name
    git(repo, "worktree", "add", "-b", name, str(writer), "main")
    (writer / ".qualification").mkdir()
    (writer / ".qualification" / "proof.txt").write_text("evidence\n")
    (writer / ".venv").mkdir()
    (writer / ".venv" / "cache").write_text("discard\n")
    (writer / "friction.md").symlink_to(coordinator / "friction.md")
    suffix = name.upper()
    manifest = {
        "session": {
            "generation": suffix,
            "writer": str(writer),
            "start_record": str(coordinator / f"start-commit.{suffix}"),
        }
    }
    (coordinator / f"start-commit.{suffix}.manifest.json").write_text(json.dumps(manifest))
    (coordinator / f"continuity-{suffix}.jsonl").write_text(
        json.dumps({"event": "stop", "yield_marker": "ASSIGNMENT_COMPLETE"}) + "\n"
    )
    return writer

def plan_args(
    repo: Path,
    root: Path,
    coordinator: Path,
    archive: Path,
    writer: Path,
    plan: Path,
) -> Namespace:
    return Namespace(
        repo=repo,
        root=root,
        coordinator=coordinator,
        archive_root=archive,
        terminal_generation=writer.name.upper(),
        writer=writer,
        plan=plan,
    )

def apply_args(plan: Path, receipt: Path) -> Namespace:
    return Namespace(
        plan=plan, receipt=receipt, plan_sha256=hashlib.sha256(plan.read_bytes()).hexdigest()
    )

def test_terminal_writer_plan_preserves_evidence_and_removes_exact_worktree(tmp_path: Path) -> None:
    repo, root, coordinator, archive = fixture(tmp_path)
    writer = terminal_writer(repo, root, coordinator)
    other = root / "other"
    git(repo, "worktree", "add", "-b", "other", str(other), "main")
    plan, receipt = tmp_path / "plan.json", tmp_path / "receipt.json"
    arguments = plan_args(repo, root, coordinator, archive, writer, plan)
    with pytest.raises(SystemExit, match="terminal generation"):
        lifecycle._plan_retire(Namespace(**(vars(arguments) | {"terminal_generation": "wrong"})))
    lifecycle._plan_retire(arguments)
    with pytest.raises(SystemExit, match="outside the writer"):
        lifecycle._apply_retire(apply_args(plan, writer / "receipt.json"))
    lifecycle._apply_retire(apply_args(plan, receipt))
    result = json.loads(receipt.read_text())
    evidence = Path(result["evidence"][0]["archive"])
    assert result["state"] == "COMPLETE"
    assert (evidence / "proof.txt").read_text() == "evidence\n"
    assert not writer.exists()
    assert other.exists()
    assert str(writer) not in git(repo, "worktree", "list", "--porcelain")

def test_changed_retirement_preimage_has_no_effect(tmp_path: Path) -> None:
    repo, root, coordinator, archive = fixture(tmp_path)
    writer = terminal_writer(repo, root, coordinator)
    plan, receipt = tmp_path / "plan.json", tmp_path / "receipt.json"
    lifecycle._plan_retire(plan_args(repo, root, coordinator, archive, writer, plan))
    expected = apply_args(plan, receipt)
    changed = json.loads(plan.read_text())
    changed["archive"] += "-replacement"
    plan.write_text(json.dumps(changed))
    with pytest.raises(SystemExit, match="plan digest changed"):
        lifecycle._apply_retire(expected)
    lifecycle._plan_retire(plan_args(repo, root, coordinator, archive, writer, plan))
    (writer / ".qualification" / "proof.txt").write_text("changed!\n")
    with pytest.raises(SystemExit, match="preimage changed"):
        lifecycle._apply_retire(apply_args(plan, receipt))

    assert writer.exists()
    assert (writer / ".qualification" / "proof.txt").exists()
    assert not receipt.exists()

def test_unknown_owner_and_nonterminal_writer_are_retained(tmp_path: Path) -> None:
    repo, root, coordinator, _archive = fixture(tmp_path)
    unknown = root / "unknown"
    git(repo, "worktree", "add", "-b", "unknown", str(unknown), "main")
    assert lifecycle.classify(repo, root, coordinator, unknown)["classification"] == "RETAIN_UNKNOWN"
    writer = terminal_writer(repo, root, coordinator)
    telemetry = coordinator / "continuity-WRITER.jsonl"
    telemetry.write_text(json.dumps({"event": "stop", "yield_marker": "REAL_BLOCKER"}) + "\n")
    result = lifecycle.classify(repo, root, coordinator, writer)
    assert result["classification"] == "RETAIN_UNKNOWN"
    assert "not_assignment_complete" in result["reasons"]

def test_dead_registration_prune_requires_unchanged_exact_set(tmp_path: Path) -> None:
    repo, root, _coordinator, _archive = fixture(tmp_path)
    dead = root / "dead"
    git(repo, "worktree", "add", "-b", "dead", str(dead), "main")
    shutil.rmtree(dead)
    plan, receipt = tmp_path / "prune.json", tmp_path / "prune-receipt.json"
    lifecycle._plan_prune(Namespace(repo=repo, plan=plan))
    assert json.loads(plan.read_text())["prunable"] == [str(dead)]

    lifecycle._apply_prune(apply_args(plan, receipt))

    result = json.loads(receipt.read_text())
    assert result["state"] == "COMPLETE"
    assert result["pruned"] == [str(dead)]
    assert str(dead) not in git(repo, "worktree", "list", "--porcelain")
