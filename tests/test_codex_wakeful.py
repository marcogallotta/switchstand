import json
import subprocess
import sys
import tempfile
import threading
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from websockets.sync.server import unix_serve

from switchstand.agent_mailboxes import AgentMailbox
from switchstand.codex_wakeful import (
    CodexBinding,
    Projection,
    QueueClient,
    WakeSourceRef,
    bind,
    chat_session_key,
    children,
    inbound,
    inbound_cycle,
    open_wakeful_event_store,
    wake_id,
    wakeful_event_cycle,
)
from switchstand.messages import PendingMessage
from switchstand.secure_file import atomic_replace_bytes
from switchstand.wakeful import EventSeverity, WakeEvent, WakefulStore


class Client(QueueClient):
    def __init__(self, home, token):
        self.calls = []
        self.lost = False
        self.queued = []
        self.turn_pages = {}
        self.turn_list_calls = 0
        self.path = home / "rollout.jsonl"
        self.thread = {"id": "exact", "path": str(self.path), "historyMode": "legacy",
                       "turns": [], "status": {"type": "idle"},
                       "canAcceptDirectInput": True}
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
        if method == "thread/turns/list":
            self.turn_list_calls += 1
            page = self.turn_pages[params.get("cursor")]
            if isinstance(page, Exception):
                raise page
            return page
        if method == "thread/queue/list":
            return {"data": list(self.queued), "nextCursor": None}
        assert method == "thread/queue/add"
        pending = json.loads(next(self.path.parent.glob("codex-wakeful-*.json")).read_text())
        assert pending[params["clientUserMessageId"]]["state"] == "PENDING"
        assert pending[params["clientUserMessageId"]]["attempted"] is True
        self.calls.append(params)
        queued = {"id": f"queued-{len(self.calls)}", "input": params["input"],
                  "clientUserMessageId": params["clientUserMessageId"]}
        self.queued.append(queued)
        if self.lost:
            raise OSError("lost response")
        return {"queuedSubmission": queued}

    def consume(self):
        queued = self.queued.pop(0)
        self.thread["turns"].append({"id": "turn", "items": [{
            "type": "userMessage", "id": "item",
            "clientId": queued["clientUserMessageId"]}]})

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
    assert bind(client, home, token, binding.thread_id) == binding
    client.listed[-1]["path"] = "/outside/exact-home"
    assert bind(client, home, token, binding.thread_id) == binding
    assert bind(client, home, token, "missing") == "NOT_BOUND"
    client.listed.pop()
    assert bind(client, home, token) == binding
    client.thread["id"] = "replacement"
    p = Projection(home, binding)
    assert p.admit(client, WakeSourceRef("child_completion", "call/child/completed")) == "UNKNOWN"


def test_binding_detects_duplicate_on_later_filtered_page(setup):
    home, token, client, _binding = setup
    pages = {
        None: {"data": [client.listed[0]], "nextCursor": "later"},
        "later": {"data": [{"id": "duplicate", "path": str(client.path)}],
                  "nextCursor": None},
    }
    seen: list[dict[str, object]] = []

    def call(method, params):
        assert method == "thread/list"
        seen.append(params)
        return pages[params.get("cursor")]

    client.call = call
    assert bind(client, home, token) == "CONFLICT"
    assert all(page["cwd"] == str(Path.cwd()) for page in seen)
    assert all(page["sourceKinds"] == ["cli"] for page in seen)
    assert all(page["useStateDbOnly"] is True for page in seen)


def test_expected_binding_uses_exact_read_and_result_taxonomy(setup):
    home, token, client, binding = setup
    client.listed = []
    assert bind(client, home, token, "exact") == binding

    client.thread["id"] = "other"
    assert bind(client, home, token, "exact") == "NOT_BOUND"
    client.thread["id"] = "exact"
    client.thread["path"] = "/outside/exact-home"
    assert bind(client, home, token, "exact") == "NOT_BOUND"
    client.thread["path"] = str(client.path)
    client.record["payload"]["content"][0]["text"] = "different start record"
    client.path.write_text(json.dumps(client.record) + "\n")
    assert bind(client, home, token, "exact") == "NOT_BOUND"
    client.thread.pop("path")
    assert bind(client, home, token, "exact") == "UNAVAILABLE"

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
    assert p.admit(client, source) == ("UNKNOWN" if lost else "PENDING")
    assert json.loads(p.path.read_text())[wake_id(binding, source)]["state"] == (
        "PENDING" if lost else "QUEUED")
    assert Projection(home, binding).admit(client, source) == "PENDING"
    assert p.path.stat().st_mode & 0o777 == 0o600
    client.consume()
    assert Projection(home, binding).admit(client, source) == "ADMITTED"
    assert len(client.calls) == 1
    assert client.calls[0]["clientUserMessageId"] == wake_id(binding, source)
    queued_text = client.calls[0]["input"][0]["text"]
    assert "mcp__switchstand__agent_message_receive" in queued_text
    assert "Do not use the codex_apps Switchstand connector" in queued_text
    assert set(asdict(source)) == {"source_kind", "source_id"}
    replacement = CodexBinding(binding.thread_id, binding.start_record, "new-generation")
    assert wake_id(replacement, source) != wake_id(binding, source)

