from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
SCRIPT = ROOT / "scripts" / "local-quality"


def _candidate(tmp_path: Path) -> tuple[Path, dict[str, str], Path]:
    repo = tmp_path / "writer"
    repo.mkdir()
    (repo / "scripts").mkdir()
    (repo / "scripts" / "local-quality").write_bytes(SCRIPT.read_bytes())
    (repo / "scripts" / "local-quality").chmod(0o755)
    subprocess.run(["git", "init", "-q", repo], check=True)
    subprocess.run(["git", "-C", repo, "add", "."], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            repo,
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "-qm",
            "candidate",
        ],
        check=True,
    )
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    log = tmp_path / "docker.log"
    docker = fake_bin / "docker"
    docker.write_text(
        """#!/bin/sh
printf '%s\\n' "$*" >>"$DOCKER_LOG"
case "$1 $2" in
  'container inspect') test "${OCCUPIED:-0}" = 1 && exit 0; exit 1 ;;
  'build --quiet') echo sha256:built ;;
  'image inspect')
    test ! -e "$IMAGE_REMOVED" || exit 1
    echo sha256:built ;;
  'image rm')
    test "${IMAGE_REMOVE_FAIL:-0}" != 1 || exit 1
    : >"$IMAGE_REMOVED" ;;
  'container create')
    case " $* " in *' postgres:18-alpine '*) echo pg-id ;; *) echo quality-id ;; esac ;;
  'container start')
    case " $* " in
      *' --attach '*)
        case "${MUTATE_CANDIDATE:-}" in
          dirty) : >"$CANDIDATE_REPO/concurrent-change" ;;
          head) git -C "$CANDIDATE_REPO" -c user.name=Test -c user.email=test@example.invalid \
                  commit --allow-empty -qm concurrent ;;
        esac
        exit "${QUALITY_EXIT:-0}" ;;
      *) exit 0 ;;
    esac ;;
  'container exec') exit 0 ;;
  'container rm') exit 0 ;;
  *) echo "unexpected docker command: $*" >&2; exit 97 ;;
esac
"""
    )
    docker.chmod(0o755)
    env = os.environ | {
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "DOCKER_LOG": str(log),
        "IMAGE_REMOVED": str(tmp_path / "image-removed"),
        "CANDIDATE_REPO": str(repo),
    }
    return repo, env, log


def test_runs_ci_commands_against_read_only_candidate_and_owned_postgres(
    tmp_path: Path,
):
    repo, env, log = _candidate(tmp_path)

    result = subprocess.run(
        [repo / "scripts" / "local-quality"],
        cwd=repo,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "QUALITY_RESULT=PASS" in result.stdout
    assert "CLEANUP_RESULT=PASS" in result.stdout
    assert "QUALITY_SUBJECT_SHA=" in result.stdout
    commands = log.read_text()
    assert "postgres:18-alpine" in commands
    assert "--network container:pg-id" in commands
    assert f"--volume {repo}:/workspace:ro" in commands
    assert f"--volume {repo / '.git'}:{repo / '.git'}:ro" in commands
    assert "TEST_DATABASE_URL=postgresql+psycopg://postgres:" in commands
    assert "/app/.venv/bin/ruff check --no-cache ." in commands
    assert "/app/.venv/bin/pyright --pythonpath /app/.venv/bin/python" in commands
    assert "/app/.venv/bin/pytest -ra -p no:cacheprovider" in commands
    assert "container rm --force quality-id" in commands
    assert "container rm --force pg-id" in commands
    assert "image rm switchstand-local-quality:" in commands
    assert "network create" not in commands


def test_failure_is_reported_and_both_exact_containers_are_removed(
    tmp_path: Path,
):
    repo, env, log = _candidate(tmp_path)
    env["QUALITY_EXIT"] = "7"

    result = subprocess.run(
        [repo / "scripts" / "local-quality"],
        cwd=repo,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 1
    assert "QUALITY_RESULT=FAIL" in result.stdout
    commands = log.read_text()
    assert "container rm --force quality-id" in commands
    assert "container rm --force pg-id" in commands


def test_dirty_candidate_is_not_run(tmp_path: Path):
    repo, env, log = _candidate(tmp_path)
    (repo / "uncommitted").write_text("change")

    result = subprocess.run(
        [repo / "scripts" / "local-quality"],
        cwd=repo,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 2
    assert "QUALITY_RESULT=NOT_RUN" in result.stdout
    assert not log.exists()


def test_occupied_deterministic_name_is_preserved_and_not_run(tmp_path: Path):
    repo, env, log = _candidate(tmp_path)
    env["OCCUPIED"] = "1"

    result = subprocess.run(
        [repo / "scripts" / "local-quality"],
        cwd=repo,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 1
    assert "QUALITY_RESULT=NOT_RUN" in result.stdout
    assert "refusing occupied deterministic container name" in result.stderr
    assert "container rm" not in log.read_text()


@pytest.mark.parametrize("change", ["dirty", "head"])
def test_concurrent_candidate_change_cannot_pass_stale_subject(
    tmp_path: Path, change: str
):
    repo, env, _ = _candidate(tmp_path)
    env["MUTATE_CANDIDATE"] = change

    result = subprocess.run(
        [repo / "scripts" / "local-quality"],
        cwd=repo,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 1
    assert "QUALITY_RESULT=FAIL" in result.stdout
    assert "result is not exact-head evidence" in result.stderr


def test_image_cleanup_failure_is_truthful(tmp_path: Path):
    repo, env, _ = _candidate(tmp_path)
    env["IMAGE_REMOVE_FAIL"] = "1"

    result = subprocess.run(
        [repo / "scripts" / "local-quality"],
        cwd=repo,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 1
    assert "QUALITY_RESULT=PASS" in result.stdout
    assert "CLEANUP_RESULT=FAIL" in result.stdout
