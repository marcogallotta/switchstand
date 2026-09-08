import asyncio
import hashlib
import os
import subprocess
from pathlib import Path
from typing import Literal

from mcp.server import MCPServer
from pydantic import BaseModel, ConfigDict

from .mcp import closed_tool


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


def build_server() -> MCPServer:
    server = MCPServer("Switchstand Development Boundary")

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
        if any(Path(path).name == ".env" or Path(path).suffix == ".env" for path in paths):
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
                   "/app/.venv/bin/ruff check . && /app/.venv/bin/pyright --pythonpath /app/.venv/bin/python && /app/.venv/bin/pytest"]
        checked = await asyncio.to_thread(_quality, command)
        unchanged = before == _git(repo, "rev-parse", "HEAD").stdout.strip()
        state = "ok" if checked.returncode == 0 and unchanged else "failed"
        return _result(state, before, repo, checked.stdout + checked.stderr)

    closed_tool(server, "commit_all_current_worktree", commit_all_current_worktree)
    closed_tool(server, "quality", quality)
    return server


def main() -> None:
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
