"""Wake boundaries: exact receipts, current bindings and one owning Root at a time."""
import asyncio
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

import switchstand.codex_inbound_wake as module
from switchstand.agent_mailboxes import AgentMailbox, chat_session_key


@pytest.fixture
def subject(tmp_path):
    identity, delivery = str(uuid4()), uuid4()
    binding = AgentMailbox(name="Test", name_key="test", endpoint_id=uuid4(),
        principal_key="p", session_key=chat_session_key(f"codex:{identity}"), generation=1)
    control = tmp_path / "control"
    control.write_text('{"enabled":true,"socket":"/unused"}')
    @asynccontextmanager
    async def fence(*_args):
        yield True
    messages = NS(engine=None, pending_delivery_ids=AsyncMock(return_value=(delivery,)),
                  pending_delivery=AsyncMock(return_value=True), pending_delivery_fence=fence)
    wake = module.InboundWake(messages, control, tmp_path / "admit")
    wake.fresh.add(delivery)
    wake.fresh_messages[delivery] = NS(route_ref="ordinary.request", payload={})
    wake._admit = AsyncMock(return_value=True)
    async def call(method, _params):
        return {"thread/read": {"thread": {"status": {"type": "idle"}}},
                "thread/turns/list": {"data": []}, "turn/start": {"turn": {"id": "t"}},
                "thread/resume": {}}[method]
    rpc = NS(call=AsyncMock(side_effect=call))
    return wake, rpc, binding, identity, delivery


@pytest.mark.parametrize("boundary", ["idle", "busy", "stop", "stop_after_read", "stale", "old", "oldnew", "review", "receipt", "routed", "unknown"])
async def test_dispatch_boundaries(subject, boundary):
    wake, rpc, binding, identity, delivery = subject
    if boundary == "oldnew":
        wake.messages.pending_delivery_ids.return_value = (uuid4(), delivery)
    if boundary == "review":
        review_id = str(uuid4())
        wake.fresh_messages[delivery] = NS(route_ref="review.request", payload={
            "brief": {"basis": {"review_id": review_id}}})
    original = rpc.call.side_effect
    async def call(method, params):
        if method == "thread/read" and boundary == "stop_after_read" and rpc.call.await_count == 3:
            wake.control.write_text('{"enabled":false}')
        if method == "thread/read" and boundary == "busy":
            return {"thread": {"status": {"type": "active"}}}
        if method == "thread/turns/list" and boundary in {"receipt", "routed"}:
            return {"data": [{"items": [{"type": "userMessage", "content": [
                {"type": "text", "text": module.MARKER + json.dumps([str(delivery)])}]}]}]}
        if method == "turn/start" and boundary == "unknown":
            raise ConnectionError("acceptance lost")
        return await original(method, params)
    rpc.call.side_effect = call
    if boundary == "stop":
        wake.control.write_text('{"enabled":false}')
    if boundary == "stale":
        wake.messages.pending_delivery_ids.return_value = None
    if boundary == "old":
        wake.fresh.clear()
    if boundary == "unknown":
        with pytest.raises(RuntimeError):
            await wake._reconcile(rpc, binding, identity)
    else:
        await wake._reconcile(rpc, binding, identity, path="/root/head" if boundary == "routed" else None)
    starts = [c.args[1] for c in rpc.call.call_args_list if c.args[0] == "turn/start"]
    assert len(starts) == (1 if boundary in {"idle", "oldnew", "review", "unknown"} else 0)
    if boundary == "oldnew":
        assert json.loads(starts[0]["input"][0]["text"].split("\n", 1)[0][len(module.MARKER):]) == [str(delivery)]
    if boundary == "old":
        assert wake.outcomes[binding.endpoint_id] == "OLDER_UNCERTAIN"
    if boundary == "review":
        assert f"review_id={review_id}" in starts[0]["input"][0]["text"]
        assert "use review_submit, never ordinary result_send" in starts[0]["input"][0]["text"]
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
    marker = {"type": "text", "text": module.MARKER + json.dumps([str(delivery)])}
    rpc.call.side_effect = [{"data": [{"items": [{"type": "agentMessage", "content": [marker]}]}],
                             "nextCursor": "next"},
                            {"data": [{"items": [{"type": "userMessage", "content": [marker]}]}]}]
    assert await module.receipts(rpc, identity) == {str(delivery)}
    assert rpc.call.call_args.kwargs == {} and rpc.call.call_args.args[1]["cursor"] == "next"


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
    assert all(task.done() for task in wake.tasks.values()) == (failure == "uncertain")
    if failure == "rejected":
        assert attempts >= 4
    if failure == "resume_rejected":
        assert resume_calls == 3 and attempts >= 2
    await wake.close()
    assert seen == set(bindings) and peak == 1
    assert sum(c.args[0] == "thread/resume" for c in rpc.call.call_args_list) == resume_calls
