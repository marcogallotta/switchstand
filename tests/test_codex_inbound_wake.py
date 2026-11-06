"""Wake boundaries: exact receipts, current bindings and one owning Root at a time."""
import asyncio
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

import switchstand.mailbox.codex_inbound_wake as module
from switchstand.agent_mailboxes import AgentMailbox, chat_session_key
from switchstand.mailbox_wake import MARKER


@pytest.fixture
def subject(tmp_path):
    identity, delivery = str(uuid4()), uuid4()
    binding = AgentMailbox(name="Test", name_key="test", endpoint_id=uuid4(),
        principal_key="p", agent_type="codex", session_key=chat_session_key(f"codex:{identity}"), generation=1)
    control = tmp_path / "control"
    control.write_text('{"enabled":true,"socket":"/unused"}')
    @asynccontextmanager
    async def fence(*_args):
        yield True
    messages = NS(engine=None, all_pending_delivery_ids=AsyncMock(return_value=(delivery,)),
                  pending_delivery=AsyncMock(return_value=True), pending_delivery_fence=fence)
    wake = module.InboundWake(messages, control, tmp_path / "admit")
    wake.fresh.add(delivery)
    wake.fresh_messages[delivery] = NS(route_ref="ordinary.request", payload={})
    wake._admit = AsyncMock(return_value=True)
    wake._alert = AsyncMock()
    async def call(method, _params):
        return {"thread/read": {"thread": {"status": {"type": "idle"}}},
                "thread/turns/list": {"data": []}, "turn/start": {"turn": {"id": "t"}},
                "thread/resume": {}}[method]
    rpc = NS(call=AsyncMock(side_effect=call))
    return wake, rpc, binding, identity, delivery


@pytest.mark.parametrize("boundary", ["idle", "busy", "stop", "stop_after_read", "stale", "old", "oldnew", "review", "review_result", "acquisition_result", "receipt", "routed", "unknown", "malformed"])
async def test_dispatch_boundaries(subject, boundary):
    wake, rpc, binding, identity, delivery = subject
    if boundary == "oldnew":
        wake.messages.all_pending_delivery_ids.return_value = (uuid4(), delivery)
    if boundary in {"review", "review_result"}:
        review_id = str(uuid4())
        wake.fresh_messages[delivery] = NS(route_ref="review.request",
            kind="request" if boundary == "review" else "result", payload={
            "brief": {"basis": {"review_id": review_id}}})
    if boundary == "acquisition_result":
        review_id = str(uuid4())
        wake.fresh_messages[delivery] = NS(
            route_ref="review.acquisition", kind="result", payload={
                "review_id": review_id, "reviewer_delivery_id": str(uuid4()),
                "reviewer_name": "Coordinator"})
    original = rpc.call.side_effect
    async def call(method, params):
        if method == "thread/read" and boundary == "stop_after_read" and rpc.call.await_count == 3:
            wake.control.write_text('{"enabled":false}')
        if method == "thread/read" and boundary == "busy":
            return {"thread": {"status": {"type": "active"}}}
        if method == "thread/turns/list" and boundary in {"receipt", "routed"}:
            return {"data": [{"items": [{"type": "userMessage", "content": [
            {"type": "text", "text": MARKER + json.dumps([str(delivery)])}]}]}]}
        if method == "turn/start" and boundary == "malformed":
            return {"turn": {"id": None}}
        if method == "turn/start" and boundary == "unknown":
            raise ConnectionError("acceptance lost")
        return await original(method, params)
    rpc.call.side_effect = call
    if boundary == "stop":
        wake.control.write_text('{"enabled":false}')
    if boundary == "stale":
        wake.messages.all_pending_delivery_ids.return_value = None
    if boundary == "old":
        wake.fresh.clear()
    if boundary in {"unknown", "malformed"}:
        with pytest.raises(module.UncertainTurn):
            await wake._reconcile(rpc, binding, identity)
    else:
        await wake._reconcile(rpc, binding, identity, path="/root/head" if boundary == "routed" else None)
    starts = [c.args[1] for c in rpc.call.call_args_list if c.args[0] == "turn/start"]
    assert len(starts) == (1 if boundary in {"idle", "oldnew", "review", "review_result", "acquisition_result", "unknown", "malformed"} else 0)
    if boundary == "oldnew":
        assert json.loads(starts[0]["input"][0]["text"].split("\n", 1)[0][len(MARKER):]) == [str(delivery)]
    if boundary == "old":
        assert wake.outcomes[binding.endpoint_id] == "OLDER_UNCERTAIN"
    if boundary == "review":
        assert f"review_id={review_id}" in starts[0]["input"][0]["text"]
        assert "use review_submit, never ordinary result_send" in starts[0]["input"][0]["text"]
    if boundary == "review_result":
        assert f"review_id={review_id}" in starts[0]["input"][0]["text"]
        assert "use review_get; never ordinary result_send" in starts[0]["input"][0]["text"]
    if boundary == "acquisition_result":
        assert f"review_id={review_id}" in starts[0]["input"][0]["text"]
        assert "use review_get; never ordinary result_send" in starts[0]["input"][0]["text"]
    if boundary == "idle":
        assert set(starts[0]) == {"threadId", "input"} and starts[0]["threadId"] == identity
    if boundary == "routed":
        assert wake.outcomes[binding.endpoint_id] == "ROUTED_PENDING"


