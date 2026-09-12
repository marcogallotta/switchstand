"""Trusted host launcher for one exact, disposable MCP qualification candidate."""

import argparse
import asyncio
import hashlib
import os
import re
import stat
import subprocess
import sys
import tempfile
import time
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from mcp.client.client import Client
from mcp.client.stdio import StdioServerParameters

from .launch import CodexReadback, clean_environment, readback, supervise_process
from .task_ref import asana_task_id

PROFILE = "switchstand-qualification"
TOOLS = frozenset(
    {"grant_get", "work_get", "source_task", "source_stories", "source_story", "work_append"}
)
AUTHORITY_ARGUMENTS = frozenset(
    {"principal", "role", "grant", "grant_id", "issuer", "allowed_operations"}
)
CLIENT_ENVIRONMENT_NAMES = ("PATH", "HOME", "LANG", "LC_ALL", "TERM", "COLORTERM", "TZ")


@dataclass(frozen=True)
class QualificationBoundary:
    primary: Path
    candidate_repo: Path
    candidate: str
    accepted: str
    image: str
    image_tag: str
    network: str
    database: str
    server: str
    active: str
    approved: tuple[str, ...]


@dataclass(frozen=True)
class ToolReadback:
    active_work_id: str
    reference_work_ids: tuple[str, ...]


def exact_sha(value: str) -> str:
    if re.fullmatch(r"[0-9a-f]{40}", value) is None:
        raise argparse.ArgumentTypeError("candidate must be exactly 40 lowercase hexadecimal characters")
    return value


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        description="Qualify one exact reviewed candidate through a disposable host boundary."
    )
    result.add_argument("--candidate", required=True, type=exact_sha)
    result.add_argument("--active", required=True, type=asana_task_id)
    result.add_argument(
        "--approved",
        action="append",
        required=True,
        type=asana_task_id,
        help="approved read-only Asana task ID or URL (repeatable, maximum eight)",
    )
    result.add_argument("prompt", nargs="?", help="optional qualification request")
    return result


def qualification_environment(source: dict[str, str]) -> dict[str, str]:
    cleaned = clean_environment(source)
    return {name: cleaned[name] for name in CLIENT_ENVIRONMENT_NAMES if name in cleaned}


def _run(
    command: list[str],
    *,
    cwd: Path | None,
    env: dict[str, str],
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command, cwd=cwd, env=env, check=check, text=True, capture_output=True
    )


def _git(
    repo: Path, *arguments: str, env: dict[str, str], check: bool = True
) -> subprocess.CompletedProcess[str]:
    return _run(["git", *arguments], cwd=repo, env=env, check=check)


def validate_primary(repo: Path, env: dict[str, str]) -> str:
    values = _git(
        repo,
        "rev-parse",
        "--path-format=absolute",
        "--show-toplevel",
        "HEAD",
        "origin/main",
        "--abbrev-ref",
        "HEAD",
        env=env,
    ).stdout.splitlines()
    if len(values) != 4:
        raise RuntimeError("qualification could not resolve the primary checkout")
    root, head, accepted, branch = values
    dirty = _git(repo, "status", "--porcelain", env=env).stdout
    if Path(root).resolve() != repo or branch != "main":
        raise RuntimeError("qualification requires the ordinary main checkout")
    if head != accepted or dirty:
        raise RuntimeError("qualification requires clean main at the locally accepted origin/main")
    return accepted


def validate_request(candidate: str, active: str, approved: tuple[str, ...]) -> None:
    exact_sha(candidate)
    if not approved or len(approved) > 8:
        raise ValueError("qualification requires one to eight approved references")
    if active in approved or len(set(approved)) != len(approved):
        raise ValueError("active and approved task references must be distinct")


def validate_host_config(env: dict[str, str]) -> Path:
    path = Path(env["HOME"]) / ".config/switchstand/.env"
    metadata = path.lstat()
    if path.is_symlink() or not stat.S_ISREG(metadata.st_mode):
        raise RuntimeError("protected Switchstand host configuration must be a regular file")
    if metadata.st_uid != os.geteuid() or stat.S_IMODE(metadata.st_mode) & 0o077:
        raise RuntimeError("protected Switchstand host configuration must be owner-only")
    return path


