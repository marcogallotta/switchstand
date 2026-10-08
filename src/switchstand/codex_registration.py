"""Registration of the exact launch-owned Codex thread."""
from __future__ import annotations

import asyncio
import json
import os
import stat
import tomllib
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path
from typing import Any, Literal, Protocol, cast

from .agent_mailboxes import AgentMailboxState, agent_name_key, chat_session_key
from .codex_wakeful import CodexBinding, QueueClient, bind
from .secure_file import atomic_replace_bytes, create_new_private_bytes, read_private_bytes


class RegistrationSpec(Protocol):
    @property
    def home(self) -> Path: ...

    @property
    def codex(self) -> Path: ...

    @property
    def start_record(self) -> Path: ...

    @property
    def default_name(self) -> str: ...

    @property
    def environment_file(self) -> Path: ...

    @property
    def profile_path(self) -> Path: ...

    @property
    def mcp_server(self) -> str: ...

    @property
    def socket_path(self) -> Path: ...


def database_url(path: Path) -> str:
    metadata = path.lstat()
    if (
        path.is_symlink()
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or stat.S_IMODE(metadata.st_mode) != 0o600
    ):
        raise ValueError("Wakeful environment must be an owned mode-0600 regular file")
    for line in path.read_text().splitlines():
        if not line.strip() or line.lstrip().startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key.strip() == "DATABASE_URL":
            result = value.strip().strip("'\"")
            if result:
                return result
    raise ValueError("Wakeful environment does not contain DATABASE_URL")


@contextmanager
def configured_database(url: str):
    previous = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = url
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = previous


async def freeze_config(
    spec: RegistrationSpec, binding: CodexBinding, name: str, url: str,
) -> Path:
    from .chatgpt_edge import resource_service

    with configured_database(url):
        async with resource_service() as (service, _runtime):
            if service.messages is None:
                raise ValueError("message state is unavailable")
            result = await AgentMailboxState(service.messages.engine).by_name(name)
    mailbox = result.mailbox
    if (
        result.status != "ok"
        or mailbox is None
        or mailbox.session_key != chat_session_key(f"codex:{binding.thread_id}")
    ):
        raise ValueError("registered mailbox does not match the exact Codex thread")
    body = json.dumps({
        "mailbox": mailbox.model_dump(mode="json"),
        "binding": asdict(binding),
        "codex_home": str(spec.home),
        "codex": str(spec.codex),
        "app_server_socket": str(spec.socket_path),
    }, sort_keys=True, separators=(",", ":")).encode()
    path = spec.home / f"wakeful-{spec.start_record.name.removeprefix('start-commit.')}.json"
    try:
        create_new_private_bytes(path, body)
    except FileExistsError:
        if read_private_bytes(path) != body:
            atomic_replace_bytes(path, body)
    return path


async def existing_registration(
    binding: CodexBinding, name: str, url: str,
) -> tuple[Literal["missing", "exact", "takeover"], str | None]:
    from .chatgpt_edge import resource_service

    with configured_database(url):
        async with resource_service() as (service, _runtime):
            if service.messages is None:
                raise ValueError("message state is unavailable")
            result = await AgentMailboxState(service.messages.engine).by_name(name)
            if result.status == "denied" and result.reason == "mailbox_not_found":
                return "missing", None
            mailbox = result.mailbox
            if result.status != "ok" or mailbox is None:
                raise ValueError("mailbox registration state is unavailable")
            if mailbox.session_key == chat_session_key(f"codex:{binding.thread_id}"):
                return "exact", mailbox.name
            principal = await service.principal()
            if principal is None:
                raise ValueError("authenticated principal is unavailable")
            if mailbox.principal_key == principal.key:
                return "takeover", mailbox.name
            raise ValueError("mailbox name is owned by another principal")


def thread_path(spec: RegistrationSpec) -> Path:
    suffix = spec.start_record.name.removeprefix("start-commit.")
    return spec.home / f"wakeful-thread-{suffix}"


