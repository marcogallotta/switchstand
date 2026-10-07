from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path

import pytest

from switchstand.codex_registration import database_url, prepare_registration
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


def test_prepare_registers_exact_launch_owned_thread(spec: Spec, monkeypatch) -> None:
    calls: list[tuple[str, dict[str, object]]] = []
    binding = CodexBinding("thread-1", str(spec.start_record), spec.start_record.name)

    class Client:
        def __init__(self, codex: Path, home: Path, socket_path: Path):
            assert (codex, home, socket_path) == (spec.codex, spec.home, spec.socket_path)

        def call(self, method: str, arguments: dict[str, object]):
            calls.append((method, arguments))
            if method == "thread/start":
                return {"thread": {"id": "thread-1"}}
            return {"structuredContent": {"status": "ok", "name": spec.default_name}}

        def close(self) -> None:
            calls.append(("closed", {}))

    async def existing(*_args):
        return "missing", None

    async def freeze(*_args):
        return spec.home / "runner.json"

    monkeypatch.setattr("switchstand.codex_registration.QueueClient", Client)
    monkeypatch.setattr("switchstand.codex_registration.bind", lambda *_args: binding)
    monkeypatch.setattr("switchstand.codex_registration.existing_registration", existing)
    monkeypatch.setattr("switchstand.codex_registration.freeze_config", freeze)

    assert prepare_registration(spec) == (
        spec.home / "runner.json", "postgresql://exact", binding,
    )
    assert calls[0][0] == "thread/start"
    assert calls[1] == ("mcpServer/tool/call", {
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
    assert calls == ["thread/start", "mcpServer/tool/call"]
