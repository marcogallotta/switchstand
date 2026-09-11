import argparse
import hashlib
import json
import os
import selectors
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, NamedTuple, cast
from uuid import UUID

from .run import RunReceipt, reserve_run
from .task_ref import asana_task_id

PROFILE = "switchstand-development"
AUTHORITY_NAMES = ("ACTIVE_WORK_ID", "REFERENCE_WORK_IDS")
MANAGED_NAME = "SWITCHSTAND_MANAGED"
START_PREPARED_NAME = "SWITCHSTAND_START_PREPARED"


class Authority(NamedTuple):
    active: UUID
    references: tuple[UUID, ...]


class CodexReadback(NamedTuple):
    profile: str
    sandbox: str
    instruction_sources: tuple[str, ...]


class DevelopmentBoundary(NamedTuple):
    image: str
    network: str
    database: str
    manifest: str


class PreparedRun(NamedTuple):
    authority: Authority
    development: DevelopmentBoundary
    receipt: RunReceipt


def linked_branch(repo: Path, env: dict[str, str]) -> str:
    completed = subprocess.run(
        [
            "git",
            "rev-parse",
            "--path-format=absolute",
            "--git-dir",
            "--git-common-dir",
            "HEAD",
            "--abbrev-ref",
            "HEAD",
        ],
        cwd=repo,
        env=env,
        check=True,
        text=True,
        capture_output=True,
    )
    git_value, common_value, head, branch = completed.stdout.splitlines()
    git_dir, common_dir = (Path(value).resolve(strict=True) for value in (git_value, common_value))
    if git_dir == common_dir:
        raise ValueError("managed launch requires a linked writer worktree")
    if branch == "HEAD":
        raise ValueError("managed launch requires a branch")
    green = (git_dir / "switchstand-green-sha").read_text().strip()
    dirty = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=repo,
        env=env,
        check=True,
        text=True,
        capture_output=True,
    ).stdout
    if head != green or dirty:
        raise ValueError("managed launch requires the exact clean green worktree baseline")
    return branch


def prepare_development(repo: Path, env: dict[str, str]) -> DevelopmentBoundary:
    suffix = hashlib.sha256(f"{repo}:{os.getpid()}".encode()).hexdigest()[:12]
    network, database = f"switchstand-dev-{suffix}", f"switchstand-test-{suffix}"
    image = subprocess.run(
        ["docker", "build", "--quiet", "--target", "development", "."],
        cwd=repo,
        env=env,
        check=True,
        text=True,
        capture_output=True,
    ).stdout.strip().splitlines()[-1]
    subprocess.run(
        ["docker", "network", "create", "--internal", network],
        env=env,
        check=True,
        capture_output=True,
    )
    try:
        subprocess.run(
            [
                "docker",
                "run",
                "-d",
                "--rm",
                "--name",
                database,
                "--network",
                network,
                "--network-alias",
                "postgres-test",
                "--tmpfs",
                "/var/lib/postgresql",
                "-e",
                "POSTGRES_DB=switchstand_test",
                "-e",
                "POSTGRES_USER=switchstand",
                "-e",
                "POSTGRES_PASSWORD=switchstand",
                "postgres:18-alpine",
            ],
            env=env,
            check=True,
            capture_output=True,
        )
        for _ in range(30):
            ready = subprocess.run(
                ["docker", "exec", database, "pg_isready", "-U", "switchstand"],
                env=env,
                capture_output=True,
                check=False,
            )
            if ready.returncode == 0:
                break
            time.sleep(1)
        else:
            raise RuntimeError("development test database did not become ready")
    except Exception:
        subprocess.run(
            ["docker", "rm", "-f", database], env=env, capture_output=True, check=False
        )
        subprocess.run(
            ["docker", "network", "rm", network], env=env, capture_output=True, check=False
        )
        raise
    digest = hashlib.sha256()
    for name in ("pyproject.toml", "uv.lock"):
        digest.update((repo / name).read_bytes())
    return DevelopmentBoundary(image, network, database, digest.hexdigest())


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        description="Bind human-readable work and start Codex inside the managed boundary."
    )
    result.add_argument("--active", required=True, help="active Asana task ID or URL")
    result.add_argument(
        "--reference", action="append", default=[], help="read-only Asana task ID or URL"
    )
    result.add_argument("codex_args", nargs=argparse.REMAINDER, help="arguments passed to Codex")
    return result


