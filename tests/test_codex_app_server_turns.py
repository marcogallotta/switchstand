"""Native adapter boundary regressions and opt-in actual autonomous turn proof."""
import json
import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest
from test_codex_stateful_client import AppServerClient, response_result


def test_reader_preserves_events_and_deadline_on_silent_pipe(tmp_path: Path):
    binary = tmp_path / "peer"
    binary.write_text(
        "#!/usr/bin/env python3\n"
        "import json,sys\n"
        "for line in sys.stdin:\n"
        " r=json.loads(line)\n"
        " if r['method']=='silent': continue\n"
        " print(json.dumps({'method':'turn/completed','params':{'threadId':'t','turn':{'id':'u'}}}),flush=True)\n"
        " print(json.dumps({'id':r['id'],'result':{'ok':True}}),flush=True)\n"
    )
    binary.chmod(0o700)
    client = AppServerClient(binary, tmp_path, dict(os.environ))
    try:
        assert response_result(client.request("answer", {})) == {"ok": True}
        assert client.event("turn/completed", "t", "u")["method"] == "turn/completed"
        started = time.monotonic()
        with pytest.raises(TimeoutError):
            client.request("silent", {}, timeout=0.05)
        assert time.monotonic() - started < 1
    finally:
        client.close()
    assert client.process.poll() is not None
    assert not client.reader.is_alive()


@pytest.mark.skipif(not os.environ.get("CODEX_NATIVE_TURN_PROOF"), reason="explicit native admission required")
def test_actual_native_turn_artifact_and_stop(tmp_path: Path):
    binary = shutil.which("codex")
    assert binary is not None
    client = AppServerClient(Path(binary), tmp_path, dict(os.environ))
    try:
        response_result(client.request("initialize", {
            "clientInfo": {"name": "switchstand-m1", "version": "1"},
            "capabilities": {"experimentalApi": True},
        }))
        client.notify("initialized")
        thread = response_result(client.request("thread/start", {
            "cwd": str(tmp_path), "ephemeral": True, "approvalPolicy": "never",
            "sandbox": "workspace-write",
            "baseInstructions": "Bounded test worker. Use shell only inside assigned workspace. No MCP, network, repository or outside file changes.",
        }))["thread"]["id"]
        case_source = Path(os.environ["CODEX_NATIVE_CASES"])
        cases = json.loads(case_source.read_text())["cases"]
        source = tmp_path / "acceptance-cases.json"
        source.write_text(json.dumps({"cases": cases}))
        started = response_result(client.request("turn/start", {
            "threadId": thread,
            "input": [{"type": "text", "text": "Use a shell command to read acceptance-cases.json and produce unresolved.json: a JSON array containing id, title and status for every case whose status is not PASS, preserving source order. This inventory is needed for the Coordinator implementation handoff. Verify it from the source and finish. This workspace is your entire writable surface.", "text_elements": []}],
        }))["turn"]["id"]
        completed = client.event("turn/completed", thread, started, timeout=120)
        assert completed["params"]["turn"]["status"] == "completed", completed
        artifact = tmp_path / "unresolved.json"
        expected = [{k: case[k] for k in ("id", "title", "status")} for case in cases if case["status"] != "PASS"]
        assert json.loads(artifact.read_text()) == expected
        (tmp_path / "native-events.json").write_text(json.dumps(client.pending + [completed]))
        assert any(m.get("method") == "item/completed" and m.get("params", {}).get("item", {}).get("type") == "commandExecution" for m in client.pending)
        stop_turn = response_result(client.request("turn/start", {
            "threadId": thread,
            "input": [{"type": "text", "text": "Run this exact shell command: echo $$ > command.pid; touch entered; i=0; while [ ! -e release ] && [ $i -lt 100 ]; do sleep 0.05; i=$((i+1)); done; touch continued. Do not launch background processes. It waits for the observer to release it.", "text_elements": []}],
        }))["turn"]["id"]
        client.receive(lambda m: m.get("method") == "item/started" and m.get("params", {}).get("turnId") == stop_turn and m.get("params", {}).get("item", {}).get("type") == "commandExecution", timeout=60)
        deadline = time.monotonic() + 10
        while not (tmp_path / "entered").exists():
            assert time.monotonic() < deadline, "command never entered"
            time.sleep(0.05)
        response_result(client.request("turn/interrupt", {"threadId": thread, "turnId": stop_turn}))
        stopped = client.event("turn/completed", thread, stop_turn, timeout=20)
        assert stopped["params"]["turn"]["status"] == "interrupted", stopped
        pid = int((tmp_path / "command.pid").read_text())
        process_stat = Path(f"/proc/{pid}/stat")
        assert not process_stat.exists() or process_stat.read_text().split()[2] == "Z", "command process survived STOP"
        (tmp_path / "release").touch()
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline:
            assert not (tmp_path / "continued").exists(), "command mutated workspace after STOP"
            time.sleep(0.05)
    finally:
        client.close()


def test_unsupported_server_request_fails_closed_and_retains_evidence(tmp_path: Path):
    binary = tmp_path / "peer"
    binary.write_text(
        "#!/usr/bin/env python3\n"
        "import json,sys\n"
        "r=json.loads(sys.stdin.readline())\n"
        "print(json.dumps({'id':99,'method':'unexpected/approval'}),flush=True)\n"
        "reply=json.loads(sys.stdin.readline())\n"
        "assert reply['id']==99 and reply['error']['code']==-32601\n"
    )
    binary.chmod(0o700)
    client = AppServerClient(binary, tmp_path, dict(os.environ))
    try:
        with pytest.raises(RuntimeError, match="Unsupported server request"):
            client.request("answer", {})
        assert client.pending[0]["method"] == "unexpected/approval"
        assert client.process.wait(timeout=2) == 0
    finally:
        client.close()


