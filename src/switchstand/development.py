import asyncio
import hashlib
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Literal, NamedTuple
from uuid import UUID

from mcp.server import MCPServer
from pydantic import BaseModel, ConfigDict

from .docker import (
    DockerKind,
    DockerObject,
    inspect,
    owned_name,
    remove_owned,
    require_absent,
    require_owned,
)
from .docker import command as docker_command
from .docker import labels as docker_labels
from .mcp import closed_tool
from .run import RECEIPT, RunStatus, inspect_receipt

CREATE_SECONDS = 30
FOCUSED_SECONDS = 120
QUALITY_SECONDS = 600
WORKLOAD_STOP_SECONDS = 5
WORKLOAD_OUTPUT_BYTES = 12000
WORKLOAD_OUTPUT_CHUNK_BYTES = 4096


class DevelopmentBoundary(NamedTuple):
    image: str
    network: str
    database: str
    manifest: str


def development_names(repo: Path, owner: UUID | int | str) -> tuple[str, str, str]:
    suffix = hashlib.sha256(f"{repo}:{owner}".encode()).hexdigest()[:12]
    return (
        f"switchstand-runner-{suffix}",
        f"switchstand-dev-{suffix}",
        f"switchstand-test-{suffix}",
    )


def development_subnet(repo: Path, owner: UUID | int | str) -> str:
    """Keep disposable development bridges out of common home-LAN address space."""
    digest = hashlib.sha256(f"{repo}:{owner}".encode()).digest()
    slot = int.from_bytes(digest[:2], "big") & 0x0FFF
    return f"10.{240 + (slot >> 8)}.{slot & 0xFF}.0/24"


def docker_run(
    arguments: list[str],
    cwd: Path | None,
    env: dict[str, str],
    *,
    timeout: float = 10,
) -> subprocess.CompletedProcess[str]:
    result = docker_command(
        arguments, env, cwd=str(cwd) if cwd is not None else None, timeout=timeout
    )
    if result.returncode == 0:
        return result
    detail = (result.stderr or result.stdout or "no diagnostic output").strip()
    raise RuntimeError(f"docker {' '.join(arguments)} failed: {detail}")


def cleanup_development(
    image: str | None,
    network: str | None,
    database: str | None,
    candidate: Path,
    owner: str,
    env: dict[str, str],
    *,
    required: bool = True,
) -> None:
    failures: list[str] = []
    image_name, network_name, database_name = development_names(candidate, owner)
    operations: tuple[tuple[DockerKind, str | None, str, str], ...] = (
        ("container", database, database_name, "database"),
        ("container", None, owned_name(owner, "focused"), "focused"),
        ("container", None, owned_name(owner, "quality"), "quality"),
        ("network", network, network_name, "qualification"),
        ("image", image, image_name, "runner"),
    )
    for kind, object_id, name, role in operations:
        try:
            if object_id is None:
                observed = inspect(kind, name, env)
                if observed is None:
                    continue
                object_id = observed.object_id
            remove_owned(kind, object_id, owner, role, env)
        except RuntimeError as error:
            failures.append(str(error))
    if failures and required:
        raise RuntimeError("development cleanup failed: " + "; ".join(failures))


def prepare_development(
    control: Path, candidate: Path, owner: UUID, env: dict[str, str]
) -> DevelopmentBoundary:
    image_name, network, database = development_names(candidate, owner)
    image_id: str | None = None
    network_id: str | None = None
    database_id: str | None = None
    try:
        with tempfile.TemporaryDirectory(prefix="switchstand-runner-") as temporary:
            context = Path(temporary)
            for name in ("pyproject.toml", "uv.lock", "README.md"):
                shutil.copyfile(candidate / name, context / name)
            require_absent("image", image_name, env)
            image = docker_run(
                [
                    "build", "--quiet", "--tag", image_name,
                    *docker_labels(str(owner), "runner"),
                    "-f", str(control / "Dockerfile.candidate-runner"), ".",
                ],
                context,
                env,
                timeout=600,
            )
        image_id = image.stdout.strip().splitlines()[-1]
        require_owned("image", image_id, str(owner), "runner", env)
        require_absent("network", network, env)
        created_network = docker_run(
            [
                "network", "create", "--subnet", development_subnet(candidate, owner),
                *docker_labels(str(owner), "qualification"), network,
            ],
            None,
            env,
        )
        network_id = created_network.stdout.strip().splitlines()[-1]
        require_owned("network", network_id, str(owner), "qualification", env)
        require_absent("container", database, env)
        created_database = docker_run([
            "run", "-d", "--name", database, *docker_labels(str(owner), "database"),
            "--network", network_id,
            "--network-alias", "postgres-test", "--tmpfs", "/var/lib/postgresql",
            "-e", "POSTGRES_DB=switchstand_test", "-e", "POSTGRES_USER=switchstand",
            "-e", "POSTGRES_PASSWORD=switchstand", "postgres:18-alpine",
        ], None, env)
        database_id = created_database.stdout.strip().splitlines()[-1]
        require_owned("container", database_id, str(owner), "database", env)
        for _ in range(30):
            ready = subprocess.run(
                ["docker", "exec", database_id, "pg_isready", "-U", "switchstand"],
                env=env, capture_output=True, check=False,
            )
            if ready.returncode == 0:
                break
            time.sleep(1)
        else:
            raise RuntimeError("development test database did not become ready")
        digest = hashlib.sha256()
        for name in ("pyproject.toml", "uv.lock"):
            digest.update((candidate / name).read_bytes())
        return DevelopmentBoundary(image_id, network_id, database_id, digest.hexdigest())
    except BaseException:
        cleanup_development(
            image_id, network_id, database_id, candidate, str(owner), env, required=False
        )
        raise


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


