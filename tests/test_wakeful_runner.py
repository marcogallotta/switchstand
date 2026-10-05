import asyncio
import json
import signal
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError

from switchstand.agent_mailboxes import AgentMailboxState, chat_session_key
from switchstand.wakeful_runner import (
    RunnerConfig,
    install_stop_handlers,
    load_config,
    run,
    serve,
    systemd_user_unit,
)


def config(home: Path) -> dict[str, object]:
    return {
        "mailbox": {
            "name": "Root",
            "name_key": "root",
            "endpoint_id": str(uuid4()),
            "principal_key": "principal",
            "session_key": chat_session_key("codex:thread-1"),
            "generation": 3,
        },
        "binding": {
            "thread_id": "thread-1",
            "start_record": str(home / "start-commit.exact"),
            "generation": "start-commit.exact",
        },
        "codex_home": str(home),
        "codex": "/opt/codex/bin/codex",
    }


def private_config(tmp_path: Path, body: dict[str, object]) -> Path:
    path = tmp_path / "runner.json"
    path.write_text(json.dumps(body))
    path.chmod(0o600)
    return path


def test_config_is_complete_exact_private_and_frozen(tmp_path):
    body = config(tmp_path)
    loaded = load_config(private_config(tmp_path, body))
    assert loaded.mailbox.name_key == "root"
    assert loaded.codex_binding().start_record == str(tmp_path / "start-commit.exact")
    with pytest.raises(ValidationError):
        loaded.codex = Path("/replacement")

    for mutation in (
        lambda value: value.pop("mailbox"),
        lambda value: value.update(extra="rejected"),
        lambda value: value["binding"].update(generation="wrong"),
    ):
        changed = json.loads(json.dumps(body))
        mutation(changed)
        with pytest.raises(ValidationError):
            load_config(private_config(tmp_path, changed))

    public = private_config(tmp_path, body)
    public.chmod(0o644)
    with pytest.raises(ValueError, match="mode-0600"):
        load_config(public)


async def test_startup_composes_existing_source_and_exact_binding(tmp_path, monkeypatch):
    loaded = RunnerConfig.model_validate(config(tmp_path))
    messages = type("Messages", (), {"engine": object()})()
    service = type("Service", (), {"messages": messages})()
    observed = []

    @asynccontextmanager
    async def resources():
        observed.append("opened")
        yield service, None
        observed.append("closed")

    async def inbound(*args, **kwargs):
        observed.append((args, kwargs))

    monkeypatch.setattr("switchstand.wakeful_runner.run_inbound", inbound)
    stop = asyncio.Event()
    await run(loaded, stop, resources)
    args, kwargs = observed[1]
    assert observed[0] == "opened" and observed[2] == "closed"
    assert args[0] is messages
    assert isinstance(args[1], AgentMailboxState) and args[1].engine is messages.engine
    assert args[2] == loaded.mailbox and args[3] == loaded.codex_binding()
    assert args[4] == loaded.codex_home and args[5] == loaded.codex
    assert args[6] is stop
    assert kwargs == {"opt_in": True}


async def test_stop_before_start_avoids_source_access(tmp_path):
    stop = asyncio.Event()
    stop.set()

    def forbidden():
        raise AssertionError("source accessed")

    await run(RunnerConfig.model_validate(config(tmp_path)), stop, forbidden)


def test_signal_stop_and_supervisor_restart_posture(tmp_path):
    callbacks = {}

    class Loop:
        def add_signal_handler(self, signum, callback):
            callbacks[signum] = callback

    stop = asyncio.Event()
    assert install_stop_handlers(Loop(), stop) == (signal.SIGINT, signal.SIGTERM)
    callbacks[signal.SIGTERM]()
    assert stop.is_set()

    unit = systemd_user_unit(
        Path("/opt/switchstand/bin/python"),
        Path("/etc/switchstand/wakeful.env"),
        tmp_path / "runner.json",
    )
    assert "--config=" in unit and "--enable" not in unit
    assert "EnvironmentFile=/etc/switchstand/wakeful.env" in unit
    assert "Restart=on-failure" in unit and "TimeoutStopSec=45" in unit
    assert "WantedBy=default.target" in unit
    assert "systemctl" not in unit


async def test_serve_removes_signal_handlers_after_graceful_stop(tmp_path, monkeypatch):
    callbacks, removed = {}, []

    class Loop:
        def add_signal_handler(self, signum, callback):
            callbacks[signum] = callback

        def remove_signal_handler(self, signum):
            removed.append(signum)
            return True

    async def stop_from_signal(_config, stop):
        callbacks[signal.SIGINT]()
        await stop.wait()

    monkeypatch.setattr("switchstand.wakeful_runner.asyncio.get_running_loop", lambda: Loop())
    monkeypatch.setattr("switchstand.wakeful_runner.run", stop_from_signal)
    await serve(RunnerConfig.model_validate(config(tmp_path)))
    assert removed == [signal.SIGINT, signal.SIGTERM]
