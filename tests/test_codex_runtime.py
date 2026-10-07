import json
import os
import selectors
import sys
import tomllib
from pathlib import Path

import pytest

from switchstand.codex_runtime import (
    PROFILE,
    _rpc_messages,
    codex_command,
    filesystem_override,
    readback,
    validate_codex_args,
)
from switchstand.managed_reentry import MANAGED_DEVELOPER_INSTRUCTIONS


def test_real_stdio_app_server_boundary_returns_managed_profile(tmp_path: Path) -> None:
    control = tmp_path / "control"
    candidate = tmp_path / "candidate"
    bindir = tmp_path / "bin"
    control.mkdir()
    candidate.mkdir()
    bindir.mkdir()

    argv_path = tmp_path / "codex-argv.json"
    codex = bindir / "codex"
    codex.write_text(
        f"""#!{sys.executable}
import json
import os
import sys
from pathlib import Path

Path({str(argv_path)!r}).write_text(json.dumps(sys.argv[1:]))

for line in sys.stdin:
    request = json.loads(line)
    request_id = request.get("id")
    if request_id is None:
        continue
    method = request["method"]
    if method == "initialize":
        result = {{"serverInfo": {{"name": "fixture", "version": "1"}}}}
    elif method == "permissionProfile/list":
        result = {{"data": [{{"id": "{PROFILE}", "allowed": True}}]}}
    elif method == "thread/start":
        roots = request["params"]["runtimeWorkspaceRoots"]
        result = {{
            "activePermissionProfile": {{"id": "{PROFILE}"}},
            "sandbox": {{
                "type": "workspaceWrite",
                "networkAccess": True,
                "writableRoots": [roots[1]],
            }},
            "runtimeWorkspaceRoots": roots,
            "approvalPolicy": "never",
            "instructionSources": [
                str(Path.home() / ".codex/AGENTS.md"),
                str(Path(roots[0]) / "AGENTS.md"),
            ],
        }}
    else:
        raise SystemExit(f"unexpected method: {{method}}")
    print(json.dumps({{"jsonrpc": "2.0", "id": request_id, "result": result}}), flush=True)
"""
    )
    codex.chmod(0o755)
    env = dict(os.environ)
    env["PATH"] = str(bindir) + os.pathsep + env.get("PATH", "")

    result = readback(control, candidate, env)

    assert result.profile == PROFILE
    assert result.sandbox == "workspaceWrite"
    assert set(result.instruction_sources) == {
        str(Path.home() / ".codex/AGENTS.md"),
        str(control / "AGENTS.md"),
    }
    argv = json.loads(argv_path.read_text())
    for override in (
        'mcp_servers.switchstand.url="https://laptop.tail46f0b9.ts.net/switchstand/mcp"',
        "mcp_servers.switchstand.enabled=false",
        'mcp_servers.switchstand_managed.command="scripts/switchstand-controller-mcp"',
        "mcp_servers.switchstand_managed.enabled=false",
        'mcp_servers.switchstand_development.command="scripts/switchstand-development-mcp"',
        "mcp_servers.switchstand_development.enabled=false",
    ):
        assert override in argv
    assert argv[-3:] == ["app-server", "--listen", "stdio://"]


def test_validate_codex_args_blocks_boundary_overrides():
    assert validate_codex_args(["--", "do the work"]) == ["do the work"]
    for arguments in (["-sdanger-full-access"], ["-C/tmp"], ["-c", "sandbox_mode=read-only"]):
        with pytest.raises(ValueError):
            validate_codex_args(arguments)


def test_managed_codex_requires_both_mcp_servers():
    command = codex_command(Path("/control"), Path("/writer"), [])
    assert "mcp_servers.switchstand.enabled=false" in command
    assert "mcp_servers.switchstand_managed.required=true" in command
    assert "mcp_servers.switchstand_development.required=true" in command


def test_agent_task_tools_are_exposed_only_for_explicit_launch_opt_in():
    default = codex_command(Path("/control"), Path("/writer"), [])
    opted_in = codex_command(
        Path("/control"), Path("/writer"), [], agent_task=True
    )

    assert not any("agent_task_" in value for value in default)
    enabled = next(value for value in opted_in if "agent_task_request" in value)
    assert enabled.startswith("mcp_servers.switchstand_managed.enabled_tools=")
    assert '"agent_task_request","agent_task_result"' in enabled
    assert opted_in[-1] == default[-1]


