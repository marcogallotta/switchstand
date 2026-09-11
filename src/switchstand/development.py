import asyncio
import hashlib
import os
import subprocess
from pathlib import Path
from typing import Literal
from uuid import uuid4

from mcp.server import MCPServer
from pydantic import BaseModel, ConfigDict

from .mcp import closed_tool
from .run import RECEIPT, RunStatus, inspect_receipt


class DevelopmentResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["ok", "failed", "stale"]
    before: str
    after: str
    output: str = ""


def _environment() -> dict[str, str]:
    allowed = ("PATH", "HOME", "LANG", "LC_ALL", "TZ")
    return {key: os.environ[key] for key in allowed if key in os.environ}


def _git(repo: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", "-c", "core.hooksPath=/dev/null",
                           "-c", "commit.gpgSign=false", *arguments], cwd=repo,
                          env=_environment(), text=True, capture_output=True, check=False)


def _bound_repo() -> tuple[Path, str, str]:
    repo = Path(os.environ["SWITCHSTAND_WORKTREE"]).resolve(strict=True)
    expected_branch, expected_common = (os.environ[name] for name in
                                        ("SWITCHSTAND_BRANCH", "SWITCHSTAND_GIT_COMMON"))
    values = _git(repo, "rev-parse", "--path-format=absolute", "--show-toplevel", "--git-dir",
                  "--git-common-dir", "--abbrev-ref", "HEAD").stdout.splitlines()
    root, git_dir, common, branch = values
    if (Path(root).resolve() != repo or Path(git_dir).resolve() == Path(common).resolve()
            or str(Path(common).resolve()) != expected_common or branch != expected_branch):
        raise RuntimeError("development boundary lost its exact linked-worktree ownership")
    return repo, branch, _git(repo, "rev-parse", "HEAD").stdout.strip()


def _result(status: Literal["ok", "failed", "stale"], before: str, repo: Path, output: str = ""):
    return DevelopmentResult(status=status, before=before,
                             after=_git(repo, "rev-parse", "HEAD").stdout.strip(),
                             output=output[-12000:])


def _manifest(repo: Path) -> str:
    digest = hashlib.sha256()
    for name in ("pyproject.toml", "uv.lock"):
        digest.update((repo / name).read_bytes())
    return digest.hexdigest()


def _quality(command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, env=_environment(), text=True, capture_output=True, check=False)


def _focused(command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command, env=_environment(), text=True, capture_output=True, check=False, timeout=120
    )


def _stop_check_container(name: str) -> str:
    try:
        stopped = subprocess.run(["docker", "rm", "-f", name], env=_environment(), text=True,
                                 capture_output=True, check=False, timeout=15)
    except (OSError, subprocess.TimeoutExpired) as error:
        return f"failed to stop focused-check container {name}: {error}"
    if stopped.returncode != 0 and "No such container" not in stopped.stderr:
        detail = (stopped.stderr or stopped.stdout).strip()
        return f"failed to stop focused-check container {name}: {detail or stopped.returncode}"
    return ""


def _credential_path(path: str) -> bool:
    name = Path(path).name
    return name.endswith(".env") or ".env." in name


