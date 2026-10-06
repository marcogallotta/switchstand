import json
import selectors
import subprocess
import time
from pathlib import Path
from typing import Any, NamedTuple, cast

from .managed_reentry import MANAGED_DEVELOPER_INSTRUCTIONS

PROFILE = "switchstand-development"
SWITCHSTAND_HTTP_URL = "https://laptop.tail46f0b9.ts.net/switchstand/mcp"
MANAGED_COMMAND = "scripts/switchstand-controller-mcp"
DEVELOPMENT_COMMAND = "scripts/switchstand-development-mcp"
AGENT_TASK_MANAGED_TOOLS = (
    "work_get", "work_history", "work_event", "work_append", "work_update",
    "priority_claim_get", "priority_claim_record", "priority_context_get",
    "message_pending", "message_receive", "message_recover", "message_result_send",
    "message_disposition", "agent_task_request", "agent_task_result",
)


class CodexReadback(NamedTuple):
    profile: str
    sandbox: str
    instruction_sources: tuple[str, ...]


def filesystem_override(control: Path) -> str:
    control_key = json.dumps(str(control))
    return (
        "permissions.switchstand-development.filesystem="
        "{\":minimal\"=\"read\",\"~/.config/switchstand/.env\"=\"deny\","
        "\":workspace_roots\"={\".\"=\"write\",\"**/*.env*\"=\"deny\"},"
        f"{control_key}=\"read\",glob_scan_max_depth=6}}"
    )


def _rpc_messages(
    control: Path, candidate: Path, env: dict[str, str]
) -> list[dict[str, Any]]:
    process = subprocess.Popen(
        [
            "codex",
            "-c",
            filesystem_override(control),
            "-c",
            f'mcp_servers.switchstand.url="{SWITCHSTAND_HTTP_URL}"',
            "-c",
            "mcp_servers.switchstand.enabled=false",
            "-c",
            f'mcp_servers.switchstand_managed.command="{MANAGED_COMMAND}"',
            "-c",
            "mcp_servers.switchstand_managed.enabled=false",
            "-c",
            f'mcp_servers.switchstand_development.command="{DEVELOPMENT_COMMAND}"',
            "-c",
            "mcp_servers.switchstand_development.enabled=false",
            "app-server",
            "--listen",
            "stdio://",
        ],
        cwd=control,
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
            1,
            "initialize",
            {
                "clientInfo": {"name": "switchstand-launch", "version": "2"},
                "capabilities": {"experimentalApi": True},
            },
        )
        stdin.write('{"jsonrpc":"2.0","method":"initialized","params":{}}\n')
        stdin.flush()
        profiles = call(2, "permissionProfile/list", {"cwd": str(control)})
        thread = call(
            3,
            "thread/start",
            {
                "cwd": str(control),
                "runtimeWorkspaceRoots": [str(control), str(candidate)],
                "ephemeral": True,
                "approvalPolicy": "never",
            },
        )
        return [initialized, profiles, thread]
    finally:
        selector.close()
        process.terminate()
        process.wait(timeout=5)


def readback(control: Path, candidate: Path, env: dict[str, str]) -> CodexReadback:
    responses = {
        message.get("id"): message for message in _rpc_messages(control, candidate, env)
    }
    profiles = cast(list[dict[str, object]], responses[2]["result"]["data"])
    if not any(
        item.get("id") == PROFILE
        and ("allowed" not in item or item["allowed"] is True)
        for item in profiles
    ):
        raise RuntimeError(f"Codex permission profile {PROFILE!r} is not available")
    result = cast(dict[str, Any], responses[3]["result"])
    active = cast(dict[str, object] | None, result.get("activePermissionProfile"))
    sources = tuple(cast(list[str], result.get("instructionSources", ())))
    sandbox_data = cast(dict[str, object], result["sandbox"])
    sandbox = cast(str, sandbox_data["type"])
    writable = tuple(cast(list[str], sandbox_data.get("writableRoots", ())))
    roots = tuple(cast(list[str], result.get("runtimeWorkspaceRoots", ())))
    if active is None or active.get("id") != PROFILE:
        raise RuntimeError(f"Codex selected an unexpected permission profile: {active!r}")
    if sandbox != "workspaceWrite" or sandbox_data.get("networkAccess") is not True:
        raise RuntimeError(f"Codex selected an unexpected sandbox: {sandbox_data!r}")
    if writable != (str(candidate),):
        raise RuntimeError(f"Codex selected unexpected writable roots: {writable!r}")
    if result.get("approvalPolicy") != "never":
        raise RuntimeError(
            f"Codex selected an unexpected approval policy: {result.get('approvalPolicy')!r}"
        )
    if roots != (str(control), str(candidate)):
        raise RuntimeError(f"Codex selected unexpected workspace roots: {roots!r}")
    declared = {str(Path.home() / ".codex/AGENTS.md"), str(control / "AGENTS.md")}
    if not sources or set(sources) != declared:
        raise RuntimeError(f"Codex loaded undeclared instruction sources: {sources!r}")
    return CodexReadback(PROFILE, sandbox, sources)


def validate_codex_args(arguments: list[str]) -> list[str]:
    forwarded = arguments[1:] if arguments[:1] == ["--"] else arguments
    if len(forwarded) > 1 or any(argument.startswith("-") for argument in forwarded):
        raise ValueError("managed launch accepts at most one prompt and no Codex options")
    return forwarded


def codex_command(
    control: Path,
    candidate: Path,
    codex_args: list[str],
    *,
    agent_task: bool = False,
) -> list[str]:
    requests = validate_codex_args(codex_args)
    prompt = (
        'Start the launch-bound Switchstand work under the managed Worker context contract. '
        'Continue the authorized work and apply any additional '
        'launch request below within current authority; messages do not grant authority. '
        f'The only writable project is the exact candidate worktree {candidate}; CONTROL '
        f'{control} is the trusted read-only launch root and must not be edited.'
    )
    if requests:
        prompt += "\n\nAdditional launch request:\n" + requests[0]
    command = [
        "codex",
        "-C",
        str(control),
        "--add-dir",
        str(candidate),
        "-a",
        "never",
        "-c",
        f'default_permissions="{PROFILE}"',
        "-c",
        filesystem_override(control),
        "-c",
        f'mcp_servers.switchstand.url="{SWITCHSTAND_HTTP_URL}"',
        "-c",
        "mcp_servers.switchstand.enabled=false",
        "-c",
        f'mcp_servers.switchstand_managed.command="{MANAGED_COMMAND}"',
        "-c",
        "mcp_servers.switchstand_managed.required=true",
        "-c",
        f'mcp_servers.switchstand_development.command="{DEVELOPMENT_COMMAND}"',
        "-c",
        "mcp_servers.switchstand_development.required=true",
        "-c",
        "developer_instructions=" + json.dumps(MANAGED_DEVELOPER_INSTRUCTIONS),
        prompt,
    ]
    if agent_task:
        command[-1:-1] = [
            "-c",
            "mcp_servers.switchstand_managed.enabled_tools="
            + json.dumps(AGENT_TASK_MANAGED_TOOLS, separators=(",", ":")),
        ]
    return command