def clean_environment(source: dict[str, str]) -> dict[str, str]:
    return {
        name: value
        for name, value in source.items()
        if name != "ASANA_TOKEN"
        and name != MANAGED_NAME
        and name != START_PREPARED_NAME
        and name not in AUTHORITY_NAMES
        and not name.startswith("DOCKER_")
    }


def parse_authority(output: str) -> Authority:
    assignments: dict[str, str] = {}
    for line in output.splitlines():
        name, separator, value = line.partition("=")
        if name not in AUTHORITY_NAMES:
            continue
        if not separator or name in assignments:
            raise ValueError("provisioner returned an invalid authority response")
        assignments[name] = value
    if set(assignments) != set(AUTHORITY_NAMES):
        raise ValueError("provisioner returned an invalid authority response")
    references = tuple(
        UUID(value) for value in assignments["REFERENCE_WORK_IDS"].split(",") if value
    )
    return Authority(UUID(assignments["ACTIVE_WORK_ID"]), references)


def provision(repo: Path, active: str, references: tuple[str, ...], env: dict[str, str]) -> Authority:
    subprocess.run(
        ["docker", "compose", "-f", "compose.state.yaml", "up", "-d", "--wait", "postgres"],
        cwd=repo,
        env=env,
        check=True,
        capture_output=True,
    )
    command = [
        "docker",
        "compose",
        "run",
        "--build",
        "--rm",
        "--no-deps",
        "controller",
        "uv",
        "run",
        "--no-sync",
        "switchstand-provision",
        "--active",
        asana_task_id(active),
    ]
    for reference in references:
        command.extend(("--reference", asana_task_id(reference)))
    completed = subprocess.run(
        command, cwd=repo, env=env, check=True, text=True, capture_output=True
    )
    return parse_authority(completed.stdout)