def test_busy_target_queues_and_unresolved_absence_never_resends(setup):
    home, _, client, binding = setup
    source = WakeSourceRef("switchstand_inbound", str(uuid4()))
    p = Projection(home, binding)
    client.thread.update(status={"type": "active", "activeFlags": []},
                         canAcceptDirectInput=False)
    assert p.admit(client, source) == "PENDING"
    assert len(client.calls) == 1
    client.queued = []
    client.thread["turns"] = []
    assert Projection(home, binding).admit(client, source) == "UNKNOWN"
    assert len(client.calls) == 1


@pytest.mark.asyncio
async def test_pending_wakeful_event_delivers_once_to_exact_root(setup):
    home, _, client, binding = setup
    mailbox = AgentMailbox(
        name="/root",
        name_key="root",
        endpoint_id=uuid4(),
        principal_key="principal",
        session_key="session",
        generation=1,
    )

    class Mailboxes:
        async def by_endpoint_id(self, _endpoint_id):
            return SimpleNamespace(status="ok", mailbox=mailbox)

    store = WakefulStore(home / "disk-pressure.sqlite3")
    now = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)
    lease = store.acquire_cycle(monitor="disk", now=now)
    assert lease is not None
    event = WakeEvent.create(
        source="switchstand.disk-pressure",
        subject="/",
        kind="disk.warning",
        severity=EventSeverity.WARNING,
        summary="Disk warning",
        observed_at=now,
    )
    store.complete_cycle(
        lease=lease,
        cursor="1",
        condition="warning",
        healthy=False,
        event=event,
        now=now,
    )

    results = await wakeful_event_cycle(
        Mailboxes(), mailbox, Projection(home, binding), client, store
    )

    assert list(results.values()) == ["PENDING"]
    assert store.pending() == ()
    assert len(client.calls) == 1
    queued = client.calls[0]["input"][0]["text"]
    assert f"event_id={event.event_id}" in queued
    assert "switchstand-disk-pressure status" in queued
    assert await wakeful_event_cycle(
        Mailboxes(), mailbox, Projection(home, binding), client, store
    ) == {}