def pending_thread(spec: RegistrationSpec) -> str | None:
    path = thread_path(spec)
    if not path.exists():
        return None
    value = read_private_bytes(path).decode().strip()
    if not value:
        raise ValueError("persisted Codex thread is invalid")
    return value


def start_thread(client: QueueClient, profile: dict[str, Any], developer: str) -> str:
    started = client.call("thread/start", {
        "cwd": str(Path.cwd()), "ephemeral": False,
        "developerInstructions": developer, "config": profile,
        "approvalPolicy": "never", "sandbox": "danger-full-access",
    })
    thread_id = cast(dict[str, Any], started["thread"])["id"]
    if not isinstance(thread_id, str) or not thread_id:
        raise ValueError("launch-owned Codex thread is unavailable")
    return thread_id


def materialize_thread(client: QueueClient, thread_id: str) -> None:
    """Persist a zero-turn thread without inventing a user assignment."""
    client.call("thread/inject_items", {
        "threadId": thread_id,
        "items": [{
            "type": "message",
            "role": "developer",
            "content": [{
                "type": "input_text",
                "text": (
                    "Switchstand Wakeful bootstrap only: this message materializes the "
                    "durable thread, is not a user assignment, and grants no authority."
                ),
            }],
        }],
    })


def prepare_registration(spec: RegistrationSpec) -> tuple[Path, str, CodexBinding]:
    """Create, register, and freeze the exact thread before the TUI owns it."""
    url = database_url(spec.environment_file)
    profile = tomllib.loads(spec.profile_path.read_text())
    developer = profile.get("developer_instructions")
    if not isinstance(developer, str) or str(spec.start_record) not in developer:
        raise ValueError("Codex profile does not bind the exact start record")
    client = QueueClient(spec.codex, spec.home, spec.socket_path)
    try:
        persisted = pending_thread(spec)
        if persisted is None:
            thread_id = start_thread(client, profile, developer)
            materialize_thread(client, thread_id)
            binding: CodexBinding | str = bind(
                client, spec.home, spec.start_record, thread_id,
            )
        else:
            thread_id = persisted
            binding = bind(client, spec.home, spec.start_record, thread_id)
        if not isinstance(binding, CodexBinding) and persisted is not None:
            thread_path(spec).unlink()
            thread_id = start_thread(client, profile, developer)
            materialize_thread(client, thread_id)
            binding = bind(client, spec.home, spec.start_record, thread_id)
        if not isinstance(binding, CodexBinding):
            raise TypeError(f"Codex thread binding is {binding}")
        state, existing_name = asyncio.run(existing_registration(
            binding, spec.default_name, url,
        ))
        is_root = agent_name_key(spec.default_name) == "root"
        if state == "missing" and is_root:
            raise ValueError("reserved /root mailbox does not exist")
        if state == "takeover":
            response = client.call("mcpServer/tool/call", {
                "threadId": binding.thread_id,
                "server": spec.mcp_server,
                "tool": "agent_takeover",
                "arguments": {"api_version": "1", "name": spec.default_name},
            })
            recovered = cast(dict[str, Any], response["structuredContent"])
            if recovered.get("status") != "ok" or not isinstance(recovered.get("name"), str):
                raise RuntimeError("authenticated mailbox recovery did not converge")
            existing_name = cast(str, recovered["name"])
        if state in {"exact", "takeover"}:
            assert existing_name is not None
            name = existing_name
        else:
            response = client.call("mcpServer/tool/call", {
                "threadId": binding.thread_id,
                "server": spec.mcp_server,
                "tool": "agent_register",
                "arguments": {"api_version": "1", "name": spec.default_name},
            })
            registered = cast(dict[str, Any], response["structuredContent"])
            if registered.get("status") != "ok" or not isinstance(registered.get("name"), str):
                raise RuntimeError("authenticated mailbox registration did not converge")
            name = cast(str, registered["name"])
        config = asyncio.run(freeze_config(spec, binding, name, url))
        atomic_replace_bytes(thread_path(spec), (thread_id + "\n").encode())
    finally:
        client.close()
    return config, url, binding