def test_agent_task_opt_in_preserves_exact_managed_tool_baseline():
    config = tomllib.loads(
        (Path(__file__).parents[1] / ".codex/config.toml").read_text()
    )
    baseline = config["mcp_servers"]["switchstand_managed"]["enabled_tools"]
    command = codex_command(
        Path("/control"), Path("/writer"), [], agent_task=True
    )
    override = next(value for value in command if "agent_task_request" in value)
    enabled = json.loads(override.split("=", 1)[1])

    assert enabled == baseline + ["agent_task_request", "agent_task_result"]


def test_managed_codex_starts_work_without_a_manual_prompt():
    command = codex_command(Path("/control"), Path("/writer"), [])
    assert command[:-1] == [
        "codex", "-C", "/control", "--add-dir", "/writer", "-a", "never", "-c",
        f'default_permissions="{PROFILE}"',
        "-c", filesystem_override(Path("/control")),
        "-c", 'mcp_servers.switchstand.url="https://laptop.tail46f0b9.ts.net/switchstand/mcp"',
        "-c", "mcp_servers.switchstand.enabled=false",
        "-c", 'mcp_servers.switchstand_managed.command="scripts/switchstand-controller-mcp"',
        "-c", "mcp_servers.switchstand_managed.required=true",
        "-c", 'mcp_servers.switchstand_development.command="scripts/switchstand-development-mcp"',
        "-c", "mcp_servers.switchstand_development.required=true",
        "-c", "developer_instructions=" + json.dumps(MANAGED_DEVELOPER_INSTRUCTIONS),
    ]
    assert "Active inbox" not in command[-1]
    assert "managed Worker context contract" in command[-1]
    instructions = next(
        value for value in command if value.startswith("developer_instructions=")
    )
    assert instructions == "developer_instructions=" + json.dumps(
        MANAGED_DEVELOPER_INSTRUCTIONS
    )
    assert 'work_get(api_version="1") without a WorkId' in MANAGED_DEVELOPER_INSTRUCTIONS
    assert "after compaction" in MANAGED_DEVELOPER_INSTRUCTIONS
    assert "each exact open review/message/watch obligation" in MANAGED_DEVELOPER_INSTRUCTIONS
    assert "exact CURRENT package" in MANAGED_DEVELOPER_INSTRUCTIONS
    assert "missing or stale role/phase binding" in MANAGED_DEVELOPER_INSTRUCTIONS
    assert "only the affected path UNKNOWN" in MANAGED_DEVELOPER_INSTRUCTIONS
    assert "Never load the Root Coordinator tracking contract" in MANAGED_DEVELOPER_INSTRUCTIONS
    assert "Markdown dependency graph" in MANAGED_DEVELOPER_INSTRUCTIONS
    assert not any(value.startswith("hooks.SessionStart=") for value in command)


@pytest.mark.parametrize("launch_request", ["", "inspect only", "Stop.\nDo not edit.\n`$HOME` 'quoted'"])
def test_managed_codex_preserves_launch_request_in_one_prompt(launch_request):
    default = codex_command(Path("/control"), Path("/writer"), [])
    command = codex_command(
        Path("/control"), Path("/writer"), validate_codex_args(["--", launch_request])
    )
    assert command[:-1] == default[:-1]
    prefix, supplied = command[-1].split("\n\nAdditional launch request:\n", 1)
    assert prefix == default[-1]
    assert supplied == launch_request


@pytest.mark.parametrize("arguments", [["--config=unsafe"], ["first", "second"]])
def test_managed_codex_command_rejects_extra_options_and_prompts(arguments):
    with pytest.raises(ValueError):
        codex_command(Path("/control"), Path("/writer"), arguments)


def readback_messages(sources):
    return [
        {"id": 2, "result": {"data": [{"id": PROFILE, "allowed": True}]}},
        {"id": 3, "result": {"activePermissionProfile": {"id": PROFILE},
                              "sandbox": {"type": "workspaceWrite", "networkAccess": True,
                                          "writableRoots": ["/writer"]},
                              "runtimeWorkspaceRoots": ["/repo", "/writer"],
                              "approvalPolicy": "never",
                              "instructionSources": sources}},
    ]