@pytest.mark.asyncio
async def test_corrupt_optional_event_store_does_not_block_inbound(
    setup, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home, _, client, binding = setup
    mailbox = AgentMailbox(
        name="/root",
        name_key="root",
        endpoint_id=uuid4(),
        principal_key="principal",
        session_key=chat_session_key(f"codex:{binding.thread_id}"),
        generation=1,
    )

    class Mailboxes:
        async def by_endpoint_id(self, _endpoint_id):
            return SimpleNamespace(status="ok", mailbox=mailbox)

    class Messages:
        async def pending_delivery_ids(self, _mailbox, _cursor):
            return []

    state = tmp_path / "state"
    database = state / "switchstand/disk-pressure/wakeful.sqlite3"
    database.parent.mkdir(parents=True)
    database.write_bytes(b"not a sqlite database")
    monkeypatch.setenv("XDG_STATE_HOME", str(state))

    assert open_wakeful_event_store(mailbox) is None
    cursor, results = await inbound_cycle(
        Messages(), Mailboxes(), mailbox, Projection(home, binding), client
    )
    assert cursor is None and results == {}


def test_paginated_absence_queues_and_legacy_does_not_page(setup):
    home, _, client, binding = setup
    legacy = WakeSourceRef("switchstand_inbound", str(uuid4()))
    assert Projection(home, binding).admit(client, legacy) == "PENDING"
    assert client.turn_list_calls == 0

    client.thread.update(historyMode="paginated", turns=[])
    client.turn_pages = {None: {"data": [{"id": "seed", "itemsView": "full", "items": []}],
                                "nextCursor": None}}
    source = WakeSourceRef("switchstand_inbound", str(uuid4()))
    assert Projection(home, binding).admit(client, source) == "PENDING"
    assert client.turn_list_calls == 1
    assert len(client.calls) == 2


def test_paginated_multipage_consumed_identity_is_admitted(setup):
    home, _, client, binding = setup
    client.thread.update(historyMode="paginated", turns=[])
    source = WakeSourceRef("switchstand_inbound", str(uuid4()))
    identity = wake_id(binding, source)
    client.turn_pages = {
        None: {"data": [{"id": "first", "itemsView": "full", "items": []}],
               "nextCursor": "second"},
        "second": {"data": [{"id": "second", "itemsView": "full", "items": [{
            "type": "userMessage", "id": "consumed", "clientId": identity}]}],
                   "nextCursor": None},
    }
    assert Projection(home, binding).admit(client, source) == "ADMITTED"
    assert client.turn_list_calls == 2
    assert not client.calls


def test_paginated_full_turns_feed_child_oracle(setup):
    home, _, client, binding = setup
    client.thread.update(historyMode="paginated", turns=[])
    started = {"type": "subAgentActivity", "id": "call", "kind": "started",
               "agentThreadId": "child", "agentPath": "/root/child"}
    completed = {**started, "id": "done", "kind": "completed"}
    turn = {"id": "turn", "itemsView": "full", "items": [started, completed]}
    client.turn_pages = {None: {"data": [turn], "nextCursor": None}}
    source, = children({"turns": [turn]})
    assert Projection(home, binding).admit(client, source) == "PENDING"
    assert len(client.calls) == 1


@pytest.mark.parametrize("page", [
    {"data": [{"id": "partial", "itemsView": "summary", "items": []}],
     "nextCursor": None},
    {"data": [{"id": "partial", "itemsView": "full"}], "nextCursor": None},
    {"data": [], "nextCursor": ""},
    OSError("lost paginated history response"),
])
def test_paginated_incomplete_malformed_or_lost_history_is_unknown(setup, page):
    home, _, client, binding = setup
    client.thread.update(historyMode="paginated", turns=[])
    client.turn_pages = {None: page}
    source = WakeSourceRef("switchstand_inbound", str(uuid4()))
    assert Projection(home, binding).admit(client, source) == "UNKNOWN"
    assert not client.calls
    record = json.loads(Projection(home, binding).path.read_text())[wake_id(binding, source)]
    assert record["attempted"] is False


def test_paginated_cursor_cycle_is_unknown(setup):
    home, _, client, binding = setup
    client.thread.update(historyMode="paginated", turns=[])
    page = {"data": [], "nextCursor": "same"}
    client.turn_pages = {None: page, "same": page}
    source = WakeSourceRef("switchstand_inbound", str(uuid4()))
    assert Projection(home, binding).admit(client, source) == "UNKNOWN"
    assert client.turn_list_calls == 2
    assert not client.calls


def test_duplicate_durable_queue_identity_is_unknown(setup):
    home, _, client, binding = setup
    source = WakeSourceRef("switchstand_inbound", str(uuid4()))
    identity = wake_id(binding, source)
    queued = {"id": "first", "input": [], "clientUserMessageId": identity}
    client.queued = [queued, {**queued, "id": "second"}]
    assert Projection(home, binding).admit(client, source) == "UNKNOWN"
    assert not client.calls

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
    assert p.admit(client, source) == "PENDING"
    client.consume()
    assert Projection(home, binding).admit(client, children(thread)[0]) == "ADMITTED"
    assert len(client.calls) == 1


def test_current_host_subagent_activity_terminal_shape_and_ambiguity():
    started = {"type": "subAgentActivity", "id": "call_exact", "kind": "started",
               "agentThreadId": "child", "agentPath": "/root/private-name"}
    completed = {"type": "subAgentActivity", "id": "completion_event", "kind": "completed",
                 "agentThreadId": "child", "agentPath": "/root/private-name"}
    thread = {"turns": [{"items": [started, completed]}]}
    source, = children(thread)
    assert json.loads(source.source_id) == ["call_exact", "child", "completed"]
    assert "private-name" not in source.source_id
    thread["turns"][0]["items"].append({**started, "id": "second_start"})
    assert children(thread) == []


@pytest.mark.parametrize("current", [
    [
        {"type": "subAgentActivity", "id": "legacy_call", "kind": "started",
         "agentThreadId": "child", "agentPath": "/root/child"},
        {"type": "subAgentActivity", "id": "completion", "kind": "completed",
         "agentThreadId": "child", "agentPath": "/root/child"},
    ],
    [{"type": "subAgentActivity", "id": "current_call", "kind": "started",
      "agentThreadId": "child", "agentPath": "/root/child"}],
    [
        {"type": "subAgentActivity", "id": "current_call", "kind": "started",
         "agentThreadId": "child", "agentPath": "/root/child"},
        {"type": "subAgentActivity", "id": "second_start", "kind": "started",
         "agentThreadId": "child", "agentPath": "/root/child"},
        {"type": "subAgentActivity", "id": "completion", "kind": "completed",
         "agentThreadId": "child", "agentPath": "/root/child"},
    ],
])
def test_mixed_child_history_shapes_fail_closed(current):
    legacy = {"type": "collabAgentToolCall", "id": "legacy_call", "tool": "spawnAgent",
              "receiverThreadIds": ["child"],
              "agentsStates": {"child": {"status": "completed"}}}
    assert children({"turns": [{"items": [legacy, *current]}]}) == []
    assert children({"turns": [{"items": [{**legacy, "agentsStates": {}}, *current]}]}) == []

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
    path = Projection(home, binding).path
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

def test_queue_client_uses_private_stdio_app_server(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    observed = tmp_path / "observed.json"
    codex = tmp_path / "codex"
    codex.write_text(f"""#!{sys.executable}
import json, os, sys
from pathlib import Path
Path({str(observed)!r}).write_text(json.dumps({{"argv": sys.argv[1:], "home": os.environ.get("CODEX_HOME")}}))
for line in sys.stdin:
    request = json.loads(line)
    if "id" not in request:
        continue
    if request["method"] == "thread/queue/list":
        frames = [
            {{"jsonrpc": "2.0", "id": 999, "method": "fixture/request"}},
            {{"jsonrpc": "2.0", "id": request["id"],
              "result": {{"data": [], "nextCursor": None}}}},
        ]
        sys.stdout.write("".join(json.dumps(frame) + "\\n" for frame in frames))
        sys.stdout.flush()
        rejection = json.loads(sys.stdin.readline())
        assert rejection["id"] == 999 and rejection["error"]["code"] == -32601
        continue
    result = ({{"serverInfo": {{"name": "fixture", "version": "1"}}}}
              if request["method"] == "initialize"
              else {{"data": [], "nextCursor": None}})
    print(json.dumps({{"jsonrpc": "2.0", "id": request["id"], "result": result}}), flush=True)
""")
    codex.chmod(0o700)
    client = QueueClient(codex, home)
    assert client.process is None
    try:
        assert client.call("thread/queue/list", {"threadId": "exact"}) == {
            "data": [], "nextCursor": None}
    finally:
        process = client.process
        client.close()
    assert process is not None and process.poll() is not None
    assert json.loads(observed.read_text()) == {
        "argv": ["app-server", "--listen", "stdio://"], "home": str(home)}


def test_queue_client_uses_websocket_over_owned_socket_alias():
    temporary = tempfile.TemporaryDirectory(prefix="wf-", dir=Path.home() / ".cache")
    home = Path(temporary.name)
    codex = home / "codex"
    codex.write_text("")
    codex.chmod(0o700)
    target = home / "physical.sock"
    alias = home / "requested.sock"
    observed = []

    def handler(connection):
        observed.append(connection.request.path)
        initialize = json.loads(connection.recv())
        observed.append(initialize["method"])
        connection.send(json.dumps({
            "jsonrpc": "2.0", "id": initialize["id"],
            "result": {"serverInfo": {"name": "fixture", "version": "1"}},
        }))
        observed.append(json.loads(connection.recv())["method"])
        request = json.loads(connection.recv())
        connection.send(json.dumps({
            "jsonrpc": "2.0", "id": 999, "method": "fixture/request",
        }))
        rejection = json.loads(connection.recv())
        observed.append(rejection["error"]["code"])
        connection.send(json.dumps({
            "jsonrpc": "2.0", "id": request["id"],
            "result": {"data": [], "nextCursor": None},
        }))

    with unix_serve(handler, target) as server:
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        alias.symlink_to(target)
        client = QueueClient(codex, home, alias)
        try:
            assert client.call("thread/queue/list", {"threadId": "exact"}) == {
                "data": [], "nextCursor": None}
        finally:
            client.close()
            server.shutdown()
            thread.join(timeout=2)
    assert observed == ["/rpc", "initialize", "initialized", -32601]
    temporary.cleanup()


def test_queue_client_rejects_unproved_runtime(tmp_path):
    with pytest.raises(OSError, match="unproved Codex runtime"):
        QueueClient(tmp_path / "missing-codex", tmp_path / "missing-home")
