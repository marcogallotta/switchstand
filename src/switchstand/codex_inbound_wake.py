"""Default-off, same-daemon mailbox wake; no durable recovery or retry of effects."""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
from collections.abc import Callable
from contextlib import AsyncExitStack, suppress
from pathlib import Path
from typing import Any, cast
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from websockets.asyncio.client import unix_connect

from .agent_mailboxes import AgentMailbox, AgentMailboxState, chat_session_key
from .mailbox_wake import delivery_marker, delivery_notice, marker_receipts
from .messages import (
    REVIEW_ROUTE_REFS,
    MessageRoute,
    MessageState,
    MessageSubmitRequest,
    PendingMessage,
)

LOG = logging.getLogger(__name__)
SOURCES = ["cli", "vscode", "exec", "appServer", "subAgent", "subAgentReview",
           "subAgentCompact", "subAgentThreadSpawn", "subAgentOther"]


class DaemonRejected(RuntimeError):
    """The owning runtime definitively rejected this request; no effect was accepted."""


class ThreadNotFound(RuntimeError):
    """No recipient matches in this daemon; safe to inspect another daemon."""


class UncertainTurn(RuntimeError):
    """An effect may have been accepted; reconnecting must never redispatch it."""


class DaemonConnection:
    """Observe notifications before attaching. Requests are never automatically retried."""

    def __init__(self, socket: str, dirty: Callable[[], None]):
        self.socket, self.dirty = socket, dirty
        self.pending: dict[int, asyncio.Future[Any]] = {}
        self.sequence = 0
        self.thread_id: str | None = None
        self.ws: Any = None
        self.reader: asyncio.Task[None] | None = None

    async def __aenter__(self):
        self.ws = await unix_connect(self.socket, max_size=32 * 1024 * 1024)
        self.reader = asyncio.create_task(self._read())
        await self.call("initialize", {
            "clientInfo": {"name": "switchstand-inbound-wake", "version": "1"},
            "capabilities": {"experimentalApi": True},
        })
        await self.ws.send(json.dumps({"method": "initialized", "params": {}}))
        return self

    async def __aexit__(self, *_: object):
        if self.reader is not None:
            self.reader.cancel()
            with suppress(asyncio.CancelledError):
                await self.reader
        await self.ws.close()

    async def _read(self):
        try:
            async for raw in self.ws:
                packet = json.loads(raw)
                future = self.pending.get(packet.get("id"))
                if future is not None and not future.done():
                    if "error" in packet:
                        future.set_exception(DaemonRejected(str(packet["error"])))
                    else:
                        future.set_result(packet["result"])
                elif (packet.get("method") in {"turn/completed", "thread/status/changed"}
                      and packet.get("params", {}).get("threadId") == self.thread_id):
                    self.dirty()
        except Exception:
            LOG.exception("inbound wake observer disconnected socket=%s thread=%s phase=observe",
                          self.socket, self.thread_id)
        finally:
            for future in self.pending.values():
                if not future.done():
                    future.set_exception(ConnectionError("daemon observer disconnected"))
            self.dirty()

    async def call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if self.reader is not None and self.reader.done():
            raise ConnectionError("daemon observer is no longer running")
        self.sequence += 1
        identity = self.sequence
        future = asyncio.get_running_loop().create_future()
        self.pending[identity] = future
        try:
            await self.ws.send(json.dumps({"id": identity, "method": method, "params": params}))
            return await asyncio.wait_for(future, 30)
        finally:
            self.pending.pop(identity, None)


async def resolve_thread(rpc: DaemonConnection, binding: AgentMailbox) -> str:
    candidates: set[str] = set()
    for method, options in (
        ("thread/loaded/list", {}),
        ("thread/list", {"limit": 100, "useStateDbOnly": True, "sourceKinds": SOURCES}),
    ):
        cursor = None
        while True:
            page = await rpc.call(method, {**options, "cursor": cursor})
            candidates.update(item if isinstance(item, str) else item["id"]
                              for item in page["data"])
            cursor = page.get("nextCursor")
            if not cursor:
                break
    matches = [identity for identity in candidates
               if chat_session_key(f"codex:{identity}") == binding.session_key]
    if not matches:
        raise ThreadNotFound("recipient not in this daemon")
    if len(matches) != 1:
        raise RuntimeError("UNKNOWN: no unique recipient in configured owning daemon")
    return matches[0]


