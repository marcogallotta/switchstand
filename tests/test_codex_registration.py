from __future__ import annotations

import json
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from switchstand.agent_mailboxes import chat_session_key
from switchstand.codex_registration import (
    database_url,
    existing_registration,
    freeze_config,
    prepare_registration,
)
from switchstand.codex_wakeful import CodexBinding


@dataclass(frozen=True)
class Spec:
    home: Path
    codex: Path
    start_record: Path
    default_name: str
    environment_file: Path
    profile_path: Path
    mcp_server: str = "switchstand"

    @property
    def socket_path(self) -> Path:
        return self.home / "wakeful-app-server-exact.sock"


@pytest.fixture
def spec(tmp_path: Path) -> Spec:
    tmp_path.chmod(0o700)
    codex = tmp_path / "codex"
    codex.write_bytes(b"binary")
    codex.chmod(0o700)
    start = tmp_path / "start-commit.exact"
    start.write_text("sha\n")
    start.chmod(0o600)
    environment = tmp_path / ".env"
    environment.write_text("DATABASE_URL=postgresql://exact\n")
    environment.chmod(0o600)
    profile = tmp_path / "profile.toml"
    profile.write_text(f'developer_instructions = "use {start}"\n')
    return Spec(tmp_path, codex, start, "codex-head-exact", environment, profile)


def test_database_url_requires_private_owned_environment(spec: Spec) -> None:
    assert database_url(spec.environment_file) == "postgresql://exact"
    spec.environment_file.chmod(0o644)
    with pytest.raises(ValueError, match="mode-0600"):
        database_url(spec.environment_file)


def test_prepare_materializes_resumable_exact_launch_owned_thread(
    spec: Spec, monkeypatch,
) -> None:
    calls: list[tuple[str, dict[str, object]]] = []
    binding = CodexBinding("thread-1", str(spec.start_record), spec.start_record.name)
    rollout = spec.home / "rollout-thread-1.jsonl"
    developer = ""

    class Client:
        def __init__(self, codex: Path, home: Path, socket_path: Path):
            assert (codex, home, socket_path) == (spec.codex, spec.home, spec.socket_path)

        def call(self, method: str, arguments: dict[str, object]):
            nonlocal developer
            calls.append((method, arguments))
            if method == "thread/start":
                developer = str(arguments["developerInstructions"])
                return {"thread": {"id": "thread-1"}}
            if method == "thread/inject_items":
                rollout.write_text(json.dumps({
                    "type": "response_item",
                    "payload": {
                        "type": "message", "role": "developer",
                        "content": [{"type": "input_text", "text": developer}],
                    },
                }) + "\n")
                return {}
            if method == "thread/read":
                assert rollout.exists(), "zero-turn thread was not materialized before readback"
                return {"thread": {"id": "thread-1", "path": str(rollout)}}
            return {"structuredContent": {"status": "ok", "name": spec.default_name}}

        def close(self) -> None:
            calls.append(("closed", {}))

    async def existing(*_args):
        return "missing", None

    async def freeze(*_args):
        return spec.home / "runner.json"

    monkeypatch.setattr("switchstand.codex_registration.QueueClient", Client)
    monkeypatch.setattr("switchstand.codex_registration.existing_registration", existing)
    monkeypatch.setattr("switchstand.codex_registration.freeze_config", freeze)

    assert prepare_registration(spec) == (
        spec.home / "runner.json", "postgresql://exact", binding,
    )
    assert calls[0][0] == "thread/start"
    assert calls[0][1]["approvalPolicy"] == "never"
    assert calls[0][1]["sandbox"] == "danger-full-access"
    assert calls[0][1]["config"] == {
        "developer_instructions": f"use {spec.start_record}",
        "bypass_hook_trust": True,
    }
    assert "permissions" not in calls[0][1]
    assert [method for method, _arguments in calls[:3]] == [
        "thread/start", "thread/inject_items", "thread/read",
    ]
    assert calls[1][1] == {
        "threadId": "thread-1",
        "items": [{
            "type": "message", "role": "developer",
            "content": [{
                "type": "input_text",
                "text": (
                    "Switchstand Wakeful bootstrap only: this message materializes the "
                    "durable thread, is not a user assignment, and grants no authority."
                ),
            }],
        }],
    }
    assert calls[3] == ("mcpServer/tool/call", {
        "threadId": "thread-1", "server": "switchstand", "tool": "agent_register",
        "arguments": {"api_version": "1", "name": spec.default_name},
    })
    assert (spec.home / "wakeful-thread-exact").read_text() == "thread-1\n"


async def _async_value(value):
    return value


