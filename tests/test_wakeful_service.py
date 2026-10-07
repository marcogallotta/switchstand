import asyncio
import json
from pathlib import Path
from uuid import uuid4

import pytest

from switchstand.agent_mailboxes import AgentMailbox, AgentMailboxState, chat_session_key
from switchstand.codex_wakeful import (
    CodexBinding,
    Projection,
    WakefulServiceConfig,
    endpoint_lock,
    load_service_config,
    resolve_current_binding,
    run_following_inbound,
)
from switchstand.messages import MessageRoute, MessageSubmitRequest
from switchstand.secure_file import atomic_replace_bytes

pytest_plugins = ["test_messages"]


def discovery_subject(home: Path):
    token = home / "start-commit.exact"
    atomic_replace_bytes(token, b"base\n")
    rollout = home / "rollout.jsonl"
    atomic_replace_bytes(rollout, (json.dumps({
        "type": "response_item",
        "payload": {"type": "message", "role": "developer", "content": [{
            "type": "input_text", "text": f"Coordinator start commit is recorded at {token}.",
        }]},
    }) + "\n").encode())
    mailbox = AgentMailbox(
        name="/root", name_key="root", endpoint_id=uuid4(), principal_key="principal",
        session_key=chat_session_key("codex:exact"), generation=18,
    )
    return token, {"id": "exact", "path": str(rollout)}, mailbox


class DiscoveryClient:
    def __init__(self, thread):
        self.thread = thread
        self.listed = [{"id": thread["id"], "path": thread["path"]}]
        self.list_parameters = []

    def call(self, method, params, *, deadline=None):
        if method == "thread/list":
            self.list_parameters.append(params)
            return {"data": list(self.listed), "nextCursor": None}
        if method == "thread/read":
            return {"thread": dict(self.thread)}
        raise AssertionError(method)


def test_strict_private_config_and_dynamic_binding(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    token, thread, mailbox = discovery_subject(home)
    codex = tmp_path / "codex"
    codex.write_text("#!/bin/sh\n")
    codex.chmod(0o700)
    path = tmp_path / "wakeful.json"
    body = {"version": 1, "mailbox_name": mailbox.name,
            "endpoint_id": str(mailbox.endpoint_id), "principal_key": mailbox.principal_key,
            "codex_home": str(home), "codex": str(codex)}
    atomic_replace_bytes(path, json.dumps(body).encode())
    assert load_service_config(path).endpoint_id == mailbox.endpoint_id

    client = DiscoveryClient(thread)
    binding = resolve_current_binding(client, home, mailbox)
    assert isinstance(binding, CodexBinding)
    assert Path(binding.start_record) == token
    assert all("cwd" not in params for params in client.list_parameters)
    client.listed.append(dict(client.listed[0]))
    assert resolve_current_binding(client, home, mailbox) == "CONFLICT"

    body["extra"] = True
    atomic_replace_bytes(path, json.dumps(body).encode())
    with pytest.raises(ValueError, match="configuration"):
        load_service_config(path)


async def test_service_follows_takeover_without_restart(subject, tmp_path, monkeypatch):
    messages, engine, _grants, principal, sender, _, _ = subject
    mailboxes = AgentMailboxState(engine)
    result = await mailboxes.register_agent("pilot-root", principal.key, "codex:first")
    mailbox = result.mailbox
    assert mailbox is not None
    submitted = await messages.submit_admitted(
        sender.authority.active_work_id,
        MessageRoute(recipient_work_id=mailbox.endpoint_id,
                     recipient_grant_version=mailbox.generation),
        MessageSubmitRequest(api_version="1", message_id=uuid4(), grant_version=1,
                             route_ref="agent.pilot-root", kind="request", payload="private"),
    )
    assert submitted.status == "ok" and submitted.message is not None
    home = tmp_path / "home"
    home.mkdir()
    token, _, _ = discovery_subject(home)
    config = WakefulServiceConfig(mailbox.name, mailbox.endpoint_id, mailbox.principal_key,
                                  home, tmp_path / "codex")
    admitted = set()

    class Client:
        failed = False

        def __init__(self, *_args): pass
        def connect(self): pass
        def close(self): pass

    monkeypatch.setattr("switchstand.codex_wakeful.QueueClient", Client)
    monkeypatch.setattr("switchstand.codex_wakeful.resolve_current_binding",
        lambda _client, _home, current, **_kwargs:
            CodexBinding(f"thread-{current.generation}", str(token), str(current.generation)))
    monkeypatch.setattr(Projection, "admit",
        lambda projection, _client, source, _deadline:
            admitted.add((projection.binding.generation, source.source_id)) or "PENDING")
    stop = asyncio.Event()
    task = asyncio.create_task(run_following_inbound(
        messages, mailboxes, config, stop, poll_seconds=0.01, admission_seconds=1,
    ))
    try:
        async with asyncio.timeout(2):
            while len(admitted) < 1:
                await asyncio.sleep(0.01)
        moved = await mailboxes.takeover(mailbox.name, mailbox.principal_key, "codex:second")
        assert moved.status == "ok"
        async with asyncio.timeout(2):
            while len(admitted) < 2:
                await asyncio.sleep(0.01)
    finally:
        stop.set()
        await task
    assert {generation for generation, _ in admitted} == {
        str(mailbox.generation), str(mailbox.generation + 1),
    }


def test_endpoint_lock_excludes_second_service(tmp_path):
    endpoint = uuid4()
    with endpoint_lock(tmp_path, endpoint), pytest.raises(BlockingIOError), \
            endpoint_lock(tmp_path, endpoint):
        pytest.fail("second service admitted")