def validate_candidate(
    primary: Path, candidate_repo: Path, candidate: str, accepted: str, env: dict[str, str]
) -> None:
    exists = _git(primary, "cat-file", "-e", f"{candidate}^{{commit}}", env=env, check=False)
    ancestor = _git(
        primary, "merge-base", "--is-ancestor", accepted, candidate, env=env, check=False
    )
    if exists.returncode or ancestor.returncode:
        raise RuntimeError("candidate must be an available commit based on accepted main")
    head = _git(candidate_repo, "rev-parse", "HEAD", env=env).stdout.strip()
    dirty = _git(candidate_repo, "status", "--porcelain", env=env).stdout
    branch = _git(candidate_repo, "branch", "--show-current", env=env).stdout.strip()
    required = (
        "scripts/chatgpt-mcp-canary.py",
        "src/switchstand/chatgpt_mcp.py",
        "migrations/versions/0002_grants_and_effects.py",
    )
    if head != candidate or branch or dirty or any(not (candidate_repo / name).is_file() for name in required):
        raise RuntimeError("qualification candidate is stale, dirty, attached, or incomplete")


def _docker(
    arguments: list[str], env: dict[str, str], cwd: Path | None = None, *, check: bool = True
) -> subprocess.CompletedProcess[str]:
    return _run(["docker", *arguments], cwd=cwd, env=env, check=check)


def _resource_names(primary: Path, candidate: str) -> tuple[str, str, str, str]:
    suffix = hashlib.sha256(f"{primary}:{candidate}:{os.getpid()}".encode()).hexdigest()[:12]
    return (
        f"switchstand-qualification:{suffix}",
        f"switchstand-qualification-net-{suffix}",
        f"switchstand-qualification-db-{suffix}",
        f"switchstand-qualification-mcp-{suffix}",
    )


def prepare_boundary(
    primary: Path,
    candidate: str,
    active: str,
    approved: tuple[str, ...],
    env: dict[str, str],
) -> QualificationBoundary:
    validate_request(candidate, active, approved)
    accepted = validate_primary(primary, env)
    validate_host_config(env)
    image_tag, network, database, server = _resource_names(primary, candidate)
    target = Path(tempfile.gettempdir()) / f"switchstand-qualification-{os.getpid()}-{candidate[:12]}"
    if target.exists():
        raise RuntimeError(f"qualification worktree target already exists: {target}")
    boundary = QualificationBoundary(
        primary, target, candidate, accepted, "", image_tag, network, database, server,
        active, approved,
    )
    try:
        _git(primary, "worktree", "add", "--detach", str(target), candidate, env=env)
        validate_candidate(primary, target, candidate, accepted, env)
        _docker(
            [
                "build", "--quiet", "--target", "development", "--label",
                f"org.opencontainers.image.revision={candidate}", "--tag", image_tag, ".",
            ],
            env,
            target,
        )
        inspected = _docker(
            [
                "image", "inspect", "--format",
                "{{.Id}} {{index .Config.Labels \"org.opencontainers.image.revision\"}}",
                image_tag,
            ],
            env,
        ).stdout.split()
        if len(inspected) != 2 or inspected[1] != candidate or not inspected[0].startswith("sha256:"):
            raise RuntimeError("built qualification image did not read back the exact candidate")
        boundary = QualificationBoundary(
            primary, target, candidate, accepted, inspected[0], image_tag, network, database,
            server, active, approved,
        )
        _docker(
            ["network", "create", "--label", f"switchstand.candidate={candidate}", network], env
        )
        _docker(
            [
                "run", "-d", "--rm", "--name", database, "--network", network,
                "--tmpfs", "/var/lib/postgresql",
                "-e", "POSTGRES_DB=switchstand_test", "-e", "POSTGRES_USER=switchstand",
                "-e", "POSTGRES_PASSWORD=switchstand", "postgres:18-alpine",
            ],
            env,
        )
        for _ in range(30):
            ready = _docker(
                ["exec", database, "pg_isready", "-U", "switchstand", "-d", "switchstand_test"],
                env,
                check=False,
            )
            if ready.returncode == 0:
                break
            time.sleep(1)
        else:
            raise RuntimeError("disposable qualification database did not become ready")
        validate_candidate(primary, target, candidate, accepted, env)
        return boundary
    except BaseException:
        cleanup_boundary(boundary, env)
        raise