def _rpc_messages(repo: Path, env: dict[str, str]) -> list[dict[str, Any]]:
    process = subprocess.Popen(
        [
            "codex",
            "-c",
            "mcp_servers.switchstand.enabled=false",
            "-c",
            "mcp_servers.switchstand_development.enabled=false",
            "app-server",
            "--listen",
            "stdio://",
        ],
        cwd=repo,
        env=env,
        text=True,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    if process.stdin is None or process.stdout is None:
        raise RuntimeError("failed to open Codex App Server pipes")
    stdin = process.stdin
    stdout = process.stdout
    selector = selectors.DefaultSelector()
    selector.register(stdout, selectors.EVENT_READ)

    def call(request_id: int, method: str, params: dict[str, Any]) -> dict[str, Any]:
        message = {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
        stdin.write(json.dumps(message) + "\n")
        stdin.flush()
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if not selector.select(deadline - time.monotonic()):
                break
            response = json.loads(stdout.readline())
            if response.get("id") == request_id:
                return cast(dict[str, Any], response)
        raise RuntimeError(f"Codex App Server did not answer {method}")

    try:
        initialized = call(
            1, "initialize", {"clientInfo": {"name": "switchstand-launch", "version": "2"}}
        )
        stdin.write('{"jsonrpc":"2.0","method":"initialized","params":{}}\n')
        stdin.flush()
        profiles = call(2, "permissionProfile/list", {"cwd": str(repo)})
        thread = call(
            3, "thread/start", {"cwd": str(repo), "ephemeral": True, "approvalPolicy": "never"}
        )
        return [initialized, profiles, thread]
    finally:
        selector.close()
        process.terminate()
        process.wait(timeout=5)


def readback(repo: Path, env: dict[str, str]) -> CodexReadback:
    responses = {message.get("id"): message for message in _rpc_messages(repo, env)}
    profiles = cast(list[dict[str, object]], responses[2]["result"]["data"])
    if not any(item["id"] == PROFILE and item["allowed"] for item in profiles):
        raise RuntimeError(f"Codex permission profile {PROFILE!r} is not available")
    result = cast(dict[str, Any], responses[3]["result"])
    active = cast(dict[str, object] | None, result.get("activePermissionProfile"))
    sources = tuple(cast(list[str], result.get("instructionSources", ())))
    sandbox_data = cast(dict[str, object], result["sandbox"])
    sandbox = cast(str, sandbox_data["type"])
    if active is None or active.get("id") != PROFILE:
        raise RuntimeError(f"Codex selected an unexpected permission profile: {active!r}")
    if sandbox != "workspaceWrite" or sandbox_data.get("networkAccess") is not False:
        raise RuntimeError(f"Codex selected an unexpected sandbox: {sandbox_data!r}")
    if result.get("approvalPolicy") != "never":
        raise RuntimeError(f"Codex selected an unexpected approval policy: {result.get('approvalPolicy')!r}")
    declared = {str(Path.home() / ".codex/AGENTS.md"), str(repo / "AGENTS.md")}
    if not sources or set(sources) != declared:
        raise RuntimeError(f"Codex loaded undeclared instruction sources: {sources!r}")
    return CodexReadback(PROFILE, sandbox, sources)


def validate_codex_args(arguments: list[str]) -> list[str]:
    forwarded = arguments[1:] if arguments[:1] == ["--"] else arguments
    if len(forwarded) > 1 or any(argument.startswith("-") for argument in forwarded):
        raise ValueError("managed launch accepts at most one prompt and no Codex options")
    return forwarded


def codex_command(repo: Path, codex_args: list[str]) -> list[str]:
    return [
        "codex",
        "-C",
        str(repo),
        "-a",
        "never",
        "-c",
        f'default_permissions="{PROFILE}"',
        "-c",
        "mcp_servers.switchstand.required=true",
        "-c",
        "mcp_servers.switchstand_development.required=true",
        *codex_args,
    ]


def prepare_managed_run(
    repo: Path,
    branch: str,
    active: str,
    references: tuple[str, ...],
    env: dict[str, str],
    git_dir: Path,
) -> PreparedRun:
    with reserve_run(repo, branch, git_dir) as record:
        authority = provision(repo, active, references, env)
        development = prepare_development(repo, env)
        receipt = record(authority.active)
    return PreparedRun(authority, development, receipt)


def mark_start_prepared(path: str | None) -> None:
    if path is None:
        return
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags, 0o600)
    try:
        os.write(descriptor, b"prepared\n")
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def run(arguments: argparse.Namespace) -> None:
    repo = Path.cwd().resolve()
    prepared_marker = os.environ.get(START_PREPARED_NAME)
    env = clean_environment(dict(os.environ))
    codex_args = validate_codex_args(arguments.codex_args)
    branch = linked_branch(repo, env)
    checked = readback(repo, env)
    git_dir = Path(
        subprocess.run(
            ["git", "rev-parse", "--absolute-git-dir"],
            cwd=repo,
            env=env,
            check=True,
            text=True,
            capture_output=True,
        ).stdout.strip()
    ).resolve(strict=True)
    prepared = prepare_managed_run(
        repo, branch, arguments.active, tuple(arguments.reference), env, git_dir
    )
    authority, development, receipt = prepared
    env["ACTIVE_WORK_ID"] = str(authority.active)
    env["REFERENCE_WORK_IDS"] = ",".join(map(str, authority.references))
    env["SWITCHSTAND_MANAGED"] = "1"
    env["SWITCHSTAND_WORKTREE"] = str(repo)
    env["SWITCHSTAND_BRANCH"] = branch
    env["SWITCHSTAND_GIT_COMMON"] = str(
        Path(
            subprocess.run(
                ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
                cwd=repo,
                env=env,
                check=True,
                text=True,
                capture_output=True,
            ).stdout.strip()
        ).resolve()
    )
    env["SWITCHSTAND_QUALITY_IMAGE"] = development.image
    env["SWITCHSTAND_QUALITY_NETWORK"] = development.network
    env["SWITCHSTAND_DATABASE_CONTAINER"] = development.database
    env["SWITCHSTAND_MANIFEST_SHA256"] = development.manifest
    print(f"Codex profile: {checked.profile} ({checked.sandbox})", file=sys.stderr)
    print(f"Run: {receipt.run_id}", file=sys.stderr)
    print("Instruction sources: " + ", ".join(checked.instruction_sources), file=sys.stderr)
    command = codex_command(repo, codex_args)
    mark_start_prepared(prepared_marker)
    os.execvpe(command[0], command, env)


def main() -> None:
    arguments = parser().parse_args()
    try:
        run(arguments)
    except (KeyError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        parser().exit(1, f"launch failed: {error}\n")


if __name__ == "__main__":
    main()
