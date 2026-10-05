import json
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import uuid4

import pytest

from switchstand.agent_mailboxes import AgentMailboxState, chat_session_key
from switchstand.codex_wakeful import run_inbound_service


def config(home: Path) -> dict[str, object]:
    return {
        "mailbox": {"name": "Root", "name_key": "root", "endpoint_id": str(uuid4()),
                    "principal_key": "principal",
                    "session_key": chat_session_key("codex:thread-1"), "generation": 3},
        "binding": {"thread_id": "thread-1", "start_record": str(home / "start-commit.exact"),
                    "generation": "start-commit.exact"},
        "codex_home": str(home), "codex": "/opt/codex/bin/codex",
    }


def private_config(tmp_path: Path, body: dict[str, object]) -> Path:
    path = tmp_path / "runner.json"
    path.write_text(json.dumps(body))
    path.chmod(0o600)
    return path


async def test_private_config_composes_existing_inbound_loop(tmp_path, monkeypatch):
    messages = type("Messages", (), {"engine": object()})()
    service = type("Service", (), {"messages": messages})()
    observed = []

    @asynccontextmanager
    async def resources():
        yield service, None

    async def inbound(*args, **kwargs):
        observed.append((args, kwargs))

    monkeypatch.setattr("switchstand.chatgpt_edge.resource_service", resources)
    monkeypatch.setattr("switchstand.codex_wakeful.run_inbound", inbound)
    await run_inbound_service(private_config(tmp_path, config(tmp_path)))
    args, kwargs = observed[0]
    assert args[0] is messages
    assert isinstance(args[1], AgentMailboxState) and args[1].engine is messages.engine
    assert args[2].name_key == "root" and args[3].thread_id == "thread-1"
    assert args[4] == tmp_path and args[5] == Path("/opt/codex/bin/codex")
    assert kwargs == {"opt_in": True}


async def test_config_fails_closed_before_source_access(tmp_path, monkeypatch):
    monkeypatch.setattr("switchstand.chatgpt_edge.resource_service",
                        lambda: pytest.fail("source accessed"))
    with pytest.raises(ValueError, match="invalid inbound configuration"):
        await run_inbound_service(private_config(tmp_path, config(tmp_path) | {"extra": True}))

    public = private_config(tmp_path, config(tmp_path))
    public.chmod(0o644)
    with pytest.raises(ValueError, match="mode-0600"):
        await run_inbound_service(public)
