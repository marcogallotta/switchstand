"""Boundary tests: mailbox truth, same recipient, no competing or uncertain retries."""

import asyncio
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from switchstand.agent_mailboxes import AgentMailbox, chat_session_key
from switchstand.codex_inbound_wake import MARKER, InboundWake, receipts, resolve_thread


@pytest.fixture
def subject(tmp_path):
    identity = str(uuid4())
    binding = AgentMailbox(name="Test", name_key="test", endpoint_id=uuid4(),
        principal_key="principal", session_key=chat_session_key(f"codex:{identity}"), generation=1)
    delivery = uuid4()
    control = tmp_path / "control.json"
    control.write_text(json.dumps({"enabled": True, "socket": "/unused"}))
    admission = tmp_path / "admit"
    admission.write_text("#!/bin/sh\nexit 0\n")
    admission.chmod(0o700)
    @asynccontextmanager
    async def fence(*_args):
        yield True
    messages = SimpleNamespace(engine=None, pending_delivery_ids=AsyncMock(return_value=(delivery,)),
                               pending_delivery_fence=fence)
    wake = InboundWake(messages, control, admission)
    wake.fresh.add(delivery)
    rpc = SimpleNamespace(call=AsyncMock())
    async def call(method, _params):
        if method == "thread/turns/list":
            return {"data": [], "nextCursor": None}
        if method == "thread/read":
            return {"thread": {"status": {"type": "idle"}}}
        if method == "turn/start":
            return {"turn": {"id": "accepted"}}
        raise AssertionError(method)
    rpc.call.side_effect = call
    return wake, rpc, binding, identity, delivery


async def test_idle_uses_delivery_marker_and_preserves_config(subject):
    wake, rpc, binding, identity, delivery = subject
    await wake._reconcile(rpc, binding, identity)
    starts = [call.args[1] for call in rpc.call.call_args_list if call.args[0] == "turn/start"]
    assert len(starts) == 1
    assert set(starts[0]) == {"threadId", "input"}
    assert starts[0]["threadId"] == identity
    assert starts[0]["input"][0]["text"].startswith(MARKER + json.dumps([str(delivery)]))


@pytest.mark.parametrize("boundary", ["busy", "stop", "stale", "wait", "old_delivery"])
async def test_dispatch_boundaries_never_start(subject, boundary):
    wake, rpc, binding, identity, _delivery = subject
    original = rpc.call.side_effect
    async def call(method, params):
        if boundary == "busy" and method == "thread/read":
            return {"thread": {"status": {"type": "active"}}}
        return await original(method, params)
    rpc.call.side_effect = call
    if boundary == "stop":
        wake.control.write_text('{"enabled":false,"socket":"/unused"}')
    elif boundary == "stale":
        wake.messages.pending_delivery_ids.return_value = None
    elif boundary == "wait":
        wake.admission.write_text("#!/bin/sh\nexit 1\n")
    elif boundary == "old_delivery":
        wake.fresh.clear()
    if boundary == "old_delivery":
        with pytest.raises(RuntimeError, match="recovery unavailable"):
            await wake._reconcile(rpc, binding, identity)
    else:
        await wake._reconcile(rpc, binding, identity)
    assert not any(call.args[0] == "turn/start" for call in rpc.call.call_args_list)


async def test_duplicate_receipt_comes_only_from_matched_user_input(subject):
    wake, rpc, binding, identity, delivery = subject
    original = rpc.call.side_effect
    async def call(method, params):
        if method == "thread/turns/list":
            assert params["threadId"] == identity and params["itemsView"] == "full"
            return {"data": [{"items": [{"type": "userMessage", "content": [
                {"type": "text", "text": MARKER + json.dumps([str(delivery)]) + "\nreminder"}
            ]}]}], "nextCursor": None}
        return await original(method, params)
    rpc.call.side_effect = call
    await wake._reconcile(rpc, binding, identity)
    await wake._reconcile(rpc, binding, identity)
    assert wake.outcomes[binding.endpoint_id] == "RECEIPT"
    assert not any(call.args[0] == "turn/start" for call in rpc.call.call_args_list)


