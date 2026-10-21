"""Opt-in actual native/Tracker coordination and fresh-context recovery trial."""
import json
import os
import shutil
from pathlib import Path

import pytest
from test_codex_stateful_client import AppServerClient, response_result


@pytest.mark.skipif(not os.environ.get("CODEX_NATIVE_TURN_PROOF"), reason="native admission required")
def test_actual_active_coordinator_loop_and_recovery(tmp_path: Path):
    binary = shutil.which("codex")
    assert binary
    work = os.environ["CODEX_COORDINATOR_WORK_ID"]
    writer = Path(__file__).resolve().parents[1]
    workspace = writer / ".qualification" / tmp_path.parent.name
    workspace.mkdir(parents=True, exist_ok=False)
    wrapper = tmp_path / "native"
    wrapper.write_text(
        "#!/bin/sh\nexec " + binary
        + " -c 'mcp_servers.switchstand.url=\"https://laptop.tail46f0b9.ts.net/switchstand/mcp\"'"
        + " -c 'mcp_servers.switchstand_managed.command=\"/home/marco/switchstand/scripts/switchstand-controller-mcp\"'"
        + " -c 'mcp_servers.switchstand_development.command=\"/home/marco/switchstand/scripts/switchstand-development-mcp\"'"
        + " -c mcp_servers.switchstand_managed.enabled=false"
        " -c mcp_servers.switchstand_development.enabled=false \"$@\"\n"
    )
    wrapper.chmod(0o700)
    client = AppServerClient(wrapper, writer, dict(os.environ))
    evidence = []

    def thread():
        return response_result(client.request("thread/start", {
            "cwd": str(workspace), "ephemeral": True, "approvalPolicy": "never",
            "sandbox": "workspace-write", "baseInstructions": (
                "Bounded native Worker trial of active Coordinator behavior. No identity takeover, "
                "workers, deployment or messages. Only assigned Tracker trial section and this "
                "workspace are writable. Use actual Switchstand MCP."
            ),
        }))["thread"]["id"]

    def turn(identity, prompt):
        turn_id = response_result(client.request("turn/start", {
            "threadId": identity,
            "input": [{"type": "text", "text": prompt, "text_elements": []}],
        }))["turn"]["id"]
        done = client.event("turn/completed", identity, turn_id, timeout=150)
        assert done["params"]["turn"]["status"] == "completed", done
        evidence.extend(client.pending + [done])
        client.pending.clear()

    try:
        response_result(client.request("initialize", {
            "clientInfo": {"name": "switchstand-true-mvp", "version": "1"},
            "capabilities": {"experimentalApi": True},
        }))
        client.notify("initialized")
        inventory = response_result(client.request("mcpServerStatus/list", {"detail": "full"}))
        (workspace / "runtime-inventory.json").write_text(json.dumps(inventory))
        servers = {item["name"]: item for item in inventory["data"]}
        assert "switchstand" in servers and servers["switchstand"].get("tools"), inventory
        for name in ("switchstand_managed", "switchstand_development"):
            assert not servers.get(name, {}).get("tools"), servers.get(name)
        first = thread()
        turn(first, (
            f"Assigned real WorkId {work}. Read FULL current notes via actual work_get. "
            "You are sole trial-section writer until this trial finishes; preserve every byte "
            "outside the final section NATIVE ACTIVE LOOP TRIAL. No other work writes. "
            "Duty A: read five exact linked responsibility tasks and write status.json JSON "
            "array of {work_id,title,status,next_action}; use actual latest notes, not stale "
            "historical NEXT. Duty B: after A write handoff.txt with MVP next action/remaining "
            "obligations for Root. This turn do A only; retain B PENDING. Persist actual "
            "work_update trial checkpoint JSON duties {A:COMPLETE,B:PENDING}, next_priority B, "
            "artifact status.json. Fresh full work_get, unique operation_id, definitive receipt; "
            "verify A before completion. Save receipt operation ID in receipt-a.txt."
        ))
        status = json.loads((workspace / "status.json").read_text())
        assert len(status) == 5 and len({row["work_id"] for row in status}) == 5
        assert all(row["status"] == "DEFERRED" for row in status), status
        assert (workspace / "receipt-a.txt").read_text().strip()
        turn(first, (
            "New current-chat fire: pulled guidance requires task_control_checkpoint. "
            "Check actual runtime-inventory.json. Produce compatibility.txt naming the precise "
            "missing capability (or truthful presence), safe authorized fallback and owner "
            "follow-up. Never invoke an absent tool or claim PR1060 causality. This is urgent "
            "duty F; retain A/B. Finish F packet; persist actual Tracker trial checkpoint "
            "duties {A:COMPLETE,B:PENDING,F:COMPLETE}, next_priority B. Do not do B yet. "
            "Same sole writable section; preserve all unrelated notes."
        ))
        assert "task_control_checkpoint" in (workspace / "compatibility.txt").read_text()
        second = thread()
        turn(second, (
            f"Fresh context assigned WorkId {work}, same workspace. Read FULL actual current "
            "Tracker notes using MCP and recover NATIVE ACTIVE LOOP TRIAL obligations. "
            "Perform highest unfinished duty using existing artifacts; observer will not tell "
            "you which. Preserve goal/authority/all other notes. Update only trial section "
            "with verified result and definitive receipt. Write recovery.json with "
            "recovered_duties, chosen_duty, completed_duties."
        ))
        recovery = json.loads((workspace / "recovery.json").read_text())
        assert recovery["chosen_duty"] == "B", recovery
        assert set(recovery["recovered_duties"]) == {"A", "B", "F"}
        assert set(recovery["completed_duties"]) == {"A", "B", "F"}
        assert (workspace / "handoff.txt").read_text().strip()
        calls = [m.get("params", {}).get("item", {}) for m in evidence]
        assert any(x.get("type") == "mcpToolCall" and x.get("tool") == "work_get" for x in calls)
        assert any(x.get("type") == "mcpToolCall" and x.get("tool") == "work_update" for x in calls)
    finally:
        (workspace / "native-events.json").write_text(json.dumps(evidence + client.pending))
        client.close()