@pytest.mark.parametrize("boundary", ["go", "token", "session", "stop", "generation", "wait"])
async def test_gate_rechecks_after_admission(subject, boundary):
    wake, rpc, binding, identity, delivery = subject
    owner = binding.model_copy(update={"endpoint_id": uuid4()})
    current = owner
    wake.mailboxes = NS(by_endpoint_id=AsyncMock(side_effect=lambda _: NS(mailbox=current)))
    rpc.call.side_effect = None
    rpc.call.return_value = {"thread": {"parentThreadId": "root", "source": {
        "subAgent": {"thread_spawn": {"agent_path": "/root/head"}}}}}
    wake.routes["secret"] = (rpc, binding, identity, owner, "root", "/root/head", delivery)
    async def admitted():
        nonlocal current
        if boundary == "stop":
            wake.control.write_text('{"enabled":false}')
        if boundary == "generation":
            current = owner.model_copy(update={"generation": 2})
        return boundary != "wait"
    wake._admit.side_effect = admitted
    result = await wake.check_route("wrong" if boundary == "token" else "secret",
                                   "wrong" if boundary == "session" else "root")
    assert result["status"] == {"go": "GO", "token": "DENIED", "session": "DENIED",
                               "stop": "STOP", "generation": "STALE", "wait": "WAIT"}[boundary]


async def test_receipts_ignore_nonuser_text_and_follow_pages(subject):
    _, rpc, _, identity, delivery = subject
    forbidden_delivery = uuid4()
    forbidden_marker = {"type": "text", "text": MARKER + json.dumps([str(forbidden_delivery)])}
    marker = {"type": "text", "text": MARKER + json.dumps([str(delivery)])}
    rpc.call.side_effect = [{"data": [{"items": [{"type": "agentMessage", "content": [forbidden_marker]}]}],
                             "nextCursor": "next"},
                            {"data": [{"items": [{"type": "userMessage", "content": [marker]}]}]}]
    assert await module.receipts(rpc, identity) == {str(delivery)}
    assert [(call.args[0], call.args[1]["threadId"], call.args[1]["cursor"])
            for call in rpc.call.call_args_list] == [
                ("thread/turns/list", identity, None),
                ("thread/turns/list", identity, "next"),
            ]


@pytest.mark.parametrize("direct_input", [False, None])
async def test_standalone_root_requires_direct_input_capability(subject, monkeypatch, direct_input):
    wake, rpc, binding, identity, delivery = subject
    wake.mailboxes = NS(by_endpoint_id=AsyncMock(return_value=NS(mailbox=binding)))
    thread = {"parentThreadId": None, "status": {"type": "idle"}}
    if direct_input is not None:
        thread["canAcceptDirectInput"] = direct_input
    rpc.call.side_effect = None
    rpc.call.return_value = {"thread": thread}

    @asynccontextmanager
    async def connection(_socket, dirty):
        dirty()
        yield rpc

    monkeypatch.setattr(module, "DaemonConnection", connection)
    monkeypatch.setattr(module, "resolve_thread", AsyncMock(return_value=identity))
    wake._reconcile = AsyncMock()
    wake.signal(NS(delivery_id=delivery, recipient_work_id=binding.endpoint_id), True)
    await asyncio.wait_for(wake.tasks[binding.endpoint_id], 1)
    wake._reconcile.assert_not_awaited()
    assert "externally wakeable Root" in wake.outcomes[binding.endpoint_id]
    assert not any(c.args[0] in {"thread/resume", "turn/start"} for c in rpc.call.call_args_list)
    await wake.close()


