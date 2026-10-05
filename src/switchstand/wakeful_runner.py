"""Explicit supervised runner for the Wakeful inbound loop."""

from __future__ import annotations

import argparse
import asyncio
import signal
from collections.abc import Callable, Sequence
from contextlib import AbstractAsyncContextManager
from pathlib import Path
from typing import Self

from pydantic import ConfigDict, Field, model_validator

from .agent_mailboxes import AgentMailbox, AgentMailboxState
from .chatgpt_edge import ChatGPTService, resource_service
from .codex_wakeful import CodexBinding, run_inbound
from .contracts import ClosedModel
from .secure_file import read_private_bytes


class RunnerBinding(ClosedModel):
    thread_id: str = Field(min_length=1)
    start_record: Path
    generation: str = Field(min_length=1)


class RunnerConfig(ClosedModel):
    """One immutable mailbox-to-Codex binding, loaded from a private file."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    mailbox: AgentMailbox
    binding: RunnerBinding
    codex_home: Path
    codex: Path

    @model_validator(mode="after")
    def exact_paths(self) -> Self:
        paths = (self.codex_home, self.codex, self.binding.start_record)
        if any(not path.is_absolute() for path in paths):
            raise ValueError("runner paths must be absolute")
        if self.binding.start_record.parent != self.codex_home:
            raise ValueError("start record must belong to the exact Codex home")
        if self.binding.start_record.name != self.binding.generation:
            raise ValueError("generation must be the exact start-record name")
        return self

    def codex_binding(self) -> CodexBinding:
        return CodexBinding(
            thread_id=self.binding.thread_id,
            start_record=str(self.binding.start_record),
            generation=self.binding.generation,
        )


def load_config(path: Path) -> RunnerConfig:
    """Load complete, exact configuration without following a final symlink."""
    return RunnerConfig.model_validate_json(read_private_bytes(path))


ResourceFactory = Callable[
    [], AbstractAsyncContextManager[tuple[ChatGPTService, tuple[str, str] | None]]
]


async def run(config: RunnerConfig, stop: asyncio.Event,
              resources: ResourceFactory = resource_service) -> None:
    """Compose existing source and admission owners for one supervised lifetime."""
    if stop.is_set():
        return
    async with resources() as (service, _runtime):
        if service.messages is None:
            raise RuntimeError("message source is unavailable")
        await run_inbound(
            service.messages,
            AgentMailboxState(service.messages.engine),
            config.mailbox,
            config.codex_binding(),
            config.codex_home,
            config.codex,
            stop,
            opt_in=True,
        )


def install_stop_handlers(loop: asyncio.AbstractEventLoop, stop: asyncio.Event) -> tuple[int, ...]:
    installed: list[int] = []
    for signum in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(signum, stop.set)
        installed.append(signum)
    return tuple(installed)


async def serve(config: RunnerConfig) -> None:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    installed = install_stop_handlers(loop, stop)
    try:
        await run(config, stop)
    finally:
        for signum in installed:
            loop.remove_signal_handler(signum)


def _systemd_path(path: Path) -> str:
    rendered = str(path)
    if not path.is_absolute() or any(character.isspace() for character in rendered):
        raise ValueError("systemd paths must be absolute and contain no whitespace")
    return rendered


def systemd_user_unit(runtime_python: Path, environment_file: Path, config: Path) -> str:
    """Render an inert unit; callers must separately install and enable it."""
    python = _systemd_path(runtime_python)
    environment = _systemd_path(environment_file)
    frozen_config = _systemd_path(config)
    return (
        "[Unit]\nAfter=network-online.target\nWants=network-online.target\n\n"
        "[Service]\nType=simple\n"
        f"EnvironmentFile={environment}\n"
        f"ExecStart={python} -m switchstand.wakeful_runner --config={frozen_config}\n"
        "Restart=on-failure\nRestartSec=2\nTimeoutStopSec=45\n\n"
        "[Install]\nWantedBy=default.target\n"
    )


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args(argv)
    asyncio.run(serve(load_config(args.config)))


if __name__ == "__main__":
    main()
