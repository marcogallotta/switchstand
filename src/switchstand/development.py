import asyncio
import hashlib
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Literal

from mcp.server import MCPServer
from pydantic import BaseModel, ConfigDict

from .docker import inspect_object, label_arguments, remove_owned, require_absent
from .mcp import closed_tool
from .run import RECEIPT, RunStatus, inspect_receipt

CREATE_SECONDS = 30
FOCUSED_SECONDS = 120
QUALITY_SECONDS = 600
WORKLOAD_STOP_SECONDS = 5


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
    return subprocess.run(
        ["git", "-c", "core.hooksPath=/dev/null", "-c", "commit.gpgSign=false", *arguments],
        cwd=repo,
        env=_environment(),
        text=True,
        capture_output=True,
        check=False,
    )


def _bound_repo() -> tuple[Path, str, str]:
    repo = Path(os.environ["SWITCHSTAND_WORKTREE"]).resolve(strict=True)
    expected_branch, expected_common = (
        os.environ[name] for name in ("SWITCHSTAND_BRANCH", "SWITCHSTAND_GIT_COMMON")
    )
    values = _git(
        repo,
        "rev-parse",
        "--path-format=absolute",
        "--show-toplevel",
        "--git-dir",
        "--git-common-dir",
        "--abbrev-ref",
        "HEAD",
    ).stdout.splitlines()
    root, git_dir, common, branch = values
    if (
        Path(root).resolve() != repo
        or Path(git_dir).resolve() == Path(common).resolve()
        or str(Path(common).resolve()) != expected_common
        or branch != expected_branch
    ):
        raise RuntimeError("development boundary lost its exact linked-worktree ownership")
    return repo, branch, _git(repo, "rev-parse", "HEAD").stdout.strip()


def _result(
    status: Literal["ok", "failed", "stale"], before: str, repo: Path, output: str = ""
) -> DevelopmentResult:
    return DevelopmentResult(
        status=status,
        before=before,
        after=_git(repo, "rev-parse", "HEAD").stdout.strip(),
        output=output[-12000:],
    )


def _manifest(repo: Path) -> str:
    digest = hashlib.sha256()
    for name in ("pyproject.toml", "uv.lock"):
        digest.update((repo / name).read_bytes())
    return digest.hexdigest()


def _credential_path(path: str) -> bool:
    name = Path(path).name
    return name.endswith(".env") or ".env." in name


def _run_owner(repo: Path, branch: str) -> str:
    git_dir = _git(repo, "rev-parse", "--absolute-git-dir")
    if git_dir.returncode != 0:
        raise RuntimeError("cannot resolve managed run receipt")
    status = inspect_receipt(Path(git_dir.stdout.strip()) / RECEIPT, repo, branch)
    if status.status != "running" or status.run_id is None:
        raise RuntimeError("development workload requires one exact running managed run")
    return str(status.run_id)


def _workload_name(owner: str, role: Literal["check", "quality"]) -> str:
    return f"switchstand-{role}-{owner.replace('-', '')}"


def _container_arguments(
    repo: Path, owner: str, role: Literal["check", "quality"], test_paths: list[Path]
) -> list[str]:
    command = [
        "create",
        "--name",
        _workload_name(owner, role),
        *label_arguments(owner, role),
        "--network",
        os.environ["SWITCHSTAND_QUALITY_NETWORK"],
        "-e",
        "TEST_DATABASE_URL=postgresql+psycopg://switchstand:switchstand@postgres-test/switchstand_test",
        "-e",
        "SWITCHSTAND_REQUIRE_TEST_DATABASE=1",
        "-v",
        f"{repo}:/workspace:ro",
        "-w",
        "/workspace",
        os.environ["SWITCHSTAND_QUALITY_IMAGE"],
        "sh",
        "-c",
    ]
    script = (
        "PYTHONPATH=/workspace/src /app/.venv/bin/ruff check --no-cache . && "
        "PYTHONPATH=/workspace/src /app/.venv/bin/pyright "
        "--pythonpath /app/.venv/bin/python && "
        "PYTHONPATH=/workspace/src /app/.venv/bin/pytest -p no:cacheprovider"
    )
    if role == "check":
        command.extend([script + ' \"$@\"', "switchstand-check", *(str(path) for path in test_paths)])
    else:
        command.append(script)
    return command