def test_prepare_never_automatically_takes_over_root(spec: Spec, monkeypatch) -> None:
    root = replace(spec, default_name="/root")
    binding = CodexBinding("thread-1", str(root.start_record), root.start_record.name)

    class Client:
        def __init__(self, *_args):
            pass

        def call(self, method: str, _arguments: dict[str, object]):
            if method == "thread/start":
                return {"thread": {"id": "thread-1"}}
            if method == "thread/inject_items":
                return {}
            raise AssertionError("root takeover must require explicit authorization")

        def close(self) -> None:
            pass

    monkeypatch.setattr("switchstand.codex_registration.QueueClient", Client)
    monkeypatch.setattr("switchstand.codex_registration.bind", lambda *_args: binding)
    monkeypatch.setattr(
        "switchstand.codex_registration.existing_registration",
        lambda *_args: _async_value(("takeover", "/root")),
    )
    with pytest.raises(ValueError, match="explicit takeover"):
        prepare_registration(root)


def test_prepare_retires_unbound_persisted_thread(spec: Spec, monkeypatch) -> None:
    thread = spec.home / "wakeful-thread-exact"
    thread.write_text("stale-thread\n")
    thread.chmod(0o600)
    calls: list[str] = []
    fresh = CodexBinding("fresh-thread", str(spec.start_record), spec.start_record.name)

    class Client:
        def __init__(self, *_args):
            pass

        def call(self, method: str, _arguments: dict[str, object]):
            calls.append(method)
            if method == "thread/start":
                return {"thread": {"id": "fresh-thread"}}
            return {"structuredContent": {"status": "ok", "name": spec.default_name}}

        def close(self) -> None:
            pass

    monkeypatch.setattr("switchstand.codex_registration.QueueClient", Client)
    monkeypatch.setattr(
        "switchstand.codex_registration.bind",
        lambda _client, _home, _record, thread_id: (
            fresh if thread_id == "fresh-thread" else "NOT_BOUND"
        ),
    )
    monkeypatch.setattr(
        "switchstand.codex_registration.existing_registration",
        lambda *_args: _async_value(("missing", None)),
    )
    monkeypatch.setattr(
        "switchstand.codex_registration.freeze_config",
        lambda *_args: _async_value(spec.home / "runner.json"),
    )

    assert prepare_registration(spec)[2] == fresh
    assert thread.read_text() == "fresh-thread\n"
    assert calls == ["thread/start", "thread/inject_items", "mcpServer/tool/call"]


def test_prepare_reuses_exact_persisted_registration(spec: Spec, monkeypatch) -> None:
    thread = spec.home / "wakeful-thread-exact"
    thread.write_text("thread-1\n")
    thread.chmod(0o600)
    binding = CodexBinding("thread-1", str(spec.start_record), spec.start_record.name)
    calls: list[str] = []

    class Client:
        def __init__(self, *_args):
            pass

        def call(self, method: str, _arguments: dict[str, object]):
            calls.append(method)
            raise AssertionError("exact registration must not be retried")

        def close(self) -> None:
            calls.append("closed")

    monkeypatch.setattr("switchstand.codex_registration.QueueClient", Client)
    monkeypatch.setattr("switchstand.codex_registration.bind", lambda *_args: binding)
    monkeypatch.setattr(
        "switchstand.codex_registration.existing_registration",
        lambda *_args: _async_value(("exact", spec.default_name)),
    )
    monkeypatch.setattr(
        "switchstand.codex_registration.freeze_config",
        lambda *_args: _async_value(spec.home / "runner.json"),
    )

    assert prepare_registration(spec) == (
        spec.home / "runner.json", "postgresql://exact", binding,
    )
    assert calls == ["closed"]


def test_prepare_recovers_registration_after_lost_response(spec: Spec, monkeypatch) -> None:
    calls: list[tuple[str, str]] = []
    frozen_threads: list[str] = []
    attempt = 0
    started = 0

    class Client:
        def __init__(self, *_args):
            pass

        def call(self, method: str, arguments: dict[str, object]):
            nonlocal started
            tool = str(arguments.get("tool", ""))
            calls.append((method, tool))
            if method == "thread/start":
                started += 1
                return {"thread": {"id": f"thread-{started}"}}
            if tool == "agent_register":
                raise OSError("response lost after durable registration")
            return {"structuredContent": {"status": "ok", "name": spec.default_name}}

        def close(self) -> None:
            pass

    async def existing(*_args):
        nonlocal attempt
        attempt += 1
        return (("missing", None) if attempt == 1 else
                ("takeover", spec.default_name))

    async def freeze(_spec, binding, *_args):
        frozen_threads.append(binding.thread_id)
        return spec.home / "runner.json"

    monkeypatch.setattr("switchstand.codex_registration.QueueClient", Client)
    monkeypatch.setattr(
        "switchstand.codex_registration.bind",
        lambda _client, _home, start, thread: CodexBinding(
            thread, str(start), start.name,
        ),
    )
    monkeypatch.setattr("switchstand.codex_registration.existing_registration", existing)
    monkeypatch.setattr("switchstand.codex_registration.freeze_config", freeze)

    with pytest.raises(OSError, match="response lost"):
        prepare_registration(spec)
    final_binding = prepare_registration(spec)[2]
    assert final_binding.thread_id == "thread-2"
    assert calls.count(("thread/start", "")) == 2
    assert calls.count(("mcpServer/tool/call", "agent_register")) == 1
    assert calls.count(("mcpServer/tool/call", "agent_takeover")) == 1
    assert frozen_threads == ["thread-2"]
    assert (spec.home / "wakeful-thread-exact").read_text() == "thread-2\n"


