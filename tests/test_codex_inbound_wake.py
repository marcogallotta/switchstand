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
    wake._admit = AsyncMock(return_value=True)
    async def call(method, _params):
        return {"thread/read": {"thread": {"status": {"type": "idle"}}},
                "thread/turns/list": {"data": []}, "turn/start": {"turn": {"id": "t"}},
                "thread/resume": {}}[method]
    rpc = NS(call=AsyncMock(side_effect=call))
    return wake, rpc, binding, identity, delivery


@pytest.mark.parametrize("boundary", ["idle", "busy", "stop", "stop_after_read", "stale", "old", "receipt", "unknown"])
async def test_dispatch_boundaries(subject, boundary):
    wake, rpc, binding, identity, delivery = subject
    original = rpc.call.side_effect
    async def call(method, params):
        if method == "thread/read" and boundary == "stop_after_read" and rpc.call.await_count == 3:
            wake.control.write_text('{"enabled":false}')
        if method == "thread/read" and boundary == "busy":
            return {"thread": {"status": {"type": "active"}}}
        if method == "thread/turns/list" and boundary == "receipt":
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
    if boundary in {"old", "unknown"}:
        with pytest.raises(RuntimeError):
            await wake._reconcile(rpc, binding, identity)
    else:
        await wake._reconcile(rpc, binding, identity)
    starts = [c.args[1] for c in rpc.call.call_args_list if c.args[0] == "turn/start"]
    assert len(starts) == (1 if boundary in {"idle", "unknown"} else 0)
    if boundary == "idle":
        assert set(starts[0]) == {"threadId", "input"} and starts[0]["threadId"] == identity


async def test_receipts_ignore_nonuser_text_and_follow_pages(subject):
    _, rpc, _, identity, delivery = subject
    marker = {"type": "text", "text": module.MARKER + json.dumps([str(delivery)])}
    rpc.call.side_effect = [{"data": [{"items": [{"type": "agentMessage", "content": [marker]}]}],
                             "nextCursor": "next"},
                            {"data": [{"items": [{"type": "userMessage", "content": [marker]}]}]}]
    assert await module.receipts(rpc, identity) == {str(delivery)}
    assert rpc.call.call_args.kwargs == {} and rpc.call.call_args.args[1]["cursor"] == "next"


@pytest.mark.parametrize("uncertain", [False, True])
async def test_root_dirty_coalesces_and_attach_is_once(subject, monkeypatch, uncertain):
    wake, rpc, binding, identity, delivery = subject
    bindings = {binding.endpoint_id: binding}
    wake.mailboxes = NS(by_endpoint_id=AsyncMock(return_value=NS(mailbox=binding)))
    rpc.call.side_effect = lambda method, params: {"thread": {"status": {"type": "idle"},
        "parentThreadId": None, "canAcceptDirectInput": True}}
    @asynccontextmanager
    async def connection(_socket, dirty):
        dirty()
        asyncio.get_running_loop().call_later(.001, dirty)
        yield rpc
    monkeypatch.setattr(module, "DaemonConnection", connection)
    monkeypatch.setattr(module, "resolve_thread", AsyncMock(return_value=identity))
    active, peak, seen = 0, 0, set()
    async def reconcile(_rpc, b, *_args):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(.01)
        seen.add(b.endpoint_id)
        active -= 1
        if uncertain:
            raise module.UncertainTurn("acceptance unknown")
    wake._reconcile = reconcile
    for b in (binding,):
        wake.signal(NS(delivery_id=delivery, recipient_work_id=b.endpoint_id), True)
    for _ in range(100):
        if len(seen) == len(bindings):
            break
        await asyncio.sleep(.005)
    for b in (binding,):
        wake.signal(NS(delivery_id=delivery, recipient_work_id=b.endpoint_id), True)
    await asyncio.sleep(0)
    assert all(task.done() for task in wake.tasks.values()) == uncertain
    await wake.close()
    assert seen == set(bindings) and peak == 1
    assert sum(c.args[0] == "thread/resume" for c in rpc.call.call_args_list) == 1