async def receipts(rpc: DaemonConnection, thread_id: str) -> set[str]:
    """Only exact matched-thread user input is a delivery receipt; scan complete pages."""
    found: set[str] = set()
    cursor = None
    while True:
        page = await rpc.call("thread/turns/list", {
            "threadId": thread_id, "cursor": cursor, "limit": 100, "itemsView": "full",
        })
        for turn in page["data"]:
            for item in turn["items"]:
                if item.get("type") == "userMessage":
                    for content in item.get("content", []):
                        text = content.get("text", "")
                        if content.get("type") == "text":
                            found.update(marker_receipts(text))
        cursor = page.get("nextCursor")
        if not cursor:
            return found


class InboundWake:
    """One in-process recipient owner, with event-driven dirty intent across busy turns."""

    def __init__(self, messages: MessageState, control: Path, admission: Path):
        self.messages, self.control, self.admission = messages, control, admission
        self.mailboxes = AgentMailboxState(messages.engine)
        self.sockets = self._sockets(json.loads(control.read_text()))
        self.tasks: dict[UUID, asyncio.Task[None]] = {}
        self.events: dict[UUID, asyncio.Event] = {}
        self.fresh: set[UUID] = set()
        self.fresh_messages: dict[UUID, PendingMessage] = {}
        self.outcomes: dict[UUID, str] = {}
        self.roots: dict[str, asyncio.Lock] = {}
        self.routes: dict[str, tuple[Any, ...]] = {}
        self.suspended: set[UUID] = set()
        self.observed: set[UUID] = set()
        self.watcher: asyncio.Task[None] | None = None

    async def start(self):
        # Baseline old mail before callbacks/observation: restart never authorizes replay.
        self.observed = {m.delivery_id for m in await self.messages.wake_pending()}
        self.messages.on_delivery = self.signal
        self.watcher = asyncio.create_task(self._watch())

    async def _watch(self):
        while True:
            await asyncio.sleep(1)
            try:
                for message in await self.messages.wake_pending():
                    if message.delivery_id not in self.observed:
                        self.signal(message, True)
            except Exception:
                LOG.exception("inbound wake committed-delivery discovery failed phase=watch")

    async def _alert(self, evidence: dict[str, Any], error: Exception):
        try:
            target = (await self.mailboxes.by_name("claude-coordinator")).mailbox
            if target is None:
                raise RuntimeError("alert recipient unavailable")
            result = await self.messages.submit_admitted(
                uuid5(NAMESPACE_URL, "switchstand:codex-inbound-wake"),
                MessageRoute(recipient_work_id=target.endpoint_id,
                             recipient_grant_version=target.generation),
                MessageSubmitRequest(api_version="1", message_id=uuid4(), grant_version=1,
                    route_ref="agent.claude-coordinator", kind="request",
                    payload={"wake_failure": evidence, "error": str(error)[:500]}),
            )
            if result.status != "ok":
                raise RuntimeError(f"alert commit {result.status}: {result.reason}")
        except Exception:
            LOG.exception("inbound wake alert unavailable evidence=%s", evidence)


    async def check_route(self, token: str, session_id: str) -> dict[str, str]:
        """Exact-route read-only bearer proof; GO is a snapshot, never an effect grant."""
        entry = self.routes.get(token)
        if entry is None or session_id != entry[4]:
            return {"status": "DENIED"}
        rpc, binding, identity, root_binding, root, path, delivery = entry
        async def current():
            thread = (await rpc.call("thread/read", {
                "threadId": identity, "includeTurns": False,
            }))["thread"]
            spawn = thread.get("source", {}).get("subAgent", {}).get("thread_spawn", {})
            return (thread.get("parentThreadId") == root and spawn.get("agent_path") == path
                    and (await self.mailboxes.by_endpoint_id(root_binding.endpoint_id)).mailbox == root_binding
                    and await self.messages.pending_delivery(binding, delivery))
        try:
            if not self.enabled():
                return {"status": "STOP"}
            if not await current():
                return {"status": "STALE"}
            admitted = await self._admit()
            valid = await current()
            status = "STOP" if not self.enabled() else "STALE" if not valid else "GO" if admitted else "WAIT"
            return {"status": status}
        except Exception:
            LOG.exception("native Head route validation unavailable")
            return {"status": "UNKNOWN"}

    async def _admit(self) -> bool:
        process = await asyncio.create_subprocess_exec(
            str(self.admission), "512", stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        output, _ = await process.communicate()
        LOG.info("wake admission: %s", output.decode().strip())
        return process.returncode == 0

    @staticmethod
    def _sockets(config: dict[str, Any]) -> tuple[str, ...]:
        sockets: Any = config.get("sockets", [config.get("socket")])
        if (not isinstance(sockets, list) or not sockets
                or any(not isinstance(socket, str) or not socket.startswith("/") for socket in cast(list[Any], sockets))
                or len(set(cast(list[Any], sockets))) != len(cast(list[Any], sockets))):
            raise ValueError("wake requires distinct absolute daemon socket paths")
        return tuple(cast(list[str], sockets))

    def enabled(self) -> bool:
        try:
            config = json.loads(self.control.read_text())
            return config.get("enabled") is True and self._sockets(config) == self.sockets
        except (OSError, ValueError):
            return False

    def signal(self, message: PendingMessage, fresh: bool):
        self.observed.add(message.delivery_id)
        if fresh:
            self.fresh.add(message.delivery_id)
            self.fresh_messages[message.delivery_id] = message
        recipient = message.recipient_work_id
        event = self.events.setdefault(recipient, asyncio.Event())
        event.set()
        if recipient not in self.tasks or (self.tasks[recipient].done()
                                            and recipient not in self.suspended):
            self.tasks[recipient] = asyncio.create_task(self._run(recipient, event))

    async def close(self):
        self.messages.on_delivery = None
        if self.watcher is not None:
            self.watcher.cancel()
            await asyncio.gather(self.watcher, return_exceptions=True)
        for task in self.tasks.values():
            task.cancel()
        await asyncio.gather(*self.tasks.values(), return_exceptions=True)

    async def _root(self, rpc: DaemonConnection, identity: str) -> dict[str, Any]:
        """Resume only stored, unsupported-capability-unknown Roots; then reread capability."""
        thread = (await rpc.call("thread/read", {
            "threadId": identity, "includeTurns": False,
        }))["thread"]
        if thread.get("parentThreadId"):
            raise RuntimeError("UNKNOWN: owning actor is not an externally wakeable Root")
        if (thread.get("status", {}).get("type") == "notLoaded"
                and thread.get("canAcceptDirectInput") is None):
            await rpc.call("thread/resume", {"threadId": identity, "excludeTurns": True})
            thread = (await rpc.call("thread/read", {
                "threadId": identity, "includeTurns": False,
            }))["thread"]
        if thread.get("parentThreadId") or thread.get("canAcceptDirectInput") is not True:
            raise RuntimeError("UNKNOWN: actor is not an externally wakeable Root")
        return thread

    async def _run(self, recipient: UUID, event: asyncio.Event):
        # Only uncertain turn acceptance is terminal. Definite pre-effect failures can
        # be retried by subsequent delivery signals without restarting the service.
        binding = None
        identity = ""
        rpc = None
        evidence: dict[str, Any] = {"recipient": str(recipient), "phase": "discovery",
                                    "generation": None, "thread": None, "socket": None,
                                    "discovery": {}, "actor_status": None}
        try:
            async with AsyncExitStack() as stack:
                connections: dict[str, DaemonConnection] = {}
                attached = ""
                while True:
                    await event.wait()
                    event.clear()
                    evidence["phase"] = "discovery"
                    result = await self.mailboxes.by_endpoint_id(recipient)
                    if result.mailbox is None:
                        raise RuntimeError("UNKNOWN: recipient binding unavailable")
                    evidence["generation"] = result.mailbox.generation
                    evidence["agent_type"] = result.mailbox.agent_type
                    if result.mailbox.agent_type != "codex":
                        self.outcomes[recipient] = ("UNKNOWN_AGENT_TYPE" if result.mailbox.agent_type is None
                                                    else "NON_CODEX")
                        LOG.info("inbound wake skipped evidence=%s", json.dumps(evidence))
                        return
                    reader = getattr(rpc, "reader", None)
                    dead = reader is not None and reader.done()
                    if result.mailbox != binding or dead:
                        matches: list[tuple[DaemonConnection, str]] = []
                        discovery: dict[str, Any] = {}
                        for socket in self.sockets:
                            try:
                                connection = connections.get(socket)
                                reader = getattr(connection, "reader", None)
                                if connection is None or (reader is not None and reader.done()):
                                    connection = await stack.enter_async_context(
                                        DaemonConnection(socket, event.set))
                                    connections[socket] = connection
                                thread_id = await resolve_thread(connection, result.mailbox)
                                matches.append((connection, thread_id))
                                discovery[socket] = {"matches": 1}
                            except ThreadNotFound:
                                discovery[socket] = {"matches": 0}
                            except (ConnectionError, OSError, TimeoutError, DaemonRejected) as error:
                                discovery[socket] = {"matches": None, "error": type(error).__name__}
                        evidence["discovery"] = discovery
                        if len(matches) == 0:
                            raise RuntimeError("UNKNOWN: missing recipient across configured daemons")
                        if len(matches) > 1:
                            raise RuntimeError("UNKNOWN: multiple recipient matches across configured daemons")
                        rpc, identity = matches[0]
                        attached = ""
                    binding = result.mailbox
                    assert rpc is not None
                    evidence.update(thread=identity, socket=getattr(rpc, "socket", None), phase="eligibility")
                    thread = (await rpc.call("thread/read", {
                        "threadId": identity, "includeTurns": False,
                    }))["thread"]
                    evidence["actor_status"] = thread.get("status")
                    evidence["direct_input"] = thread.get("canAcceptDirectInput")
                    root = thread.get("parentThreadId") or identity
                    root_binding = binding
                    path = None
                    if root != identity:
                        spawn = thread.get("source", {}).get("subAgent", {}).get("thread_spawn", {})
                        if spawn.get("parent_thread_id") != root or spawn.get("depth") != 1:
                            raise RuntimeError("UNKNOWN: only direct native Heads supported")
                        path = spawn["agent_path"]
                        root_binding = (await self.mailboxes.for_session(f"codex:{root}")).mailbox
                        if root_binding is None or root_binding.principal_key != binding.principal_key:
                            raise RuntimeError("UNKNOWN: owning Root binding unavailable")
                    rpc.thread_id = root
                    async with self.roots.setdefault(root, asyncio.Lock()):
                        evidence["phase"] = "attach"
                        await self._root(rpc, root)
                        if attached != root:
                            await rpc.call("thread/resume", {"threadId": root, "excludeTurns": True})
                            attached = root
                        evidence["phase"] = "pre-dispatch"
                        await self._reconcile(rpc, binding, identity, root, root_binding, path)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            uncertain = isinstance(error, UncertainTurn)
            if uncertain:
                self.suspended.add(recipient)
                evidence["phase"] = "turn/start-uncertain"
            self.outcomes[recipient] = f"{'DENIED' if isinstance(error, DaemonRejected) else 'UNKNOWN'}: {error}"
            LOG.exception("inbound wake %s evidence=%s", "suspended" if uncertain else "failed",
                          json.dumps(evidence, sort_keys=True))
            await self._alert(evidence, error)

    async def _reconcile(self, rpc: DaemonConnection, binding: AgentMailbox, identity: str,
                         root: str | None = None, root_binding: AgentMailbox | None = None,
                         path: str | None = None):
        root, root_binding = root or identity, root_binding or binding
        recipient = binding.endpoint_id
        pending = await self.messages.all_pending_delivery_ids(binding)
        if pending is None:
            self.outcomes[recipient] = "STALE"
            return
        seen = await receipts(rpc, root)
        remaining = [delivery for delivery in pending if str(delivery) not in seen]
        if not remaining:
            self.outcomes[recipient] = ("ROUTED_PENDING" if path else "RECEIPT") if pending else "EMPTY"
            return
        remaining = [delivery for delivery in remaining if delivery in self.fresh]
        if not remaining:
            self.outcomes[recipient] = "OLDER_UNCERTAIN"
            return
        status = (await rpc.call("thread/read", {
            "threadId": root, "includeTurns": False,
        }))["thread"]["status"]["type"]
        if status != "idle":
            self.outcomes[recipient] = f"BUSY:{status}"
            return
        if not self.enabled():
            self.outcomes[recipient] = "STOP"
            return
        for attempt in range(2):
            if await self._admit():
                break
            self.outcomes[recipient] = "WAIT"
            if attempt or not self.enabled():
                return
            await asyncio.sleep(5)
        # Fence takeover until start acceptance; host admission is outside the transaction.
        async with self.messages.pending_delivery_fence(binding, remaining[0]) as current:
            if not current or not self.enabled():
                self.outcomes[recipient] = "STALE_OR_STOP"
                return
            status = (await rpc.call("thread/read", {
                "threadId": root, "includeTurns": False,
            }))["thread"]["status"]["type"]
            if status != "idle":
                self.outcomes[recipient] = f"BUSY:{status}"
                return
            if not self.enabled():
                self.outcomes[recipient] = "STOP"
                return
            marker = delivery_marker(remaining)
            text = delivery_notice(((delivery, self.fresh_messages[delivery])
                                    for delivery in remaining), REVIEW_ROUTE_REFS)
            if path is not None:
                current = (await self.mailboxes.by_endpoint_id(root_binding.endpoint_id)).mailbox
                if current != root_binding:
                    self.outcomes[recipient] = "STALE_ROOT"
                    return
                token = secrets.token_urlsafe(24)
                self.routes[token] = (rpc, binding, identity, root_binding, root, path, remaining[0])
                gate_url = json.loads(self.control.read_text()).get(
                    "gate_url", "http://127.0.0.1:8790/internal/codex-stop-mailbox")
                gate = {"session_id": root, "wake_route_token": token}
                text = (marker + "\nSWITCHSTAND_HEAD_ROUTING=" + json.dumps({
                    "head_thread": identity, "head_path": path,
                    "head_generation": binding.generation, "root_generation": root_binding.generation,
                }) + "\nPreserve original objective, current authority and STOP. Native list_agents "
                    "must verify this SAME direct Head. Your LAST awaited evidence operation before "
                    f"followup_task must POST JSON {json.dumps(gate)} to {gate_url} "
                    "using local exec/urllib with proxies disabled. Only GO permits immediate native "
                    "followup to that same Head, without another awaited evidence tool. Gate includes "
                    "fresh admission/currentness/STOP; GO is a snapshot, not a reservation or authority. "
                    "Never create/replace/proxy a Head. Send it this notice then wait for its own "
                    "reply/disposition. Root routing acceptance is not Head completion.\n" + text)
            if not self.enabled():
                self.outcomes[recipient] = "STOP"
                return
            try:
                result = await rpc.call("turn/start", {
                    "threadId": root, "input": [{"type": "text", "text": text}],
                })
                turn_id = result["turn"]["id"]
            except DaemonRejected:
                raise
            except Exception as error:
                raise UncertainTurn("turn acceptance UNKNOWN; no replay") from error
            self.outcomes[recipient] = "ACCEPTED:" + turn_id
            self.fresh.difference_update(remaining)
            for delivery in remaining:
                self.fresh_messages.pop(delivery, None)