@pytest.mark.asyncio
async def test_missing_registration_does_not_require_local_principal(monkeypatch) -> None:
    class Mailboxes:
        def __init__(self, _engine):
            pass

        async def by_name(self, _name: str):
            return SimpleNamespace(
                status="denied", reason="mailbox_not_found", mailbox=None,
            )

    async def unexpected_principal():
        raise AssertionError("fresh registration must authenticate through MCP")

    service = SimpleNamespace(
        messages=SimpleNamespace(engine=object()),
        principal=unexpected_principal,
    )

    @asynccontextmanager
    async def resources():
        yield service, object()

    monkeypatch.setattr("switchstand.codex_registration.AgentMailboxState", Mailboxes)
    monkeypatch.setattr("switchstand.chatgpt_edge.resource_service", resources)
    binding = CodexBinding("thread-1", "/start", "generation")
    assert await existing_registration(
        binding, "codex-head-exact", "postgresql://exact",
    ) == ("missing", None)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "session_key,principal_key,expected",
    [
        (chat_session_key("codex:thread-1"), "principal-1", ("exact", "codex-head-exact")),
        (chat_session_key("codex:old-thread"), "principal-1", ("takeover", "codex-head-exact")),
    ],
)
async def test_existing_registration_classifies_exact_and_same_principal(
    monkeypatch, session_key: str, principal_key: str, expected,
) -> None:
    mailbox = SimpleNamespace(
        name="codex-head-exact",
        session_key=session_key,
        principal_key=principal_key,
    )

    class Mailboxes:
        def __init__(self, _engine):
            pass

        async def by_name(self, _name: str):
            return SimpleNamespace(status="ok", mailbox=mailbox)

    service = SimpleNamespace(
        messages=SimpleNamespace(engine=object()),
        principal=lambda: _async_value(SimpleNamespace(key="principal-1")),
    )

    @asynccontextmanager
    async def resources():
        yield service, object()

    monkeypatch.setattr("switchstand.codex_registration.AgentMailboxState", Mailboxes)
    monkeypatch.setattr("switchstand.chatgpt_edge.resource_service", resources)
    binding = CodexBinding("thread-1", "/start", "generation")
    assert await existing_registration(binding, "codex-head-exact", "postgresql://exact") == expected


@pytest.mark.asyncio
async def test_existing_registration_rejects_cross_principal(monkeypatch) -> None:
    mailbox = SimpleNamespace(
        name="codex-head-exact",
        session_key=chat_session_key("codex:old-thread"),
        principal_key="principal-2",
    )

    class Mailboxes:
        def __init__(self, _engine):
            pass

        async def by_name(self, _name: str):
            return SimpleNamespace(status="ok", mailbox=mailbox)

    service = SimpleNamespace(
        messages=SimpleNamespace(engine=object()),
        principal=lambda: _async_value(SimpleNamespace(key="principal-1")),
    )

    @asynccontextmanager
    async def resources():
        yield service, object()

    monkeypatch.setattr("switchstand.codex_registration.AgentMailboxState", Mailboxes)
    monkeypatch.setattr("switchstand.chatgpt_edge.resource_service", resources)
    binding = CodexBinding("thread-1", "/start", "generation")
    with pytest.raises(ValueError, match="another principal"):
        await existing_registration(binding, "codex-head-exact", "postgresql://exact")


@pytest.mark.asyncio
async def test_freeze_config_replaces_stale_config_after_exact_readback(
    spec: Spec, monkeypatch,
) -> None:
    binding = CodexBinding("thread-1", str(spec.start_record), spec.start_record.name)
    mailbox = SimpleNamespace(
        session_key=chat_session_key("codex:thread-1"),
        model_dump=lambda **_kwargs: {"name": spec.default_name},
    )

    class Mailboxes:
        def __init__(self, _engine):
            pass

        async def by_name(self, _name: str):
            return SimpleNamespace(status="ok", mailbox=mailbox)

    service = SimpleNamespace(messages=SimpleNamespace(engine=object()))

    @asynccontextmanager
    async def resources():
        yield service, object()

    monkeypatch.setattr("switchstand.codex_registration.AgentMailboxState", Mailboxes)
    monkeypatch.setattr("switchstand.chatgpt_edge.resource_service", resources)
    path = spec.home / "wakeful-exact.json"
    path.write_text("stale")
    path.chmod(0o600)

    assert await freeze_config(spec, binding, spec.default_name, "postgresql://exact") == path
    value = json.loads(path.read_text())
    assert value["binding"]["thread_id"] == "thread-1"
    assert value["app_server_socket"] == str(spec.socket_path)