def boundary_environment(
    source: dict[str, str], boundary: QualificationBoundary
) -> dict[str, str]:
    env = qualification_environment(source)
    env.update(
        {
            "SWITCHSTAND_QUALIFICATION": "1",
            "SWITCHSTAND_QUALIFICATION_IMAGE": boundary.image,
            "SWITCHSTAND_QUALIFICATION_NETWORK": boundary.network,
            "SWITCHSTAND_QUALIFICATION_DATABASE": boundary.database,
            "SWITCHSTAND_QUALIFICATION_SERVER": boundary.server,
            "SWITCHSTAND_QUALIFICATION_ACTIVE": boundary.active,
            "SWITCHSTAND_QUALIFICATION_APPROVED": ",".join(boundary.approved),
        }
    )
    return env


def _absent(detail: str) -> bool:
    lowered = detail.lower()
    return "no such" in lowered or "not found" in lowered


def cleanup_boundary(
    boundary: QualificationBoundary, env: dict[str, str], *, required: bool = True
) -> None:
    failures: list[str] = []
    for resource, command, inspect in (
        (boundary.server, ["rm", "-f", boundary.server], ["container", "inspect", boundary.server]),
        (
            boundary.database,
            ["rm", "-f", boundary.database],
            ["container", "inspect", boundary.database],
        ),
        (
            boundary.network,
            ["network", "rm", boundary.network],
            ["network", "inspect", boundary.network],
        ),
        (
            boundary.image_tag,
            ["image", "rm", boundary.image_tag],
            ["image", "inspect", boundary.image_tag],
        ),
    ):
        try:
            removed = _docker(command, env, check=False)
            detail = (removed.stderr or removed.stdout).strip()
            if removed.returncode and not _absent(detail):
                failures.append(f"{resource}: {detail or 'removal failed'}")
            if _docker(inspect, env, check=False).returncode == 0:
                failures.append(f"{resource}: still present after cleanup")
        except OSError as error:
            failures.append(f"{resource}: cleanup could not be inspected: {error}")
    if boundary.candidate_repo.exists():
        try:
            validate_candidate(
                boundary.primary,
                boundary.candidate_repo,
                boundary.candidate,
                boundary.accepted,
                env,
            )
            _git(
                boundary.primary,
                "worktree",
                "remove",
                str(boundary.candidate_repo),
                env=env,
            )
        except (OSError, RuntimeError, subprocess.SubprocessError) as error:
            failures.append(f"{boundary.candidate_repo}: {error}")
    listed = _git(boundary.primary, "worktree", "list", "--porcelain", env=env, check=False)
    if boundary.candidate_repo.exists() or str(boundary.candidate_repo) in listed.stdout:
        failures.append(f"{boundary.candidate_repo}: worktree cleanup not proven")
    if failures and required:
        raise RuntimeError("qualification cleanup failed: " + "; ".join(failures))


@contextmanager
def prepared_boundary(
    primary: Path,
    candidate: str,
    active: str,
    approved: tuple[str, ...],
    env: dict[str, str],
) -> Generator[QualificationBoundary]:
    boundary = prepare_boundary(primary, candidate, active, approved, env)
    try:
        yield boundary
    finally:
        cleanup_boundary(boundary, env)


def _content(result: object) -> dict[str, Any]:
    content = cast(Any, result).structured_content
    if not isinstance(content, dict):
        raise TypeError("qualification server returned no structured readback")
    return cast(dict[str, Any], content)


