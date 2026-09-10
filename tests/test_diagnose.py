import asyncio
from pathlib import Path

from switchstand import diagnose


def app_evidence():
    tools = {
        name: {"approval_mode": mode}
        for name, mode in diagnose.TOOL_POLICY.items()
    }
    return {
        "config": {"config": {
            "approval_policy": "on-request",
            "approvals_reviewer": "user",
            "mcp_servers": {"switchstand": {
                "enabled_tools": list(diagnose.TOOL_POLICY), "tools": tools,
            }},
        }},
        "mcp_status": {"data": [{
            "name": "switchstand", "tools": {name: {} for name in tools},
        }]},
        "host_approval": "POLICY_NOT_AVAILABLE",
    }


def test_direct_probe_separates_sealed_admitted_and_external_effects():
    result = asyncio.run(diagnose._probe_controller())
    assert result["pre_admit"] == ["ok", "denied", "denied"]
    assert result["admitted"] is True
    assert result["update"] == result["append"] == "ok"
    assert result["foreign_update"] == "denied"
    assert result["local_effects"] == 2
    assert result["external_provider_effects"] == 0
    assert result["tools"] == sorted(diagnose.TOOL_POLICY)
    assert result["annotations"]["work_update"]["destructiveHint"] is True


def test_validate_keeps_codex_and_controller_evidence_independent():
    controller = asyncio.run(diagnose._probe_controller())
    assert diagnose._validate(app_evidence(), controller) == []
    app = app_evidence()
    app["config"]["config"]["mcp_servers"]["switchstand"]["tools"]["work_update"][
        "approval_mode"
    ] = "auto"
    assert "wrong work_update approval mode" in diagnose._validate(app, controller)[0]


def test_diagnose_writes_private_ready_receipt(monkeypatch, tmp_path):
    root = Path(__file__).parents[1]
    controller = asyncio.run(diagnose._probe_controller())
    monkeypatch.setattr(diagnose, "_app_server", lambda root: (app_evidence(), {"config_ms": 1}))
    monkeypatch.setattr(diagnose, "_probe_controller", lambda: _async_value(controller))
    monkeypatch.setenv("HOME", str(tmp_path))
    result = diagnose.diagnose(root)
    receipt = Path(result["evidence_path"])
    assert result["outcome"] == "READY_FOR_CANARY"
    assert result["qualification"] == "CANARY_REQUIRED"
    assert receipt.stat().st_mode & 0o777 == 0o600


def test_app_server_canary_requires_observable_approval_resolution(monkeypatch, tmp_path):
    root = Path(__file__).parents[1]
    diagnostic_path = tmp_path / "candidate.json"
    monkeypatch.setattr(diagnose, "diagnose", lambda root: {
        "outcome": "READY_FOR_CANARY", "candidate_sha256": "a" * 64,
        "candidate": {"worktree_sha256": "same"}, "evidence_path": str(diagnostic_path),
    })
    monkeypatch.setattr(diagnose, "_worktree_sha", lambda root: "same")
    events = []
    for name in ("work_get", "work_append", "work_get", "work_update"):
        events.append({"method": "item/completed", "params": {"item": {
            "type": "mcpToolCall", "tool": name, "status": "completed",
            "result": {"structuredContent": {"status": "ok"}},
        }}})
    events.extend([
        {"id": 9, "method": "item/commandExecution/requestApproval",
         "params": {"command": "touch /etc/switchstand-diagnostic-canary"}},
        {"method": "serverRequest/resolved", "params": {"requestId": 9}},
        {"method": "item/completed", "params": {"item": {
            "type": "commandExecution", "status": "declined",
            "command": "touch /etc/switchstand-diagnostic-canary",
        }}},
        {"method": "item/completed", "params": {"item": {
            "type": "agentMessage", "text": "CANARY_DONE",
        }}},
        {"method": "turn/completed", "params": {}},
    ])
    monkeypatch.setattr(diagnose, "_model_turn", lambda root, prompt, journal: {
        "events": events, "sent": [], "launch_ms": 5, "errors": [],
        "timeout_kind": None, "harness_error": None, "thread_id": "thread",
        "turn_id": "turn", "thread_read": None, "last_event": events[-1],
        "process_returncode": 0, "journal_path": str(journal),
    })
    result = diagnose.qualify(root)
    assert result["outcome"] == "QUALIFIED"
    assert result["assertions"]["escalation_denied"] is True


def test_canary_does_not_accept_prompt_text_or_uncompleted_denial(monkeypatch, tmp_path):
    root = Path(__file__).parents[1]
    monkeypatch.setattr(diagnose, "diagnose", lambda root: {
        "outcome": "READY_FOR_CANARY", "candidate_sha256": "b" * 64,
        "candidate": {"worktree_sha256": "same"},
        "evidence_path": str(tmp_path / "candidate.json"),
    })
    monkeypatch.setattr(diagnose, "_worktree_sha", lambda root: "same")
    events = [
        {"method": "item/completed", "params": {"item": {
            "type": "userMessage", "text": "CANARY_DONE",
        }}},
        {"id": 9, "method": "item/commandExecution/requestApproval",
         "params": {"command": "touch /etc/switchstand-diagnostic-canary"}},
        {"method": "serverRequest/resolved", "params": {"requestId": 9}},
        {"method": "turn/completed", "params": {}},
    ]
    monkeypatch.setattr(diagnose, "_model_turn", lambda root, prompt, journal: {
        "events": events, "sent": [], "launch_ms": 5, "errors": [],
        "timeout_kind": None, "harness_error": None, "thread_id": "thread",
        "turn_id": "turn", "thread_read": None, "last_event": events[-1],
        "process_returncode": 0, "journal_path": str(journal),
    })
    result = diagnose.qualify(root)
    assert result["assertions"]["completion_marker"] is False
    assert result["assertions"]["escalation_denied"] is False


def test_timeout_recovery_uses_exact_app_server_identity_contract():
    assert diagnose._interrupt_params("thread", "turn") == {
        "threadId": "thread", "turnId": "turn",
    }
    assert diagnose._thread_read_params("thread") == {"threadId": "thread"}


async def _async_value(value):
    return value