def _container_arguments(
    repo: Path, owner: str, role: Literal["focused", "quality"], test_paths: list[Path]
) -> list[str]:
    command = [
        "create",
        "--name",
        owned_name(owner, role),
        *docker_labels(owner, role),
        "--read-only",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--pids-limit",
        "512",
        "--memory",
        "2g",
        "--tmpfs",
        "/tmp:rw,nosuid,nodev,noexec",
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
    if role == "focused":
        command.extend([script + ' \"$@\"', "switchstand-check", *(str(path) for path in test_paths)])
    else:
        command.append(script)
    return command


async def _stop_process(process: asyncio.subprocess.Process) -> None:
    if process.returncode is not None:
        return
    process.terminate()
    try:
        await asyncio.wait_for(process.wait(), WORKLOAD_STOP_SECONDS)
    except TimeoutError:
        process.kill()
        await process.wait()


async def _read_bounded_output(stream: asyncio.StreamReader) -> str:
    output = bytearray()
    while chunk := await stream.read(
        min(WORKLOAD_OUTPUT_CHUNK_BYTES, WORKLOAD_OUTPUT_BYTES - len(output) + 1)
    ):
        output.extend(chunk)
        if len(output) > WORKLOAD_OUTPUT_BYTES:
            raise RuntimeError(
                f"Docker workload output exceeded the {WORKLOAD_OUTPUT_BYTES}-byte hard limit; "
                "reduce test or tool output and rerun"
            )
    return output.decode(errors="replace")


async def _wait_with_bounded_output(
    process: asyncio.subprocess.Process, timeout: int
) -> str:
    if process.stdout is None:
        raise RuntimeError("Docker workload output pipe is unavailable")
    wait = asyncio.create_task(process.wait())
    read = asyncio.create_task(_read_bounded_output(process.stdout))
    try:
        _, text = await asyncio.wait_for(asyncio.gather(wait, read), timeout)
        return text
    finally:
        for task in (wait, read):
            if not task.done():
                task.cancel()
        await asyncio.gather(wait, read, return_exceptions=True)


async def _remove_exact_id(
    object_id: str, owner: str, role: str, env: dict[str, str]
) -> None:
    cleanup = asyncio.create_task(
        asyncio.to_thread(remove_owned, "container", object_id, owner, role, env)
    )
    cancelled: asyncio.CancelledError | None = None
    while True:
        try:
            await asyncio.shield(cleanup)
            break
        except asyncio.CancelledError as error:
            cancelled = error
            if cleanup.done():
                break
    try:
        cleanup.result()
    except BaseException as cleanup_error:
        raise RuntimeError(
            f"exact Docker cleanup is unresolved for container identity {object_id!r}: {cleanup_error}"
        ) from cleanup_error
    if cancelled is not None:
        raise cancelled


async def _remove_created(container: DockerObject, env: dict[str, str]) -> None:
    await _remove_exact_id(
        container.object_id, container.owner or "", container.role or "", env
    )


async def _cleanup_named_if_owned(
    name: str, owner: str, role: Literal["focused", "quality"], env: dict[str, str]
) -> None:
    existing = await asyncio.to_thread(inspect, "container", name, env)
    if existing is None:
        return
    if existing.owner != owner or existing.role != role:
        raise RuntimeError(f"refusing to clean foreign Docker container {name!r}")
    await _remove_exact_id(existing.object_id, owner, role, env)


async def _create_owned_container(
    arguments: list[str], owner: str, role: Literal["focused", "quality"]
) -> DockerObject:
    env = _environment()
    name = owned_name(owner, role)
    await asyncio.to_thread(require_absent, "container", name, env)
    process = await asyncio.create_subprocess_exec(
        "docker",
        *arguments,
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), CREATE_SECONDS)
    except BaseException:
        try:
            await _stop_process(process)
        finally:
            await _cleanup_named_if_owned(name, owner, role, env)
        raise
    if process.returncode != 0:
        await _cleanup_named_if_owned(name, owner, role, env)
        detail = (stderr or stdout).decode(errors="replace").strip()
        raise RuntimeError(f"Docker {role} create failed: {detail or 'no diagnostic output'}")
    values = stdout.decode(errors="replace").strip().splitlines()
    if not values:
        await _cleanup_named_if_owned(name, owner, role, env)
        raise RuntimeError(f"Docker {role} create returned no container identity")
    container_id = values[-1]
    try:
        existing = await asyncio.to_thread(inspect, "container", container_id, env)
    except BaseException as readback_error:
        try:
            await _remove_exact_id(container_id, owner, role, env)
        except (RuntimeError, asyncio.CancelledError) as cleanup_error:
            raise RuntimeError(
                f"Docker {role} create readback failed and exact cleanup is unresolved: "
                f"{cleanup_error}"
            ) from readback_error
        if isinstance(readback_error, asyncio.CancelledError):
            raise
        raise RuntimeError(f"Docker {role} create readback failed after exact cleanup") from readback_error
    if (
        existing is None
        or existing.object_id != container_id
        or existing.name != name
        or existing.owner != owner
        or existing.role != role
    ):
        if existing is not None and existing.owner == owner and existing.role == role:
            await _remove_exact_id(container_id, owner, role, env)
        raise RuntimeError(f"Docker {role} create did not bind the exact owned container")
    return existing


