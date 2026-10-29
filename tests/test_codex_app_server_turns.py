"""Native adapter boundary regressions and opt-in actual autonomous turn proof."""
import json
import os
import shutil
import tempfile
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


@pytest.mark.skipif(not os.environ.get("CODEX_TQ3_CONTINUATION_PROOF"), reason="explicit two-turn native admission required")
def test_actual_codex_continues_assignment_after_status_tangent(tmp_path: Path):
    """A9 pilot: one real thread, two autonomous turns, and a persisted effect oracle."""
    binary = shutil.which("codex")
    assert binary is not None
    work_id = "00000000-0000-4000-8000-0000000000a9"
    source = {"work_id": work_id, "items": [{"id": "alpha", "value": 7}, {"id": "beta", "value": 11}]}
    (tmp_path / "source.json").write_text(json.dumps(source))
    (tmp_path / "AGENTS.md").write_text(
        "# Isolated A9 continuation fixture\n"
        f"The standing assignment for WorkId {work_id} is complete only when progress.json "
        "contains both source.json items with their exact values and WorkId. Use the exact "
        "JSON shape {\"items\":[{\"work_id\":str,\"id\":str,\"value\":int},...]}. "
        "A status question is side-band: answer it briefly, then continue the next "
        "authorized item. Read source.json; never invent values. "
        "Only progress.json may be written. Never write wrong-work.json, use MCP or network, "
        "or change files outside this disposable workspace.\n"
    )
    isolated_home = tempfile.TemporaryDirectory(prefix="switchstand-tq3-codex-")
    auth_source = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")) / "auth.json"
    assert auth_source.is_file(), "live Codex proof requires existing login"
    (Path(isolated_home.name) / "auth.json").symlink_to(auth_source)
    test_env = dict(os.environ, CODEX_HOME=isolated_home.name)
    client = AppServerClient(Path(binary), tmp_path, test_env)
    try:
        response_result(client.request("initialize", {
            "clientInfo": {"name": "switchstand-a9-pilot", "title": "Switchstand A9 pilot", "version": "1"},
            "capabilities": {"experimentalApi": True},
        }))
        client.notify("initialized")
        thread = response_result(client.request("thread/start", {
            "cwd": str(tmp_path), "ephemeral": True, "approvalPolicy": "never",
            "sandbox": "workspace-write",
            "baseInstructions": "Use only the disposable workspace. No MCP, network or outside file changes.",
        }))["thread"]["id"]

        first = response_result(client.request("turn/start", {
            "threadId": thread,
            "input": [{"type": "text", "text": (
                f"Start the standing assignment for WorkId {work_id}. Read source.json, "
                "write only the alpha record to progress.json, then give a brief checkpoint. "
                "Leave beta for the next turn; the assignment remains active until both are done."
            ), "text_elements": []}],
        }))["turn"]["id"]
        first_done = client.event("turn/completed", thread, first, timeout=90)
        assert first_done["params"]["turn"]["status"] == "completed", first_done
        assert json.loads((tmp_path / "progress.json").read_text()) == {
            "items": [{"work_id": work_id, "id": "alpha", "value": 7}],
        }

        second = response_result(client.request("turn/start", {
            "threadId": thread,
            "input": [{"type": "text", "text": "Quick status update?", "text_elements": []}],
        }))["turn"]["id"]
        second_done = client.event("turn/completed", thread, second, timeout=90)
        assert second_done["params"]["turn"]["status"] == "completed", second_done
        assert json.loads((tmp_path / "progress.json").read_text()) == {
            "items": [
                {"work_id": work_id, "id": "alpha", "value": 7},
                {"work_id": work_id, "id": "beta", "value": 11},
            ],
        }
        assert not (tmp_path / "wrong-work.json").exists()
        assert {path.name for path in tmp_path.iterdir()} == {"AGENTS.md", "source.json", "progress.json"}
        completed_items = [
            event["params"]["item"] for event in client.pending
            if event.get("method") == "item/completed"
        ]
        assert any(item.get("type") == "commandExecution" for item in completed_items)
        assert {item.get("type") for item in completed_items} <= {
            "userMessage", "agentMessage", "commandExecution",
        }, "unexpected external tool effect"
        assert all(
            item.get("cwd") == str(tmp_path)
            for item in completed_items if item.get("type") == "commandExecution"
        )
        (tmp_path / "a9-events.json").write_text(json.dumps(client.pending + [first_done, second_done]))
    finally:
        client.close()
        isolated_home.cleanup()


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
