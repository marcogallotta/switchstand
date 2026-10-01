import json
import subprocess
from dataclasses import asdict
from uuid import uuid4

import pytest

from switchstand.codex_wakeful import (
    CodexBinding,
    Projection,
    SharedClient,
    WakeSourceRef,
    bind,
    children,
    inbound,
    wake_id,
)
from switchstand.messages import PendingMessage
from switchstand.secure_file import atomic_replace_bytes


class Client(SharedClient):
    def __init__(self, home, token):
        self.calls = []
        self.lost = False
        self.thread = {"id": "exact", "historyMode": "legacy", "turns": [],
                       "status": {"type": "idle"}, "canAcceptDirectInput": True}
        self.path = home / "rollout.jsonl"
        self.record = {"type": "response_item", "payload": {"type": "message",
            "role": "developer", "content": [{"type": "input_text", "text":
            f"Coordinator start commit is recorded at {token}. Reread that file after "
            "context compaction and before handoff."}]}}
        self.path.write_text(json.dumps(self.record, ensure_ascii=False) + "\n")
        self.listed = [{"id": "exact", "path": str(self.path)}]

    def call(self, method, params):
        if method == "thread/list":
            return {"data": self.listed, "nextCursor": None}
        if method == "thread/read":
            return {"thread": self.thread}
        assert method == "turn/start"
        pending = json.loads((self.path.parent / "codex-wakeful.json").read_text())
        assert pending[params["clientUserMessageId"]]["state"] == "PENDING"
        assert pending[params["clientUserMessageId"]]["attempted"] is True
        self.calls.append(params)
        self.thread["turns"].append({"id": "turn", "items": [{
            "type": "userMessage", "id": "item", "clientId": params["clientUserMessageId"]}]})
        if self.lost:
            raise OSError("lost response")
        return {}

@pytest.fixture
def setup(tmp_path):
    token = tmp_path / "start-commit.one"
    atomic_replace_bytes(token, b"base\n")
    client = Client(tmp_path, token)
    binding = bind(client, tmp_path, token)
    assert isinstance(binding, CodexBinding)
    return tmp_path, token, client, binding

def test_binding_exact_zero_multiple_and_reconnect(setup):
    home, token, client, binding = setup
    other = home / "start-commit.two"
    atomic_replace_bytes(other, b"base\n")
    assert bind(client, home, other) == "NOT_BOUND"
    client.listed.append({"id": "other", "path": str(client.path)})
    assert bind(client, home, token) == "CONFLICT"
    client.listed.pop()
    assert bind(client, home, token) == binding
    client.listed[0]["id"] = "replacement"
    p = Projection(home, binding)
    assert p.admit(client, WakeSourceRef("child_completion", "call/child/completed")) == "STALE"

@pytest.mark.parametrize("suffix,expected", [(". Reread", True), (", next", True),
    (".suffix", False), ("/child", False), ("longer", False), (".suffix next", False)])
def test_binding_path_boundary_and_unicode_jsonl(setup, suffix, expected):
    home, token, client, binding = setup
    client.record["payload"]["content"][0]["text"] = f"Start record: {token}{suffix}\u2028\u0085"
    client.path.write_text(json.dumps(client.record, ensure_ascii=False) + "\n")
    assert bind(client, home, token) == (binding if expected else "NOT_BOUND")
    client.record["payload"]["role"] = "user"
    client.path.write_text(json.dumps(client.record) + "\n")
    assert bind(client, home, token) == "NOT_BOUND"

@pytest.mark.parametrize("lost", [False, True])
def test_persist_before_send_lost_response_duplicate_stability(setup, lost):
    home, _, client, binding = setup
    source = WakeSourceRef("switchstand_inbound", str(uuid4()))
    client.lost = lost
    p = Projection(home, binding)
    assert p.admit(client, source) == "UNKNOWN"
    assert json.loads(p.path.read_text())[wake_id(binding, source)]["state"] == "PENDING"
    assert Projection(home, binding).admit(client, source) == "ADMITTED"
    assert p.path.stat().st_mode & 0o777 == 0o600
    assert Projection(home, binding).admit(client, source) == "ADMITTED"
    assert len(client.calls) == 1
    assert client.calls[0]["clientUserMessageId"] == wake_id(binding, source)
    assert set(asdict(source)) == {"source_kind", "source_id"}
    replacement = CodexBinding(binding.thread_id, binding.start_record, "new-generation")
    assert wake_id(replacement, source) != wake_id(binding, source)