def test_close_reaps_owned_descendant_holding_stdout(tmp_path: Path):
    binary = tmp_path / "peer"
    binary.write_text(
        "#!/usr/bin/env python3\n"
        "import json,subprocess,time\n"
        "child=subprocess.Popen(['sleep','60'])\n"
        "print(json.dumps({'child':child.pid}),flush=True)\n"
        "time.sleep(60)\n"
    )
    binary.chmod(0o700)
    client = AppServerClient(binary, tmp_path, dict(os.environ))
    client.receive(lambda m: "child" in m)
    started = time.monotonic()
    client.close()
    assert time.monotonic() - started < 4
    assert client.process.poll() is not None
    assert not client.reader.is_alive()


@pytest.mark.skipif(not os.environ.get("CODEX_NATIVE_TURN_PROOF"), reason="explicit native admission required")
def test_actual_guidance_runtime_skew_continues_owned_duties(tmp_path: Path):
    binary = shutil.which("codex")
    assert binary is not None
    client = AppServerClient(Path(binary), tmp_path, dict(os.environ))
    try:
        response_result(client.request("initialize", {
            "clientInfo": {"name": "switchstand-skew-proof", "version": "1"},
            "capabilities": {"experimentalApi": True},
        }))
        client.notify("initialized")
        inventory = response_result(client.request("mcpServerStatus/list", {"detail": "full"}))
        entries = [entry for entry in inventory["data"] if entry.get("name") == "switchstand"]
        if not entries or not entries[0].get("tools"):
            pytest.skip("MISSING_CAPABILITY: actual Switchstand runtime inventory unavailable")
        tools = entries[0]["tools"]
        missing = "task_control_checkpoint"
        if missing in tools:
            pytest.skip("Runtime now exposes checkpoint; old-runtime skew is absent")
        observed = {"tools": sorted(tools), "cli": subprocess.run([binary, "--version"], text=True, capture_output=True, check=True).stdout.strip()}
        (tmp_path / "runtime-inventory.json").write_text(json.dumps(observed))
        case_source = Path(os.environ["CODEX_NATIVE_CASES"])
        cases = json.loads(case_source.read_text())["cases"]
        (tmp_path / "acceptance-cases.json").write_text(json.dumps({"cases": cases}))
        safe = "Use shell file checkpoint.json for this isolated fixture; preserve every named duty. No external effects."
        (tmp_path / "instructions-safe.md").write_text(safe)
        (tmp_path / "instructions-active.md").write_text(
            "Before continuing duties, save the checkpoint via task_control_checkpoint."
        )
        thread = response_result(client.request("thread/start", {
            "cwd": str(tmp_path), "ephemeral": True, "approvalPolicy": "never",
            "sandbox": "workspace-write",
            "baseInstructions": "Bounded compatibility test worker. Only this workspace is writable. No network/provider mutations or workers. Check actual available capabilities before following fixture instructions.",
        }))["thread"]["id"]
        turn = response_result(client.request("turn/start", {
            "threadId": thread,
            "input": [{"type": "text", "text": (
                "Follow instructions-active.md using actual runtime-inventory.json. A is highest priority: "
                "write unresolved.json containing id/title/status of non-PASS acceptance-cases in source order. "
                "B is the capability-skew investigation. C is a pending human review duty you must retain; "
                "do not invent its completion. If active instructions require an unavailable tool, do not call "
                "or retry it. You are authorized to replace only this fixture's instructions-active.md with "
                "instructions-safe.md and continue with that known compatible pair. Produce checkpoint.json "
                "with missing_tool (absent tool name), instruction_pair (chosen instructions filename), runtime_cli (exact observed cli string), duties {A,B,C} statuses COMPLETE/COMPLETE/PENDING "
                "only after A/B actually finish, and next_priority C. Report the exact mismatch. "
                "Verify all artifacts; do not claim runtime deployment or PR1060 causality."
            ), "text_elements": []}],
        }))["turn"]["id"]
        completed = client.event("turn/completed", thread, turn, timeout=120)
        assert completed["params"]["turn"]["status"] == "completed", completed
        assert (tmp_path / "instructions-active.md").read_text() == safe
        expected = [{k: c[k] for k in ("id", "title", "status")} for c in cases if c["status"] != "PASS"]
        assert json.loads((tmp_path / "unresolved.json").read_text()) == expected
        checkpoint = json.loads((tmp_path / "checkpoint.json").read_text())
        assert checkpoint["missing_tool"] == missing
        assert checkpoint["instruction_pair"] == "instructions-safe.md"
        assert checkpoint["runtime_cli"] == observed["cli"]
        assert checkpoint["duties"] == {"A": "COMPLETE", "B": "COMPLETE", "C": "PENDING"}
        assert checkpoint["next_priority"] == "C"
        items = [m.get("params", {}).get("item", {}) for m in client.pending]
        assert any(item.get("type") == "commandExecution" for item in items)
        assert not any(item.get("type") == "mcpToolCall" and item.get("tool") == missing for item in items)
        (tmp_path / "native-skew-events.json").write_text(json.dumps(client.pending + [completed]))
    finally:
        client.close()