@pytest.mark.parametrize("failure", [None, "uncertain", "rejected", "resume_rejected"])
async def test_same_root_serializes_heads_and_attach_is_once(subject, monkeypatch, failure):
    wake, rpc, binding, identity, delivery = subject
    other = binding.model_copy(update={"endpoint_id": uuid4()})
    owner = binding.model_copy(update={"endpoint_id": uuid4()})
    bindings = {b.endpoint_id: b for b in (binding, other)}
    wake.mailboxes = NS(by_endpoint_id=AsyncMock(side_effect=lambda key: NS(mailbox=bindings[key])),
                        for_session=AsyncMock(return_value=NS(mailbox=owner)))
    resume_calls = 0
    def call(method, params):
        nonlocal resume_calls
        if method == "thread/resume":
            resume_calls += 1
            if failure == "resume_rejected" and resume_calls == 1:
                raise module.DaemonRejected("no effect accepted")
        return ({"thread": {"status": {"type": "idle"},
            "parentThreadId": None, "canAcceptDirectInput": True}} if params.get("threadId") == "root"
            else {"thread": {"parentThreadId": "root", "source": {"subAgent": {"thread_spawn": {
                "parent_thread_id": "root", "depth": 1, "agent_path": "/root/head"}}}}})
    rpc.call.side_effect = call
    @asynccontextmanager
    async def connection(_socket, dirty):
        dirty()
        asyncio.get_running_loop().call_later(.001, dirty)
        yield rpc
    monkeypatch.setattr(module, "DaemonConnection", connection)
    monkeypatch.setattr(module, "resolve_thread", AsyncMock(return_value=identity))
    active, peak, seen, attempts = 0, 0, set(), 0
    async def reconcile(_rpc, b, *_args):
        nonlocal active, peak, attempts
        attempts += 1
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(.01)
        seen.add(b.endpoint_id)
        active -= 1
        if failure == "uncertain":
            raise module.UncertainTurn("acceptance unknown")
        if failure == "rejected":
            raise module.DaemonRejected("no effect accepted")
    wake._reconcile = reconcile
    for b in (binding, other):
        wake.signal(NS(delivery_id=delivery, recipient_work_id=b.endpoint_id), True)
    for _ in range(100):
        if len(seen) == 2:
            break
        await asyncio.sleep(.005)
    for b in (binding, other):
        wake.signal(NS(delivery_id=delivery, recipient_work_id=b.endpoint_id), True)
    await asyncio.sleep(.03)
    assert bool(wake.suspended) == (failure == "uncertain")
    if failure == "uncertain":
        assert attempts == 2
    if failure == "rejected":
        assert attempts >= 4
    if failure == "resume_rejected":
        assert resume_calls == 3 and attempts >= 2
    await wake.close()
    assert seen == set(bindings) and peak == 1
    assert sum(c.args[0] == "thread/resume" for c in rpc.call.call_args_list) == resume_calls


@pytest.mark.parametrize("owner", ["first", "second", "neither", "both"])
async def test_only_unique_owning_daemon_is_attached(subject, monkeypatch, owner):
    wake, _rpc, binding, identity, delivery = subject
    wake.control.write_text('{"enabled":true,"sockets":["/first","/second"]}')
    wake.sockets = ("/first", "/second")
    wake.mailboxes = NS(by_endpoint_id=AsyncMock(return_value=NS(mailbox=binding)))
    peers = {}
    @asynccontextmanager
    async def connect(socket, dirty):
        async def call(method, params):
            if method in {"thread/list", "thread/loaded/list"}:
                owns = owner == "both" or socket == "/" + owner
                return {"data": [identity] if owns else []}
            return {"thread": {"parentThreadId": None, "canAcceptDirectInput": True}}
        peers[socket] = NS(call=AsyncMock(side_effect=call))
        yield peers[socket]
    monkeypatch.setattr(module, "DaemonConnection", connect)
    wake._reconcile = AsyncMock(side_effect=module.UncertainTurn("test ends after unique attachment"))
    wake.signal(NS(delivery_id=delivery, recipient_work_id=binding.endpoint_id), True)
    await asyncio.wait_for(wake.tasks[binding.endpoint_id], 1)
    for socket, rpc in peers.items():
        attached = any(c.args[0] == "thread/resume" for c in rpc.call.call_args_list)
        assert attached == (socket == "/" + owner)
    assert wake._reconcile.await_count == (1 if owner in {"first", "second"} else 0)
    await wake.close()


@pytest.mark.parametrize("agent_type", [None, "claude", "chatgpt"])
async def test_other_runtime_never_discovers_or_suspends(subject, monkeypatch, agent_type):
    wake, _, binding, _, delivery = subject
    binding = binding.model_copy(update={"agent_type": agent_type})
    wake.mailboxes = NS(by_endpoint_id=AsyncMock(return_value=NS(mailbox=binding)))
    connect = AsyncMock(side_effect=AssertionError("must not contact Codex"))
    monkeypatch.setattr(module, "DaemonConnection", connect)
    wake.signal(NS(delivery_id=delivery, recipient_work_id=binding.endpoint_id), True)
    await wake.tasks[binding.endpoint_id]
    connect.assert_not_called()
    assert binding.endpoint_id not in wake.suspended
    assert delivery not in wake.fresh
    assert delivery not in wake.fresh_messages
    wake._alert.assert_not_awaited()


