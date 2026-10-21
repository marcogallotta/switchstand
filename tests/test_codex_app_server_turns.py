"""Native adapter boundary regressions and opt-in actual autonomous turn proof."""
import json
import os
import shutil
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
            "input": [{"type": "text", "text": "Run a shell command sleep 30; do not change files.", "text_elements": []}],
        }))["turn"]["id"]
        client.receive(lambda m: m.get("method") == "item/started" and m.get("params", {}).get("turnId") == stop_turn and m.get("params", {}).get("item", {}).get("type") == "commandExecution", timeout=60)
        response_result(client.request("turn/interrupt", {"threadId": thread, "turnId": stop_turn}))
        stopped = client.event("turn/completed", thread, stop_turn, timeout=20)
        assert stopped["params"]["turn"]["status"] == "interrupted", stopped
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