async def _inspect_tools(
    wrapper: Path, env: dict[str, str], active: str, approved: tuple[str, ...]
) -> ToolReadback:
    parameters = StdioServerParameters(command=str(wrapper), env=env)
    async with Client(parameters) as client:
        tools = (await client.list_tools()).tools
        if frozenset(tool.name for tool in tools) != TOOLS:
            raise RuntimeError("qualification server exposed an unexpected tool inventory")
        for tool in tools:
            schema = tool.input_schema
            properties = set(schema.get("properties", {}))
            if schema.get("additionalProperties") is not False or properties & AUTHORITY_ARGUMENTS:
                raise RuntimeError(f"qualification tool {tool.name!r} accepts authority input")
        grant_result = _content(await client.call_tool("grant_get", {"api_version": "1"}))
        grant_value = grant_result.get("grant")
        principal_value = grant_result.get("principal")
        if (
            grant_result.get("status") != "ok"
            or not isinstance(grant_value, dict)
            or not isinstance(principal_value, dict)
        ):
            raise RuntimeError("qualification grant readback failed")
        grant = cast(dict[str, Any], grant_value)
        principal = cast(dict[str, Any], principal_value)
        if (
            principal.get("assurance") != "test"
            or principal.get("subject") != active
            or set(cast(list[str], grant.get("operations", []))) != {"work_get", "work_append"}
            or grant.get("append_qualification") != f"test:disposable-task:{active}"
        ):
            raise RuntimeError("qualification server returned unexpected principal or operations")
        authority = cast(dict[str, Any], grant["authority"])
        active_work_id = str(authority["active_work_id"])
        reference_work_ids = tuple(map(str, authority["reference_work_ids"]))
        selected = (active_work_id, *reference_work_ids)
        if len(reference_work_ids) != len(approved) or len(set(selected)) != len(selected):
            raise RuntimeError("qualification grant did not bind all approved references")
        expected = (active, *approved)
        for work_id, task_id in zip(selected, expected, strict=True):
            arguments: dict[str, str] = {"api_version": "1"}
            if work_id != active_work_id or task_id != active:
                arguments["work_id"] = work_id
            result = _content(await client.call_tool("work_get", arguments))
            item_value = result.get("item")
            item = cast(dict[str, Any], item_value) if isinstance(item_value, dict) else None
            source_value = item.get("source") if item is not None else None
            source = (
                cast(dict[str, Any], source_value) if isinstance(source_value, dict) else None
            )
            if (
                result.get("status") != "ok"
                or not isinstance(source, dict)
                or source.get("task_gid") != task_id
                or work_id == task_id
            ):
                raise RuntimeError("qualification authority/source readback did not match the request")
        return ToolReadback(active_work_id, reference_work_ids)


def inspect_tools(
    wrapper: Path, env: dict[str, str], active: str, approved: tuple[str, ...]
) -> ToolReadback:
    return asyncio.run(asyncio.wait_for(_inspect_tools(wrapper, env, active, approved), 120))


def codex_command(repo: Path, candidate: str, prompt: str | None) -> list[str]:
    request = (
        "Start the explicit host-bound Switchstand qualification. Call grant_get and then "
        "work_get without a WorkId; use only the six launch-bound qualification tools. Follow "
        "the active task's exact qualification instructions, preserving OperationId replay and "
        "UNKNOWN. Do not seek credentials, Docker, another provider path, or broader authority. "
        f"The server image is pinned to candidate {candidate}."
    )
    if prompt is not None:
        request += "\n\nAdditional qualification request:\n" + prompt
    return [
        "codex", "-C", str(repo), "-a", "never", "-c", f'default_permissions="{PROFILE}"',
        "-c", "mcp_servers.switchstand.enabled=false",
        "-c", "mcp_servers.switchstand_development.enabled=false",
        "-c", "mcp_servers.switchstand_qualification.enabled=true",
        "-c", "mcp_servers.switchstand_qualification.required=true", request,
    ]


def run(arguments: argparse.Namespace) -> None:
    primary = Path.cwd().resolve()
    host_env = qualification_environment(dict(os.environ))
    approved = tuple(arguments.approved)
    validate_request(arguments.candidate, arguments.active, approved)
    checked: CodexReadback = readback(
        primary, host_env, profile=PROFILE, sandbox="readOnly"
    )
    wrapper = primary / "scripts/switchstand-qualification-mcp"
    with prepared_boundary(
        primary, arguments.candidate, arguments.active, approved, host_env
    ) as boundary:
        client_env = boundary_environment(dict(os.environ), boundary)
        inventory = inspect_tools(wrapper, client_env, arguments.active, approved)
        validate_candidate(primary, boundary.candidate_repo, boundary.candidate, boundary.accepted,
                           host_env)
        print(f"Codex profile: {checked.profile} ({checked.sandbox})", file=sys.stderr)
        print(f"Candidate: {boundary.candidate} ({boundary.image})", file=sys.stderr)
        print(f"Active WorkId: {inventory.active_work_id}", file=sys.stderr)
        print("Qualification tools: " + ", ".join(sorted(TOOLS)), file=sys.stderr)
        command = codex_command(primary, boundary.candidate, arguments.prompt)
        raise SystemExit(supervise_process(command, client_env, lambda: None))


def main() -> None:
    arguments = parser().parse_args()
    try:
        run(arguments)
    except (KeyError, OSError, TypeError, ValueError, RuntimeError,
            subprocess.SubprocessError) as error:
        parser().exit(1, f"qualification launch failed: {error}\n")


if __name__ == "__main__":
    main()