async def test_resolve_paginates_metadata_including_native_children(subject):
    _wake, rpc, binding, identity, _delivery = subject
    async def call(method, params):
        if method == "thread/loaded/list":
            return {"data": []}
        assert method == "thread/list" and params["useStateDbOnly"] is True
        assert "subAgentThreadSpawn" in params["sourceKinds"]
        if params["cursor"] is None:
            return {"data": [{"id": "unrelated"}], "nextCursor": "next"}
        return {"data": [{"id": identity}], "nextCursor": None}
    rpc.call.side_effect = call
    assert await resolve_thread(rpc, binding) == identity
    binding = binding.model_copy(update={"session_key": chat_session_key(identity)})
    with pytest.raises(RuntimeError, match="no unique recipient"):
        await resolve_thread(rpc, binding)


async def test_receipt_requires_complete_history_pages(subject):
    _wake, rpc, _binding, identity, delivery = subject
    rpc.call.side_effect = [
        {"data": [], "nextCursor": "older"},
        {"data": [{"items": [{"type": "userMessage", "content": [
            {"type": "text", "text": MARKER + json.dumps([str(delivery)])}]}]}]},
    ]
    assert await receipts(rpc, identity) == {str(delivery)}


async def test_dirty_arrival_remains_set_during_busy_pass(subject):
    wake, rpc, binding, identity, _delivery = subject
    event = asyncio.Event()
    original = rpc.call.side_effect
    async def call(method, params):
        if method == "thread/read":
            event.set()  # Arrival/completion during reconciliation must survive the pass.
            return {"thread": {"status": {"type": "active"}}}
        return await original(method, params)
    rpc.call.side_effect = call
    event.set()
    await event.wait()
    event.clear()
    await wake._reconcile(rpc, binding, identity)
    assert event.is_set()


async def test_lost_start_response_is_unknown_never_a_retry(subject):
    from switchstand.codex_inbound_wake import UncertainTurn
    wake, rpc, binding, identity, _delivery = subject
    original = rpc.call.side_effect
    async def call(method, params):
        if method == "turn/start":
            raise ConnectionError("response lost after send")
        return await original(method, params)
    rpc.call.side_effect = call
    with pytest.raises(UncertainTurn, match="no replay"):
        await wake._reconcile(rpc, binding, identity)
    assert sum(call.args[0] == "turn/start" for call in rpc.call.call_args_list) == 1


@pytest.mark.parametrize("completion", ["before_attach", "during_attach", "after_busy"])
async def test_subscription_races_reconcile_busy_to_idle(subject, monkeypatch, completion):
    import switchstand.codex_inbound_wake as module
    wake, rpc, binding, identity, delivery = subject
    wake.mailboxes = SimpleNamespace(by_endpoint_id=AsyncMock(
        return_value=SimpleNamespace(mailbox=binding)))
    dirty = None
    active = completion == "after_busy"
    seen = False
    async def call(method, params):
        nonlocal active, seen
        if method == "thread/resume":
            assert params == {"threadId": identity, "excludeTurns": True}
            if completion == "during_attach":
                dirty()
            return {}
        if method == "thread/read":
            if active:
                active = False
                asyncio.get_running_loop().call_soon(dirty)
                return {"thread": {"status": {"type": "active"}}}
            return {"thread": {"status": {"type": "idle"}}}
        if method == "thread/turns/list":
            return {"data": [{"items": [{"type": "userMessage", "content": [
                {"type": "text", "text": MARKER + json.dumps([str(delivery)])}]}]}]
                if seen else []}
        if method == "turn/start":
            seen = True
            return {"turn": {"id": "once"}}
        raise AssertionError(method)
    rpc.call.side_effect = call
    class Connection:
        def __init__(self, _socket, signal):
            nonlocal dirty
            dirty = signal
        async def __aenter__(self):
            return rpc
        async def __aexit__(self, *_args):
            return None
    monkeypatch.setattr(module, "DaemonConnection", Connection)
    monkeypatch.setattr(module, "resolve_thread", AsyncMock(return_value=identity))
    event = asyncio.Event()
    task = asyncio.create_task(wake._run(binding.endpoint_id, event))
    for _ in range(100):
        if seen:
            break
        await asyncio.sleep(.001)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert seen
    assert sum(call.args[0] == "turn/start" for call in rpc.call.call_args_list) == 1