async def _run_owned_workload(
    arguments: list[str], owner: str, role: Literal["focused", "quality"], timeout: int
) -> subprocess.CompletedProcess[str]:
    env = _environment()
    container = await _create_owned_container(arguments, owner, role)
    command = ["docker", "start", "--attach", container.object_id]
    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=subprocess.STDOUT,
            limit=WORKLOAD_OUTPUT_CHUNK_BYTES,
        )
    except BaseException:
        await _remove_created(container, env)
        raise
    try:
        text = await _wait_with_bounded_output(process, timeout)
    except BaseException:
        try:
            await _stop_process(process)
        finally:
            await _remove_created(container, env)
        raise
    try:
        finished = await asyncio.to_thread(inspect, "container", container.object_id, env)
    except BaseException as readback_error:
        try:
            await _remove_created(container, env)
        except (RuntimeError, asyncio.CancelledError) as cleanup_error:
            raise RuntimeError(
                f"Docker {role} daemon readback failed and exact cleanup is unresolved: "
                f"{cleanup_error}"
            ) from readback_error
        if isinstance(readback_error, asyncio.CancelledError):
            raise
        raise RuntimeError(f"Docker {role} daemon readback failed after exact cleanup") from readback_error
    if finished is None:
        raise RuntimeError(f"Docker {role} workload identity disappeared before daemon readback")
    if (
        finished.object_id != container.object_id
        or finished.owner != owner
        or finished.role != role
    ):
        raise RuntimeError(f"Docker {role} workload identity changed before daemon readback")
    if finished.running is not False or finished.status != "exited" or finished.exit_code is None:
        await _remove_created(container, env)
        raise RuntimeError(
            f"Docker {role} attach ended without an exited daemon workload; exact container removed"
        )
    returncode = finished.exit_code
    await _remove_created(container, env)
    return subprocess.CompletedProcess(command, returncode, stdout=text, stderr="")


def build_server(bound: bool = True) -> MCPServer:
    server = MCPServer("Switchstand Development Boundary")
    if not bound:
        return server
    workload_locks: dict[tuple[str, str], asyncio.Lock] = {}

    async def run_serialized(
        arguments: list[str],
        owner: str,
        role: Literal["focused", "quality"],
        timeout: int,
    ) -> subprocess.CompletedProcess[str]:
        lock = workload_locks.setdefault((owner, role), asyncio.Lock())
        async with lock:
            return await _run_owned_workload(arguments, owner, role, timeout)

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
            checked = await run_serialized(
                _container_arguments(repo, owner, "focused", paths),
                owner,
                "focused",
                FOCUSED_SECONDS,
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
            checked = await run_serialized(
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