def test_nonsteerable_pending_and_unresolved_absence_never_resends(setup):
    home, _, client, binding = setup
    source = WakeSourceRef("switchstand_inbound", str(uuid4()))
    p = Projection(home, binding)
    client.thread.update(status={"type": "active", "activeFlags": []},
                         canAcceptDirectInput=False)
    assert p.admit(client, source) == "PENDING"
    assert not client.calls
    client.thread["canAcceptDirectInput"] = True
    assert p.admit(client, source) == "UNKNOWN"
    client.thread["turns"] = []
    assert Projection(home, binding).admit(client, source) == "UNKNOWN"
    assert len(client.calls) == 1

@pytest.mark.parametrize("stale", [["other", "child", "completed"],
    ["call", "other", "completed"], ["call", "child", "errored"]])
def test_delivery_identity_and_missed_child_latest_parent_oracle(setup, stale):
    home, _, client, binding = setup
    delivery = PendingMessage(delivery_id=uuid4(), message_id=uuid4(), sender_work_id=uuid4(),
        recipient_work_id=uuid4(), route_ref="synthetic", kind="request", payload="discard",
        state="AVAILABLE", recipient_grant_version=1)
    assert inbound(delivery).source_id == str(delivery.delivery_id)
    assert inbound(delivery).source_id != str(delivery.message_id)
    spawn = {"type": "collabAgentToolCall", "id": "call", "tool": "spawnAgent",
             "receiverThreadIds": ["child"], "agentsStates": {"child": {"status": "running"}}}
    terminal = {**spawn, "id": "wait", "tool": "wait", "agentsStates": {
        "child": {"status": "completed", "message": "discard"}}}
    thread = {"turns": [{"items": [spawn, terminal]}]}
    source, = children(thread)
    assert "discard" not in source.source_id
    client.thread["turns"] = thread["turns"]
    p = Projection(home, binding)
    terminal["agentsStates"]["child"]["status"] = "running"
    assert p.admit(client, source) == "STALE"
    assert not client.calls
    terminal["agentsStates"]["child"]["status"] = "completed"
    assert p.admit(client, WakeSourceRef("child_completion",
        json.dumps(stale, separators=(",", ":")))) == "STALE"
    assert not client.calls
    assert p.admit(client, source) == "UNKNOWN"
    assert Projection(home, binding).admit(client, children(thread)[0]) == "ADMITTED"
    assert len(client.calls) == 1

def test_simultaneous_probe_and_private_file_boundary(setup):
    import fcntl
    import os
    import sys

    home, token, _, binding = setup
    with token.open("rb") as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = subprocess.run([sys.executable, "-m", "switchstand.codex_wakeful",
            "--opt-in", "--home", str(home), "--codex", "/must-not-run",
            "--start-record", str(token)], capture_output=True, text=True, check=False)
    assert result.returncode == 0 and "simultaneous probe" in result.stdout
    path = home / "codex-wakeful.json"
    path.symlink_to(token)
    with pytest.raises(OSError):
        Projection(home, binding)
    path.unlink()
    atomic_replace_bytes(path, b"{}")
    os.chmod(path, 0o644)
    with pytest.raises(ValueError):
        Projection(home, binding)

def test_claude_conformance_fake_only():
    def fake(session_id, generation, candidates):
        return (session_id, generation) if candidates == [(session_id, generation)] else "UNKNOWN"
    assert fake("exact", "one", [("exact", "one")]) == ("exact", "one")
    assert fake("exact", "one", [("newest", "two")]) == "UNKNOWN"
    assert fake("exact", "one", [("exact", "one"), ("exact", "two")]) == "UNKNOWN"

def test_proxy_stream_multiple_buffered_frames():
    import sys

    client = object.__new__(SharedClient)
    client.sequence, client.buffer = 0, b""
    client.process = subprocess.Popen([sys.executable, "-c", ("import sys; "
        "sys.stdin.readline(); sys.stdout.write("
        "'{\"method\":\"notification\"}\\n{\"id\":1,\"result\":{\"ok\":true}}\\n'); "
        "sys.stdout.flush(); sys.stdin.read()")], stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    try:
        assert client.call("thread/read", {}) == {"ok": True}
    finally:
        client.close()
