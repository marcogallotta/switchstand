"""Opt-in actual native/Tracker coordination and fresh-context recovery trial."""
import json
import os
import shutil
from pathlib import Path

import pytest
from test_codex_stateful_client import AppServerClient, response_result


def verify_trace(workspace, evidence, work):
    """Independent actual server receipts and ordering, never agent claims alone."""
    calls = []
    for index, event in enumerate(evidence):
        item = event.get("params", {}).get("item", {})
        if item.get("type") == "mcpToolCall" and item.get("status") == "completed":
            assert not item.get("error"), item
            result = item["result"]
            assert not result.get("isError"), result
            data = json.loads(result["content"][0]["text"])
            calls.append((index, event["params"]["threadId"], item, data))
    reads = [c for c in calls if c[2]["tool"] == "work_get" and c[3].get("status") == "ok"]
    parent = [c for c in reads if c[3]["item"]["id"] == work]
    prefix = parent[0][3]["item"]["notes"].split("NATIVE ACTIVE LOOP TRIAL")[0]
    ids = {"a3269670-9f74-52bc-ad85-585059bee766", "96b74135-0018-54b8-b6b5-a086047c177f",
           "b7948ccd-fa2f-5a55-8447-670df7abc9cc", "3b7fc7d2-b811-5eae-ad11-1dc7a4356a5e",
           "a3d94830-f5b0-5321-95ed-15e21723fa3d"}
    rows = json.loads((workspace / "status.json").read_text())
    assert {r["work_id"] for r in rows} == ids
    for row in rows:
        source = next(c[3]["item"] for c in reads if c[3]["item"]["id"] == row["work_id"])
        assert row["title"] == source["title"] and "DEFERRED" in source["notes"]
        assert row["next_action"].strip() and "NOT_RUN" in source["notes"]
    writes = [c for c in calls if c[2]["tool"] == "work_update"]
    assert len(writes) == 3, writes
    for position, call in enumerate(writes):
        data, args = call[3], call[2]["arguments"]
        assert args["work_id"] == work and data["status"] == "ok" and data["effect"] == "applied"
        receipt = data["receipt"]
        assert receipt["work_id"] == work and receipt["operation_id"] == args["operation_id"]
        notes = receipt["patch"]["notes"]
        assert notes.split("NATIVE ACTIVE LOOP TRIAL")[0] == prefix
        compact = notes.replace(" ", "")
        assert '"A":"COMPLETE"' in compact
        assert ('"B":"PENDING"' if position < 2 else '"B":"COMPLETE"') in compact
        if position: assert '"F":"COMPLETE"' in compact
    fresh = [c for c in parent if c[1] != writes[0][1]]
    assert fresh and fresh[0][0] < writes[2][0] and '"B":"PENDING"' in fresh[0][3]["item"]["notes"].replace(" ", "")
    commands = [(n, e["params"]["item"]) for n, e in enumerate(evidence)
                if e.get("params", {}).get("threadId") == fresh[0][1]
                and e.get("params", {}).get("item", {}).get("type") == "commandExecution"]
    assert any(n > fresh[0][0] and "handoff.txt" in i["command"] for n, i in commands)
    inventory = json.loads((workspace / "runtime-inventory.json").read_text())
    advertised = any("task_control_checkpoint" in s.get("tools", {}) for s in inventory["data"])
    compatibility = (workspace / "compatibility.txt").read_text().lower()
    assert "task_control_checkpoint" in compatibility and any(x in compatibility for x in ("absent", "missing", "unavailable"))
    assert ("lists `task_control_checkpoint`" in compatibility) == advertised
    assert not any(c[2]["tool"] == "task_control_checkpoint" for c in calls)
    handoff = (workspace / "handoff.txt").read_text()
    assert work in handoff and ("13:25" in handoff or "deadline" in handoff.lower())


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
        assert {"work_get", "work_update"} <= servers["switchstand"]["tools"].keys()
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
        assert all(row["status"].startswith("DEFERRED") for row in status), status
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
        verify_trace(workspace, evidence, work)
    finally:
        (workspace / "native-events.json").write_text(json.dumps(evidence + client.pending))
        client.close()
