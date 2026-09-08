import argparse
import json
import os
import selectors
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast
from uuid import UUID

from .task_ref import asana_task_id

PROFILE = "switchstand-development"
AUTHORITY_NAMES = ("ACTIVE_WORK_ID", "REFERENCE_WORK_IDS")
FORBIDDEN_CODEX_OPTIONS = frozenset(
    {
        "--add-dir",
        "--cd",
        "--config",
        "--dangerously-bypass-approvals-and-sandbox",
        "--ignore-user-config",
        "--profile",
        "--sandbox",
        "-C",
        "-c",
        "-p",
        "-s",
    }
)


@dataclass(frozen=True)
class Authority:
    active: UUID
    references: tuple[UUID, ...]


@dataclass(frozen=True)
class CodexReadback:
    profile: str
    sandbox: str
    instruction_sources: tuple[str, ...]


def git_permission_override(git_common_dir: Path) -> str:
    roots = "{" + json.dumps(str(git_common_dir)) + "=true}"
    return f"permissions.{PROFILE}.workspace_roots={roots}"


def find_git_common_dir(repo: Path, env: dict[str, str]) -> Path:
    completed = subprocess.run(
        ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
        cwd=repo,
        env=env,
        check=True,
        text=True,
        capture_output=True,
    )
    return Path(completed.stdout.strip()).resolve(strict=True)


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
        if name != "ASANA_TOKEN" and name not in AUTHORITY_NAMES
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
    command = [
        "docker",
        "compose",
        "run",
        "--build",
        "--rm",
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


def _rpc_messages(
    repo: Path, git_common_dir: Path, env: dict[str, str]
) -> list[dict[str, Any]]:
    process = subprocess.Popen(
        [
            "codex",
            "-c",
            "mcp_servers.switchstand.enabled=false",
            "-c",
            git_permission_override(git_common_dir),
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
        thread = call(3, "thread/start", {"cwd": str(repo), "ephemeral": True})
        return [initialized, profiles, thread]
    finally:
        selector.close()
        process.terminate()
        process.wait(timeout=5)


def readback(repo: Path, git_common_dir: Path, env: dict[str, str]) -> CodexReadback:
    responses = {
        message.get("id"): message for message in _rpc_messages(repo, git_common_dir, env)
    }
    profiles = cast(list[dict[str, object]], responses[2]["result"]["data"])
    if not any(item["id"] == PROFILE and item["allowed"] for item in profiles):
        raise RuntimeError(f"Codex permission profile {PROFILE!r} is not available")
    result = cast(dict[str, Any], responses[3]["result"])
    active = cast(dict[str, object] | None, result.get("activePermissionProfile"))
    sources = tuple(cast(list[str], result.get("instructionSources", ())))
    sandbox = cast(str, result["sandbox"]["type"])
    if active is None or active.get("id") != PROFILE:
        raise RuntimeError(f"Codex selected an unexpected permission profile: {active!r}")
    if sandbox == "dangerFullAccess":
        raise RuntimeError("Codex selected danger-full-access")
    if not sources or any(Path(source).name != "AGENTS.md" for source in sources):
        raise RuntimeError(f"Codex loaded undeclared instruction sources: {sources!r}")
    return CodexReadback(PROFILE, sandbox, sources)


def validate_codex_args(arguments: list[str]) -> list[str]:
    forwarded = arguments[1:] if arguments[:1] == ["--"] else arguments
    for argument in forwarded:
        option = argument.split("=", 1)[0]
        if option in FORBIDDEN_CODEX_OPTIONS:
            raise ValueError(f"managed launch forbids Codex option {option}")
    return forwarded


def run(arguments: argparse.Namespace) -> None:
    repo = Path.cwd().resolve()
    env = clean_environment(dict(os.environ))
    codex_args = validate_codex_args(arguments.codex_args)
    git_common_dir = find_git_common_dir(repo, env)
    authority = provision(repo, arguments.active, tuple(arguments.reference), env)
    env["ACTIVE_WORK_ID"] = str(authority.active)
    env["REFERENCE_WORK_IDS"] = ",".join(map(str, authority.references))
    checked = readback(repo, git_common_dir, env)
    print(f"Codex profile: {checked.profile} ({checked.sandbox})", file=sys.stderr)
    print("Instruction sources: " + ", ".join(checked.instruction_sources), file=sys.stderr)
    command = [
        "codex",
        "-C",
        str(repo),
        "-a",
        "never",
        "-c",
        f'default_permissions="{PROFILE}"',
        "-c",
        git_permission_override(git_common_dir),
        *codex_args,
    ]
    os.execvpe(command[0], command, env)


def main() -> None:
    arguments = parser().parse_args()
    try:
        run(arguments)
    except (KeyError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        parser().exit(1, f"launch failed: {error}\n")


if __name__ == "__main__":
    main()