async def test_unloaded_root_resumes_before_capability_check(subject):
    wake, rpc, _, identity, _ = subject
    rpc.call.side_effect = [
        {"thread": {"parentThreadId": None, "status": {"type": "notLoaded"},
                    "canAcceptDirectInput": None}}, {},
        {"thread": {"parentThreadId": None, "status": {"type": "idle"},
                    "canAcceptDirectInput": True}},
    ]
    assert (await wake._root(rpc, identity))["canAcceptDirectInput"]
    assert [c.args[0] for c in rpc.call.call_args_list] == [
        "thread/read", "thread/resume", "thread/read"]


@pytest.mark.parametrize("owner", ["dead_reader", "unrelated_offline", "missing", "multiple"])
async def test_discovery_recovers_and_reports_failure(subject, monkeypatch, caplog, owner):
    wake, _rpc, binding, identity, delivery = subject
    wake.sockets = ("/first", "/second")
    wake.mailboxes = NS(by_endpoint_id=AsyncMock(return_value=NS(mailbox=binding)))
    calls = []
    @asynccontextmanager
    async def connect(socket, dirty):
        calls.append(socket)
        if socket == "/second" and owner == "unrelated_offline":
            raise ConnectionError("offline unrelated daemon")
        async def call(method, params):
            if method in {"thread/list", "thread/loaded/list"}:
                return {"data": [identity] if owner != "missing" and (
                    socket == "/first" or owner == "multiple") else []}
            return {"thread": {"parentThreadId": None, "canAcceptDirectInput": True,
                               "status": {"type": "idle"}}}
        peer = NS(call=AsyncMock(side_effect=call), socket=socket, reader=None)
        yield peer
    monkeypatch.setattr(module, "DaemonConnection", connect)
    attempts = 0
    async def reconcile(peer, *_):
        nonlocal attempts
        attempts += 1
        if owner == "dead_reader" and attempts == 1:
            peer.reader = asyncio.create_task(asyncio.sleep(0))
            await peer.reader
            wake.events[binding.endpoint_id].set()
            return
        raise module.UncertainTurn("test ends at dispatch boundary")
    wake._reconcile = reconcile
    wake.signal(NS(delivery_id=delivery, recipient_work_id=binding.endpoint_id), True)
    await wake.tasks[binding.endpoint_id]
    if owner == "dead_reader":
        assert calls.count("/first") == 2 and attempts == 2
    if owner == "unrelated_offline":
        assert attempts == 1
    if owner in {"missing", "multiple"}:
        assert owner in wake.outcomes[binding.endpoint_id]
        assert attempts == 0
    wake._alert.assert_awaited_once()
    assert '"generation": 1' in caplog.text and '"discovery":' in caplog.text
    await wake.close()


async def test_committed_discovery_baselines_old_mail_and_deduplicates_callback(subject):
    wake, _, binding, _, delivery = subject
    old = NS(delivery_id=uuid4(), recipient_work_id=binding.endpoint_id)
    new = NS(delivery_id=delivery, recipient_work_id=binding.endpoint_id)
    wake.messages.wake_pending = AsyncMock(return_value=(old,))
    wake._run = AsyncMock()
    await wake.start()
    wake.messages.wake_pending.return_value = (old, new)
    await asyncio.sleep(1.05)
    wake._run.assert_awaited_once()
    assert old.delivery_id not in wake.fresh and delivery in wake.fresh
    await asyncio.sleep(1.05)
    wake._run.assert_awaited_once()
    await wake.close()


async def test_failure_alert_is_one_durable_mail_to_claude(subject):
    wake, _, binding, _, _ = subject
    wake.mailboxes = NS(by_name=AsyncMock(return_value=NS(mailbox=binding)))
    wake.messages.submit_admitted = AsyncMock(return_value=NS(status="ok"))
    await module.InboundWake._alert(wake, {"generation": 1, "phase": "discovery"},
                                    RuntimeError("missing route"))
    wake.messages.submit_admitted.assert_awaited_once()
    args = wake.messages.submit_admitted.call_args.args
    assert args[1].recipient_work_id == binding.endpoint_id
    assert args[2].route_ref == "agent.claude-coordinator"
    assert args[2].payload["error"] == "missing route"