def _create_owned_container(
    arguments: list[str], owner: str, role: Literal["check", "quality"]
) -> str:
    env = _environment()
    name = _workload_name(owner, role)
    require_absent("container", name, owner, role, env)
    try:
        created = subprocess.run(
            ["docker", *arguments],
            env=env,
            text=True,
            capture_output=True,
            check=False,
            timeout=CREATE_SECONDS,
        )
    except subprocess.TimeoutExpired as error:
        existing = inspect_object("container", name, env)
        if existing is not None and existing.owner == owner and existing.role == role:
            remove_owned("container", name, owner, role, env)
        raise RuntimeError(f"Docker {role} create exceeded {CREATE_SECONDS} seconds") from error
    if created.returncode != 0:
        detail = (created.stderr or created.stdout).strip()
        raise RuntimeError(f"Docker {role} create failed: {detail or 'no diagnostic output'}")
    container_id = created.stdout.strip().splitlines()[-1]
    existing = inspect_object("container", name, env)
    if (
        existing is None
        or existing.object_id != container_id
        or existing.owner != owner
        or existing.role != role
    ):
        raise RuntimeError(f"Docker {role} create did not bind the exact owned container")
    return container_id


async def _stop_process(process: asyncio.subprocess.Process) -> None:
    if process.returncode is not None:
        return
    process.terminate()
    try:
        await asyncio.wait_for(process.wait(), WORKLOAD_STOP_SECONDS)
    except TimeoutError:
        process.kill()
        await process.wait()


async def _run_owned_workload(
    arguments: list[str], owner: str, role: Literal["check", "quality"], timeout: int
) -> subprocess.CompletedProcess[str]:
    env = _environment()
    name = _workload_name(owner, role)
    container_id = _create_owned_container(arguments, owner, role)
    command = ["docker", "start", "--attach", container_id]
    with tempfile.TemporaryFile(mode="w+", encoding="utf-8") as output:
        process = await asyncio.create_subprocess_exec(
            *command,
            env=env,
            stdout=output,
            stderr=subprocess.STDOUT,
        )
        try:
            await asyncio.wait_for(process.wait(), timeout)
        except BaseException:
            await _stop_process(process)
            remove_owned("container", name, owner, role, env)
            raise
        output.seek(0)
        text = output.read()
    returncode = process.returncode
    remove_owned("container", name, owner, role, env)
    if returncode is None:
        raise RuntimeError(f"Docker {role} workload ended without a return code")
    return subprocess.CompletedProcess(command, returncode, stdout=text, stderr="")


def build_server(bound: bool = True) -> MCPServer:
    server = MCPServer("Switchstand Development Boundary")
    if not bound:
        return server

    async def check(expected_head: str, test_paths: list[str]) -> DevelopmentResult:
        """Run Ruff, strict Pyright, and selected tests against the active worktree."""
        repo, branch, before = _bound_repo()
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
        owner = _run_owner(repo, branch)
        try:
            checked = await _run_owned_workload(
                _container_arguments(repo, owner, "check", paths), owner, "check", FOCUSED_SECONDS
            )
        except TimeoutError as error:
            return _result(
                "failed", before, repo, f"focused check exceeded {FOCUSED_SECONDS} seconds: {error}"
            )
        except RuntimeError as error:
            return _result("failed", before, repo, str(error))
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
        paths = _git(
            repo, "ls-files", "--cached", "--others", "--exclude-standard"
        ).stdout.splitlines()
        if any(_credential_path(path) for path in paths):
            return _result("failed", before, repo, "tracked credential path rejected")
        added = _git(repo, "add", "-A", "--", ":/")
        committed = _git(repo, "commit", "-m", message) if added.returncode == 0 else added
        state = "ok" if committed.returncode == 0 else "failed"
        return _result(state, before, repo, committed.stdout + committed.stderr)

    async def quality(expected_head: str) -> DevelopmentResult:
        """Run the fixed full gate against one clean, exact, dependency-pinned candidate."""
        repo, branch, before = _bound_repo()
        if before != expected_head:
            return _result("stale", before, repo)
        if _git(repo, "status", "--porcelain").stdout:
            return _result("failed", before, repo, "candidate worktree is not clean")
        if _manifest(repo) != os.environ["SWITCHSTAND_MANIFEST_SHA256"]:
            return _result("stale", before, repo, "dependency manifests changed; relaunch required")
        owner = _run_owner(repo, branch)
        try:
            checked = await _run_owned_workload(
                _container_arguments(repo, owner, "quality", []), owner, "quality", QUALITY_SECONDS
            )
        except TimeoutError as error:
            return _result(
                "failed", before, repo, f"full quality exceeded {QUALITY_SECONDS} seconds: {error}"
            )
        except RuntimeError as error:
            return _result("failed", before, repo, str(error))
        unchanged = (
            before == _git(repo, "rev-parse", "HEAD").stdout.strip()
            and not _git(repo, "status", "--porcelain").stdout
            and _manifest(repo) == os.environ["SWITCHSTAND_MANIFEST_SHA256"]
        )
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
    build_server().run()


if __name__ == "__main__":
    main()
