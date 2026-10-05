from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from mcp import Client, StdioServerParameters

from switchstand.coordinator_sync import CoordinatorSync, build_server


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


def control(home: Path, remote: Path) -> CoordinatorSync:
    return CoordinatorSync(
        home, remote_source=str(remote), accepted_origins=frozenset({str(remote)}),
        remote_protocol="file",
    )


def test_exact_observation_then_fast_forward(tmp_path: Path) -> None:
    home, primary, source, started, final = setup(tmp_path)
    subject = control(home, source.parent / "remote.git")

    observed = subject.currentness()
    result = subject.sync(final)

    assert observed.model_dump() == {
        "status": "SYNC_REQUIRED", "current_sha": started, "target_sha": final,
        "reason": "canonical_main_differs",
    }
    assert result.status == "ok" and result.effect == "applied"
    assert result.previous_sha == started and result.resulting_sha == final
    assert git(primary, "status", "--porcelain", "--untracked-files=all") == ""


def test_moved_target_and_dirty_checkout_do_not_change_head(tmp_path: Path) -> None:
    home, primary, source, started, first_target = setup(tmp_path)
    subject = control(home, source.parent / "remote.git")
    assert subject.currentness().target_sha == first_target
    (source / "tracked.txt").write_text("later\n")
    git(source, "add", "tracked.txt")
    git(source, "commit", "-m", "later")
    git(source, "push", "origin", "main")

    moved = subject.sync(first_target)
    assert moved.status == "not_applied" and moved.effect == "not_sent"
    assert moved.reason == "observed_target_is_no_longer_remote_main"
    assert git(primary, "rev-parse", "HEAD") == started

    (primary / "untracked.txt").write_text("preserve\n")
    dirty = subject.sync(git(source, "rev-parse", "HEAD"))
    assert dirty.status == "not_applied" and dirty.reason == "dirty_canonical_checkout"
    assert git(primary, "rev-parse", "HEAD") == started
    assert (primary / "untracked.txt").read_text() == "preserve\n"


def test_divergence_is_rejected_without_changing_head(tmp_path: Path) -> None:
    home, primary, source, _started, target = setup(tmp_path)
    (primary / "local.txt").write_text("local\n")
    git(primary, "add", "local.txt")
    git(primary, "commit", "-m", "local")
    local = git(primary, "rev-parse", "HEAD")

    result = control(home, source.parent / "remote.git").sync(target)

    assert result.status == "not_applied" and result.effect == "not_sent"
    assert result.reason == "canonical_main_is_not_ancestor"
    assert git(primary, "rev-parse", "HEAD") == local
    assert git(primary, "status", "--porcelain", "--untracked-files=all") == ""


def test_fast_forward_failure_reports_reason_and_preserves_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    home, primary, source, started, target = setup(tmp_path)
    subject = control(home, source.parent / "remote.git")
    real_git = subject._git

    def fail_merge(*arguments: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        if arguments[:2] == ("merge", "--ff-only"):
            return subprocess.CompletedProcess(arguments, 1, "", "injected failure")
        return real_git(*arguments, check=check)

    monkeypatch.setattr(subject, "_git", fail_merge)
    result = subject.sync(target)

    assert result.status == "not_applied" and result.effect == "not_sent"
    assert result.reason == "fast_forward_failed"
    assert result.previous_sha == result.resulting_sha == started
    assert git(primary, "rev-parse", "HEAD") == started
    assert git(primary, "status", "--porcelain", "--untracked-files=all") == ""


async def test_real_stdio_boundary_performs_fixed_host_fast_forward(tmp_path: Path) -> None:
    home, primary, source, _started, final = setup(tmp_path)
    server = StdioServerParameters(
        command=sys.executable,
        args=[str(Path(__file__)), "serve", str(home), str(source.parent / "remote.git")],
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


def executable(path: Path, marker: Path, *, passthrough: bool = False) -> None:
    suffix = "\n/bin/cat" if passthrough else "\nexit 97"
    path.write_text(f'#!/bin/sh\ntouch "{marker}"{suffix}\n')
    path.chmod(0o755)


def test_hostile_git_execution_config_is_sanitized_during_real_fast_forward(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    home, primary, source, started, target = setup(tmp_path)
    marker = tmp_path / "escaped"
    helper = tmp_path / "helper"
    executable(helper, marker)
    hook = primary / ".git/hooks/post-merge"
    executable(hook, marker)
    git(primary, "config", "core.sshCommand", str(helper))
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.fsmonitor")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", str(helper))
    monkeypatch.setenv("GIT_SSH_COMMAND", str(helper))
    monkeypatch.setenv("GIT_EXEC_PATH", str(tmp_path))

    result = control(home, source.parent / "remote.git").handoff(started)

    assert result.status == "ready" and result.effect == "applied"
    assert git(primary, "rev-parse", "HEAD") == target
    assert not marker.exists()


def test_filter_command_and_noncanonical_remote_are_rejected_before_execution(
    tmp_path: Path,
) -> None:
    home, primary, source, started, _target = setup(tmp_path)
    marker = tmp_path / "escaped"
    helper = tmp_path / "helper"
    executable(helper, marker, passthrough=True)
    (primary / ".git/info/attributes").write_text("tracked.txt filter=evil\n")
    git(primary, "config", "filter.evil.clean", str(helper))
    git(primary, "config", "filter.evil.smudge", str(helper))
    subject = control(home, source.parent / "remote.git")

    filtered = subject.handoff(started)

    assert filtered.status == "blocked" and filtered.reason == "unsafe_local_git_config"
    assert git(primary, "rev-parse", "HEAD") == started
    assert not marker.exists()

    git(primary, "config", "--remove-section", "filter.evil")
    git(primary, "remote", "set-url", "origin", f"ext::{helper}")
    wrong_remote = subject.handoff(started)
    assert wrong_remote.status == "blocked"
    assert wrong_remote.reason == "canonical_remote_identity_mismatch"
    assert git(primary, "rev-parse", "HEAD") == started
    assert not marker.exists()


def test_alternate_refs_command_is_rejected_then_normal_fast_forward_works(
    tmp_path: Path,
) -> None:
    home, primary, source, started, target = setup(tmp_path)
    marker = tmp_path / "alternate-refs-escaped"
    git(primary, "config", "core.alternateRefsCommand", f"/usr/bin/touch {marker}")
    subject = control(home, source.parent / "remote.git")

    rejected = subject.sync(target)

    assert rejected.status == "not_applied" and rejected.effect == "not_sent"
    assert rejected.reason == "unsafe_local_git_config"
    assert git(primary, "rev-parse", "HEAD") == started
    assert not marker.exists()

    git(primary, "config", "--unset", "core.alternateRefsCommand")
    applied = subject.sync(target)
    assert applied.status == "ok" and applied.effect == "applied"
    assert git(primary, "rev-parse", "HEAD") == target
    assert not marker.exists()


if __name__ == "__main__" and len(sys.argv) == 4 and sys.argv[1] == "serve":
    test_home, test_remote = Path(sys.argv[2]), Path(sys.argv[3])
    build_server(CoordinatorSync(
        test_home, remote_source=str(test_remote),
        accepted_origins=frozenset({str(test_remote)}), remote_protocol="file",
    )).run()
