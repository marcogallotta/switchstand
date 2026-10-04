from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from mcp import Client, StdioServerParameters

from switchstand.coordinator_sync import CoordinatorSync


def git(repo: Path, *arguments: str) -> str:
    return subprocess.check_output(["git", "-C", repo, *arguments], text=True).strip()


def setup(tmp_path: Path) -> tuple[Path, Path, Path, str, str]:
    home = tmp_path / "home"
    primary = home / "switchstand"
    remote = tmp_path / "remote.git"
    source = tmp_path / "source"
    subprocess.run(["git", "init", "--bare", "--initial-branch=main", remote], check=True,
                   capture_output=True)
    primary.mkdir(parents=True)
    subprocess.run(["git", "-C", primary, "init", "-b", "main"], check=True,
                   capture_output=True)
    for repo in (primary,):
        git(repo, "config", "user.name", "Test")
        git(repo, "config", "user.email", "test@example.invalid")
    (primary / "tracked.txt").write_text("start\n")
    git(primary, "add", "tracked.txt")
    git(primary, "commit", "-m", "start")
    started = git(primary, "rev-parse", "HEAD")
    git(primary, "remote", "add", "origin", str(remote))
    git(primary, "push", "-u", "origin", "main")
    subprocess.run(["git", "clone", remote, source], check=True, capture_output=True)
    git(source, "config", "user.name", "Test")
    git(source, "config", "user.email", "test@example.invalid")
    (source / "tracked.txt").write_text("final\n")
    git(source, "add", "tracked.txt")
    git(source, "commit", "-m", "final")
    final = git(source, "rev-parse", "HEAD")
    git(source, "push", "origin", "main")
    return home, primary, source, started, final


def test_exact_observation_then_fast_forward(tmp_path: Path) -> None:
    home, primary, _source, started, final = setup(tmp_path)
    control = CoordinatorSync(home)

    observed = control.currentness()
    result = control.sync(final)

    assert observed.model_dump() == {
        "status": "SYNC_REQUIRED", "current_sha": started, "target_sha": final,
        "reason": "canonical_main_differs",
    }
    assert result.status == "ok" and result.effect == "applied"
    assert result.previous_sha == started and result.resulting_sha == final
    assert git(primary, "status", "--porcelain", "--untracked-files=all") == ""


def test_moved_target_and_dirty_checkout_do_not_change_head(tmp_path: Path) -> None:
    home, primary, source, started, first_target = setup(tmp_path)
    control = CoordinatorSync(home)
    assert control.currentness().target_sha == first_target
    (source / "tracked.txt").write_text("later\n")
    git(source, "add", "tracked.txt")
    git(source, "commit", "-m", "later")
    git(source, "push", "origin", "main")

    moved = control.sync(first_target)
    assert moved.status == "not_applied" and moved.effect == "not_sent"
    assert moved.reason == "observed_target_is_no_longer_remote_main"
    assert git(primary, "rev-parse", "HEAD") == started

    (primary / "untracked.txt").write_text("preserve\n")
    dirty = control.sync(git(source, "rev-parse", "HEAD"))
    assert dirty.status == "not_applied" and dirty.reason == "dirty_canonical_checkout"
    assert git(primary, "rev-parse", "HEAD") == started
    assert (primary / "untracked.txt").read_text() == "preserve\n"


def test_divergence_is_rejected_without_changing_head(tmp_path: Path) -> None:
    home, primary, _source, _started, target = setup(tmp_path)
    (primary / "local.txt").write_text("local\n")
    git(primary, "add", "local.txt")
    git(primary, "commit", "-m", "local")
    local = git(primary, "rev-parse", "HEAD")

    result = CoordinatorSync(home).sync(target)

    assert result.status == "not_applied" and result.effect == "not_sent"
    assert result.reason == "canonical_main_is_not_ancestor"
    assert git(primary, "rev-parse", "HEAD") == local
    assert git(primary, "status", "--porcelain", "--untracked-files=all") == ""


def test_fast_forward_failure_reports_reason_and_preserves_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    home, primary, _source, started, target = setup(tmp_path)
    control = CoordinatorSync(home)
    real_git = control._git

    def fail_merge(*arguments: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        if arguments[:2] == ("merge", "--ff-only"):
            return subprocess.CompletedProcess(arguments, 1, "", "injected failure")
        return real_git(*arguments, check=check)

    monkeypatch.setattr(control, "_git", fail_merge)
    result = control.sync(target)

    assert result.status == "not_applied" and result.effect == "not_sent"
    assert result.reason == "fast_forward_failed"
    assert result.previous_sha == result.resulting_sha == started
    assert git(primary, "rev-parse", "HEAD") == started
    assert git(primary, "status", "--porcelain", "--untracked-files=all") == ""


async def test_real_stdio_boundary_performs_fixed_host_fast_forward(tmp_path: Path) -> None:
    home, primary, _source, _started, final = setup(tmp_path)
    server = StdioServerParameters(
        command=sys.executable,
        args=["-m", "switchstand.coordinator_sync"],
        env=os.environ | {"HOME": str(home), "PYTHONPATH": str(Path.cwd() / "src")},
    )
    async with Client(server) as client:
        tools = (await client.list_tools()).tools
        assert {tool.name for tool in tools} == {
            "coordinator_currentness_get", "coordinator_main_sync",
        }
        assert all(tool.input_schema.get("additionalProperties") is False for tool in tools)
        sync_schema = next(tool.input_schema for tool in tools
                           if tool.name == "coordinator_main_sync")
        assert set(sync_schema["properties"]) == {"api_version", "target_sha"}
        observed = await client.call_tool("coordinator_currentness_get", {"api_version": "1"})
        rejected = await client.call_tool(
            "coordinator_main_sync",
            {"api_version": "1", "target_sha": final, "path": str(primary)},
        )
        result = await client.call_tool(
            "coordinator_main_sync", {"api_version": "1", "target_sha": final}
        )
    assert observed.structured_content["target_sha"] == final
    assert rejected.is_error
    assert result.structured_content["status"] == "ok"
    assert git(primary, "rev-parse", "HEAD") == final