def test_readback_disables_all_switchstand_servers(monkeypatch):
    responses = iter(
        json.dumps(message) + "\n"
        for message in [
            {"id": 1, "result": {}},
            {"id": 2, "result": {"data": []}},
            {"id": 3, "result": {}},
        ]
    )
    launched = {}

    class Input:
        def write(self, value):
            pass

        def flush(self):
            pass

    class Output:
        def readline(self):
            return next(responses)

    class Process:
        stdin = Input()
        stdout = Output()

        def terminate(self):
            pass

        def wait(self, timeout=None):
            return 0

    class Selector:
        def register(self, *args):
            pass

        def select(self, timeout=None):
            return [(object(), selectors.EVENT_READ)]

        def close(self):
            pass

    def popen(arguments, **kwargs):
        launched["arguments"] = arguments
        return Process()

    monkeypatch.setattr("switchstand.codex_runtime.subprocess.Popen", popen)
    monkeypatch.setattr("switchstand.codex_runtime.selectors.DefaultSelector", Selector)

    _rpc_messages(Path("/repo"), Path("/writer"), {})
    arguments = launched["arguments"]
    assert 'default_permissions="switchstand-development"' in arguments
    assert 'mcp_servers.switchstand.url="https://laptop.tail46f0b9.ts.net/switchstand/mcp"' in arguments
    assert "mcp_servers.switchstand.enabled=false" in arguments
    assert 'mcp_servers.switchstand_managed.command="scripts/switchstand-controller-mcp"' in arguments
    assert "mcp_servers.switchstand_managed.enabled=false" in arguments
    assert 'mcp_servers.switchstand_development.command="scripts/switchstand-development-mcp"' in arguments
    assert "mcp_servers.switchstand_development.enabled=false" in arguments


def test_readback_accepts_profile_when_codex_omits_allowed(monkeypatch):
    messages = readback_messages(
        [str(Path.home() / ".codex/AGENTS.md"), "/repo/AGENTS.md"]
    )
    del messages[0]["result"]["data"][0]["allowed"]
    monkeypatch.setattr(
        "switchstand.codex_runtime._rpc_messages",
        lambda control, candidate, env: messages,
    )
    assert readback(Path("/repo"), Path("/writer"), {}).profile == PROFILE


def test_readback_rejects_explicitly_disallowed_profile(monkeypatch):
    messages = readback_messages(
        [str(Path.home() / ".codex/AGENTS.md"), "/repo/AGENTS.md"]
    )
    messages[0]["result"]["data"][0]["allowed"] = False
    monkeypatch.setattr(
        "switchstand.codex_runtime._rpc_messages",
        lambda control, candidate, env: messages,
    )
    with pytest.raises(RuntimeError, match="permission profile.*not available"):
        readback(Path("/repo"), Path("/writer"), {})


@pytest.mark.parametrize("source, accepted", [(str(Path.home() / ".codex/AGENTS.md"), True),
                                               ("/home/test/.claude/CLAUDE.md", False)])
def test_readback_allows_only_declared_instruction_sources(monkeypatch, source, accepted):
    monkeypatch.setattr(
        "switchstand.codex_runtime._rpc_messages",
        lambda control, candidate, env: readback_messages([source, "/repo/AGENTS.md"]),
    )
    if accepted:
        assert readback(Path("/repo"), Path("/writer"), {}).profile == PROFILE
    else:
        with pytest.raises(RuntimeError, match="undeclared instruction"):
            readback(Path("/repo"), Path("/writer"), {})


def test_readback_rejects_control_or_other_writable_roots(monkeypatch):
    messages = readback_messages([str(Path.home() / ".codex/AGENTS.md"), "/repo/AGENTS.md"])
    messages[1]["result"]["sandbox"]["writableRoots"] = ["/repo", "/writer"]
    monkeypatch.setattr(
        "switchstand.codex_runtime._rpc_messages",
        lambda control, candidate, env: messages,
    )
    with pytest.raises(RuntimeError, match="unexpected writable roots"):
        readback(Path("/repo"), Path("/writer"), {})


def test_readback_rejects_disabled_network(monkeypatch):
    messages = readback_messages([str(Path.home() / ".codex/AGENTS.md"), "/repo/AGENTS.md"])
    messages[1]["result"]["sandbox"]["networkAccess"] = False
    monkeypatch.setattr(
        "switchstand.codex_runtime._rpc_messages",
        lambda control, candidate, env: messages,
    )
    with pytest.raises(RuntimeError, match="unexpected sandbox"):
        readback(Path("/repo"), Path("/writer"), {})