def build_server(bound: bool = True) -> MCPServer:
    server = MCPServer("Switchstand Development Boundary")
    if not bound:
        return server

    async def check(expected_head: str, test_paths: list[str]) -> DevelopmentResult:
        """Run Ruff, strict Pyright, and selected tests against the active worktree."""
        repo, _, before = _bound_repo()
        if before != expected_head:
            return _result("stale", before, repo)
        if _manifest(repo) != os.environ["SWITCHSTAND_MANIFEST_SHA256"]:
            return _result("stale", before, repo, "dependency manifests changed; relaunch required")
        if not test_paths or len(test_paths) > 20:
            return _result("failed", before, repo, "select 1-20 affected test paths")
        paths = [Path(value) for value in test_paths]
        if any(
            path.is_absolute()
            or ".." in path.parts
            or path.parts[:1] != ("tests",)
            or not (repo / path).is_file()
            for path in paths
        ):
            return _result("failed", before, repo, "test paths must be existing files under tests/")
        container_name = f"switchstand-check-{uuid4().hex}"
        command = [
            "docker", "run", "--rm", "--name", container_name,
            "--network", os.environ["SWITCHSTAND_QUALITY_NETWORK"],
            "-e", "TEST_DATABASE_URL=postgresql+psycopg://switchstand:switchstand@postgres-test/switchstand_test",
            "-e", "SWITCHSTAND_REQUIRE_TEST_DATABASE=1", "-v", f"{repo}:/workspace:ro",
            "-w", "/workspace", os.environ["SWITCHSTAND_QUALITY_IMAGE"], "sh", "-c",
            (
                "PYTHONPATH=/workspace/src /app/.venv/bin/ruff check --no-cache . && "
                "PYTHONPATH=/workspace/src /app/.venv/bin/pyright "
                "--pythonpath /app/.venv/bin/python && "
                "PYTHONPATH=/workspace/src /app/.venv/bin/pytest -p no:cacheprovider \"$@\""
            ),
            "switchstand-check", *(str(path) for path in paths),
        ]
        try:
            checked = await asyncio.to_thread(_focused, command)
        except subprocess.TimeoutExpired as error:
            cleanup_error = await asyncio.to_thread(_stop_check_container, container_name)
            detail = f"focused check exceeded 120 seconds: {error}"
            if cleanup_error:
                detail += f"; {cleanup_error}"
            return _result("failed", before, repo, detail)
        except asyncio.CancelledError:
            cleanup_error = await asyncio.to_thread(_stop_check_container, container_name)
            if cleanup_error:
                raise RuntimeError(cleanup_error) from None
            raise
        unchanged = before == _git(repo, "rev-parse", "HEAD").stdout.strip()
        state = "ok" if checked.returncode == 0 and unchanged else "failed"
        return _result(state, before, repo, checked.stdout + checked.stderr)

    async def commit_all_current_worktree(expected_head: str, message: str) -> DevelopmentResult:
        """Commit all changes on the bound branch when its exact head still matches."""
        repo, _, before = _bound_repo()
        if before != expected_head:
            return _result("stale", before, repo)
        if not message.strip() or len(message) > 2000:
            return _result("failed", before, repo, "commit message must contain 1-2000 characters")
        tracked = _git(repo, "ls-files", "--stage").stdout
        if "160000 " in tracked or any(path != repo / ".git" for path in repo.rglob(".git")):
            return _result("failed", before, repo, "nested repositories are not supported")
        paths = _git(repo, "ls-files", "--cached", "--others", "--exclude-standard").stdout.splitlines()
        if any(_credential_path(path) for path in paths):
            return _result("failed", before, repo, "tracked credential path rejected")
        added = _git(repo, "add", "-A", "--", ":/")
        committed = _git(repo, "commit", "-m", message) if added.returncode == 0 else added
        state = "ok" if committed.returncode == 0 else "failed"
        return _result(state, before, repo, committed.stdout + committed.stderr)

    async def quality(expected_head: str) -> DevelopmentResult:
        """Run the fixed full gate against one clean, exact, dependency-pinned candidate."""
        repo, _, before = _bound_repo()
        if before != expected_head:
            return _result("stale", before, repo)
        if _git(repo, "status", "--porcelain").stdout:
            return _result("failed", before, repo, "candidate worktree is not clean")
        if _manifest(repo) != os.environ["SWITCHSTAND_MANIFEST_SHA256"]:
            return _result("stale", before, repo, "dependency manifests changed; relaunch required")
        command = ["docker", "run", "--rm", "--network", os.environ["SWITCHSTAND_QUALITY_NETWORK"],
                   "-e", "TEST_DATABASE_URL=postgresql+psycopg://switchstand:switchstand@postgres-test/switchstand_test",
                   "-v", f"{repo}:/workspace:ro", "-w", "/workspace",
                   os.environ["SWITCHSTAND_QUALITY_IMAGE"], "sh", "-c",
                   "PYTHONPATH=/workspace/src /app/.venv/bin/ruff check --no-cache . && PYTHONPATH=/workspace/src /app/.venv/bin/pyright --pythonpath /app/.venv/bin/python && PYTHONPATH=/workspace/src /app/.venv/bin/pytest -p no:cacheprovider"]
        checked = await asyncio.to_thread(_quality, command)
        unchanged = (before == _git(repo, "rev-parse", "HEAD").stdout.strip()
                     and not _git(repo, "status", "--porcelain").stdout
                     and _manifest(repo) == os.environ["SWITCHSTAND_MANIFEST_SHA256"])
        state = "ok" if checked.returncode == 0 and unchanged else "failed"
        return _result(state, before, repo, checked.stdout + checked.stderr)

    async def run_status() -> RunStatus:
        """Report the exact managed run identity and observed process state."""
        repo, branch, _ = _bound_repo()
        result = _git(repo, "rev-parse", "--absolute-git-dir")
        if result.returncode != 0:
            return RunStatus(status="unknown")
        return inspect_receipt(Path(result.stdout.strip()) / RECEIPT, repo, branch)

    closed_tool(server, "check", check)
    closed_tool(server, "commit_all_current_worktree", commit_all_current_worktree)
    closed_tool(server, "quality", quality)
    closed_tool(server, "run_status", run_status)
    return server


def main() -> None:
    bound = os.getenv("SWITCHSTAND_MANAGED") == "1"
    if not bound:
        build_server(False).run()
        return
    try:
        build_server().run()
    finally:
        env = _environment()
        subprocess.run(["docker", "rm", "-f", os.environ["SWITCHSTAND_DATABASE_CONTAINER"]],
                       env=env, capture_output=True, check=False)
        subprocess.run(["docker", "network", "rm", os.environ["SWITCHSTAND_QUALITY_NETWORK"]],
                       env=env, capture_output=True, check=False)


if __name__ == "__main__":
    main()
