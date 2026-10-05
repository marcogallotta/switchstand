"""Default-off Codex admission probe; no daemon or neutral outbox ownership."""
from __future__ import annotations

import argparse
import asyncio
import fcntl
import hashlib
import json
import os
import re
import selectors
import stat
import subprocess
import time
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, BinaryIO, cast
from uuid import UUID

from sqlalchemy.exc import SQLAlchemyError

from switchstand.agent_mailboxes import AgentMailbox, AgentMailboxState, chat_session_key
from switchstand.messages import MessageState, PendingMessage
from switchstand.secure_file import (
    atomic_replace_bytes,
    create_new_private_bytes,
    read_private_bytes,
)


@dataclass(frozen=True)
class WakeSourceRef:
    source_kind: str
    source_id: str

    @property
    def reread_instruction(self) -> str:
        if self.source_kind == "switchstand_inbound":
            return "Reread this exact delivery_id through Switchstand MCP before acting."
        if self.source_kind == "child_completion":
            return "Reread the exact parent call and latest persisted child state before acting."
        raise ValueError("unsupported source")


def inbound(message: PendingMessage) -> WakeSourceRef:
    """Only pass a committed MessageState delivery readback, never a sender message id."""
    return WakeSourceRef("switchstand_inbound", str(message.delivery_id))


@dataclass(frozen=True)
class CodexBinding:
    thread_id: str
    start_record: str
    generation: str


def wake_id(binding: CodexBinding, source: WakeSourceRef) -> str:
    return hashlib.sha256(json.dumps([
        "codex", binding.thread_id, binding.generation, source.source_kind, source.source_id
    ], separators=(",", ":")).encode()).hexdigest()


class QueueClient:
    """Lazy bounded client for Codex's durable same-home user-message queue."""

    def __init__(self, codex: Path, home: Path):
        try:
            self.codex = codex.resolve(strict=True)
            self.home = home.resolve(strict=True)
        except OSError as exc:
            raise OSError("UNAVAILABLE: unproved Codex runtime") from exc
        if (not self.codex.is_file() or not os.access(self.codex, os.X_OK)
                or not self.home.is_dir()):
            raise OSError("UNAVAILABLE: unproved Codex runtime")
        self.process: subprocess.Popen[bytes] | None = None
        self.stdin: BinaryIO | None = None
        self.stdout: BinaryIO | None = None
        self.selector: selectors.BaseSelector | None = None
        self.read_buffer = bytearray()
        self.sequence = 0

    def _start(self) -> None:
        if self.process is not None:
            return
        environment = dict(os.environ)
        environment["CODEX_HOME"] = str(self.home)
        try:
            process = subprocess.Popen(
                [str(self.codex), "app-server", "--listen", "stdio://"],
                cwd=self.home,
                env=environment,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                bufsize=0,
            )
            self.process = process
            if process.stdin is None or process.stdout is None:
                raise OSError("Codex app-server pipes unavailable")
            self.stdin = cast(BinaryIO, process.stdin)
            self.stdout = cast(BinaryIO, process.stdout)
            self.selector = selectors.DefaultSelector()
            self.selector.register(process.stdout, selectors.EVENT_READ)
            self.call("initialize", {"clientInfo": {"name": "switchstand-wakeful-probe",
                      "version": "1"}, "capabilities": {"experimentalApi": True}})
            self.write({"method": "initialized", "params": {}})
        except Exception as exc:
            self.close()
            raise OSError("UNAVAILABLE: Codex queue client initialization failed") from exc

    def write(self, message: dict[str, Any]) -> None:
        self._start()
        assert self.stdin is not None
        try:
            self.stdin.write((json.dumps(message, separators=(",", ":")) + "\n").encode())
            self.stdin.flush()
        except (OSError, BrokenPipeError) as exc:
            raise OSError("UNKNOWN: request send failed") from exc

    def call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        self._start()
        assert self.stdout is not None and self.selector is not None
        self.sequence += 1
        deadline = time.monotonic() + 10
        try:
            self.write({"jsonrpc": "2.0", "id": self.sequence,
                        "method": method, "params": params})
            while time.monotonic() < deadline:
                line = self.read_line(deadline)
                if not line:
                    break
                response = json.loads(line)
                if isinstance(response.get("method"), str) and "id" in response:
                    self.write({"jsonrpc": "2.0", "id": response["id"], "error": {
                        "code": -32601, "message": "Wakeful does not handle server requests"}})
                    continue
                if response.get("id") == self.sequence:
                    if "error" in response:
                        raise OSError("UNKNOWN: RPC rejected")
                    return cast(dict[str, Any], response["result"])
        except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
            raise OSError("UNKNOWN: response lost") from exc
        raise OSError("UNKNOWN: response lost")

    def read_line(self, deadline: float) -> bytes | None:
        assert self.stdout is not None and self.selector is not None
        while True:
            newline = self.read_buffer.find(b"\n")
            if newline >= 0:
                line = bytes(self.read_buffer[:newline])
                del self.read_buffer[:newline + 1]
                return line
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not self.selector.select(remaining):
                return None
            chunk = os.read(self.stdout.fileno(), 65536)
            if not chunk:
                return None
            self.read_buffer.extend(chunk)

    def close(self) -> None:
        selector, process = self.selector, self.process
        self.selector = None
        self.process = None
        self.stdin = None
        self.stdout = None
        self.read_buffer.clear()
        if selector is not None:
            selector.close()
        if process is None or process.poll() is not None:
            return
        try:
            process.terminate()
        except ProcessLookupError:
            return
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


def bind(client: QueueClient, home: Path, token: Path) -> CodexBinding | str:
    if token.parent != home or not token.name.startswith("start-commit."):
        return "NOT_BOUND"
    try:
        read_private_bytes(token)
        matches: list[str] = []
        cursor = None
        while True:
            page = client.call("thread/list", {"cursor": cursor, "limit": 100})
            for thread in page["data"]:
                path = Path(thread["path"])
                if not path.resolve().is_relative_to(home.resolve()):
                    return "UNAVAILABLE"
                matched = False
                for line in path.read_text().split("\n"):
                    if not line:
                        continue
                    record = json.loads(line)
                    payload = record.get("payload", {})
                    if (record["type"] == "response_item" and payload.get("type") == "message"
                            and payload.get("role") == "developer"):
                        matched |= any(re.search(r"(?<!\S)" + re.escape(str(token))
                            + r"(?=$|\s|[.,;:!?](?=\s|$))", part.get("text", "")) is not None
                            for part in payload.get("content", [])
                            if part.get("type") == "input_text")
                if matched:
                    matches.append(thread["id"])
            cursor = page.get("nextCursor")
            if not cursor:
                break
        if len(matches) != 1:
            return "CONFLICT" if matches else "NOT_BOUND"
        return CodexBinding(matches[0], str(token), token.name)
    except (OSError, ValueError, KeyError, TypeError):
        return "UNAVAILABLE"


def children(thread: dict[str, Any]) -> list[WakeSourceRef]:
    calls = [item for turn in thread["turns"] for item in turn["items"]
             if item["type"] == "collabAgentToolCall"]
    legacy_children = ({child for call in calls for child in call["agentsStates"]}
                       | {child for call in calls for child in call.get("receiverThreadIds", [])})
    latest = {child: state["status"] for call in calls
              for child, state in call["agentsStates"].items()}
    candidates: dict[str, set[str]] = {}
    for call in calls:
        if call["tool"] != "spawnAgent":
            continue
        for child in call["receiverThreadIds"]:
            if latest.get(child) in {"completed", "errored", "shutdown"}:
                candidates.setdefault(child, set()).add(json.dumps(
                    [call["id"], child, latest[child]], separators=(",", ":")))

    activities = [item for turn in thread["turns"] for item in turn["items"]
                  if item["type"] == "subAgentActivity"]
    activity_children = {activity["agentThreadId"] for activity in activities}
    starts: dict[str, set[str]] = {}
    activity_latest: dict[str, str] = {}
    for activity in activities:
        child = activity["agentThreadId"]
        activity_latest[child] = activity["kind"]
        if activity["kind"] == "started":
            starts.setdefault(child, set()).add(activity["id"])
    for child, start_ids in starts.items():
        state = activity_latest.get(child)
        if len(start_ids) == 1 and state in {"completed", "errored", "shutdown"}:
            candidates.setdefault(child, set()).add(json.dumps(
                [next(iter(start_ids)), child, state], separators=(",", ":")))

    mixed_children = legacy_children & activity_children
    return [WakeSourceRef("child_completion", next(iter(identities)))
            for child, identities in candidates.items()
            if child not in mixed_children and len(identities) == 1]


class Projection:
    def __init__(self, home: Path, binding: CodexBinding):
        self.path = home / "codex-wakeful.json"
        self.binding = binding
        try:
            self.records: dict[str, dict[str, Any]] = json.loads(read_private_bytes(self.path))
        except OSError:
            if self.path.exists() or self.path.is_symlink():
                raise
            self.records = {}

    def save(self) -> None:
        atomic_replace_bytes(self.path, json.dumps(self.records, sort_keys=True).encode())

    def hydrate_thread(self, client: QueueClient, thread: dict[str, Any]) -> dict[str, Any] | None:
        if thread.get("historyMode") == "legacy":
            return thread
        if thread.get("historyMode") != "paginated":
            return None
        turns: list[dict[str, Any]] = []
        cursor = None
        seen_cursors: set[str] = set()
        while True:
            page = client.call("thread/turns/list", {
                "threadId": self.binding.thread_id, "cursor": cursor, "limit": 100,
                "sortDirection": "asc", "itemsView": "full"})
            raw_turns = page["data"]
            if not isinstance(raw_turns, list):
                return None
            current: list[dict[str, Any]] = []
            for raw_turn in cast(list[Any], raw_turns):
                if not isinstance(raw_turn, dict):
                    return None
                turn = cast(dict[str, Any], raw_turn)
                if (turn.get("itemsView") != "full"
                        or not isinstance(turn.get("items"), list)):
                    return None
                current.append(turn)
            turns.extend(current)
            cursor = page.get("nextCursor")
            if cursor is None:
                return {**thread, "turns": turns}
            if not isinstance(cursor, str) or not cursor or cursor in seen_cursors:
                return None
            seen_cursors.add(cursor)

    def reconcile_thread(self, thread: dict[str, Any], identity: str) -> str:
        record = self.records[identity]
        if thread["id"] != self.binding.thread_id:
            return "STALE"
        for turn in thread["turns"]:
            for item in turn["items"]:
                if item["type"] == "userMessage" and item.get("clientId") == identity:
                    record.update(state="CONSUMED", evidence=f'{turn["id"]}/{item["id"]}',
                                  timestamp=time.time())
                    self.save()
                    return "ADMITTED"
        return "PROVEN_ABSENT"

    def reconcile_queue(self, client: QueueClient, identity: str) -> str:
        cursor = None
        matches: list[dict[str, Any]] = []
        while True:
            page = client.call("thread/queue/list", {
                "threadId": self.binding.thread_id, "cursor": cursor, "limit": 100})
            matches.extend(item for item in page["data"]
                           if item.get("clientUserMessageId") == identity)
            cursor = page.get("nextCursor")
            if not cursor:
                break
        if len(matches) > 1:
            return "UNKNOWN"
        if len(matches) == 1:
            self.records[identity].update(
                state="QUEUED", evidence=matches[0]["id"], timestamp=time.time())
            self.save()
            return "PENDING"
        return "PROVEN_ABSENT"

    def admit(self, client: QueueClient, source: WakeSourceRef) -> str:
        identity = wake_id(self.binding, source)
        record = self.records.get(identity)
        if record is None:
            record = {"binding": asdict(self.binding), **asdict(source), "wake_id": identity,
                      "state": "PENDING", "attempted": False, "timestamp": time.time()}
            self.records[identity] = record
            self.save()
        if record["binding"] != asdict(self.binding):
            return "STALE"
        if record["state"] == "CONSUMED":
            return "ADMITTED"
        try:
            rebound = bind(client, self.path.parent, Path(self.binding.start_record))
            if rebound != self.binding:
                return "STALE" if isinstance(rebound, CodexBinding) else "UNKNOWN"
            thread = client.call("thread/read", {"threadId": self.binding.thread_id,
                                                "includeTurns": True})["thread"]
            if thread["id"] != self.binding.thread_id:
                return "STALE"
            thread = self.hydrate_thread(client, thread)
            if thread is None:
                return "UNKNOWN"
            result = self.reconcile_thread(thread, identity)
            if result != "PROVEN_ABSENT":
                return result
            result = self.reconcile_queue(client, identity)
            if result != "PROVEN_ABSENT":
                return result
            # Queue/history absence cannot fence an attempted request whose response was lost.
            if record["attempted"]:
                return "UNKNOWN"
            if source.source_kind == "child_completion" and source not in children(thread):
                return "STALE"
            record.update(attempted=True, timestamp=time.time())
            self.save()
            queued = client.call("thread/queue/add", {"threadId": self.binding.thread_id,
                "clientUserMessageId": identity, "input": [{"type": "text",
                "text": f'{source.source_kind} '
                        + ('delivery_id=' if source.source_kind == 'switchstand_inbound'
                           else 'parent_call_child_terminal=') + source.source_id + '. '
                        + source.reread_instruction}]})["queuedSubmission"]
            if queued.get("clientUserMessageId") != identity:
                return "UNKNOWN"
            record.update(state="QUEUED", evidence=queued["id"], timestamp=time.time())
            self.save()
            return "PENDING"
        except (OSError, KeyError, ValueError, TypeError):
            return "UNKNOWN"


@contextmanager
def projection_lock(home: Path) -> Generator[None]:
    """All precursor writers share one lock, including different root generations."""
    path = home / "codex-wakeful.lock"
    try:
        create_new_private_bytes(path, b"")
    except FileExistsError:
        pass
    descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_mode & 0o777 != 0o600:
            raise ValueError("not an exact mode-0600 regular lock")
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        os.close(descriptor)


async def inbound_cycle(
    messages: MessageState, mailboxes: AgentMailboxState, mailbox: AgentMailbox,
    projection: Projection, client: QueueClient, cursor: UUID | None = None,
    stop: asyncio.Event | None = None,
) -> tuple[UUID | None, dict[str, str]]:
    """One metadata-only page with source revalidation before durable queue admission."""
    binding = projection.binding
    current = await mailboxes.by_endpoint_id(mailbox.endpoint_id)
    if current.status == "recovery_required":
        return None, {"source": "UNKNOWN"}
    if (current.mailbox != mailbox
            or mailbox.session_key != chat_session_key(f"codex:{binding.thread_id}")):
        return None, {"source": "STALE"}
    delivery_ids = await messages.pending_delivery_ids(mailbox, cursor)
    if delivery_ids is None:
        return None, {"source": "STALE"}
    results: dict[str, str] = {}
    for delivery_id in delivery_ids:
        await asyncio.sleep(0)
        if stop is not None and stop.is_set():
            break
        source = WakeSourceRef("switchstand_inbound", str(delivery_id))
        identity = wake_id(binding, source)
        if not await messages.pending_delivery(mailbox, delivery_id):
            results[identity] = "STALE"
            continue
        results[identity] = projection.admit(client, source)
    return (delivery_ids[-1] if len(delivery_ids) == 50 else None), results


async def run_inbound(
    messages: MessageState, mailboxes: AgentMailboxState, mailbox: AgentMailbox,
    binding: CodexBinding, home: Path, codex: Path, stop: asyncio.Event,
    *, opt_in: bool = False,
) -> None:
    """Dedicated supervised host process, never a task on the live edge's event loop."""
    if not opt_in:
        return
    with projection_lock(home):
        projection = Projection(home, binding)
        cursor = None
        while not stop.is_set():
            client = None
            try:
                client = QueueClient(codex, home)
                cursor, results = await inbound_cycle(
                    messages, mailboxes, mailbox, projection, client, cursor, stop,
                )
                for identity, result in results.items():
                    print(identity, result, flush=True)
            except (OSError, ValueError, KeyError, TypeError, SQLAlchemyError):
                print("inbound UNKNOWN", flush=True)
            finally:
                if client is not None:
                    client.close()
            try:
                await asyncio.wait_for(stop.wait(), timeout=2)
            except TimeoutError:
                pass


async def run_inbound_service(path: Path) -> None:
    config = json.loads(read_private_bytes(path))
    if set(config) != {"mailbox", "binding", "codex_home", "codex"}:
        raise ValueError("invalid inbound configuration")
    mailbox = AgentMailbox.model_validate(config["mailbox"])
    binding = CodexBinding(**config["binding"])
    home, codex = Path(config["codex_home"]), Path(config["codex"])
    if not home.is_absolute() or not codex.is_absolute() or Path(binding.start_record).parent != home:
        raise ValueError("invalid inbound binding paths")
    from switchstand.chatgpt_edge import resource_service
    async with resource_service() as (service, _runtime):
        assert service.messages is not None
        await run_inbound(service.messages, AgentMailboxState(service.messages.engine), mailbox,
                          binding, home, codex, asyncio.Event(), opt_in=True)


def inbound_main() -> None:
    parser = argparse.ArgumentParser(description="Run committed-message intake continuously")
    parser.add_argument("--config", type=Path, required=True)
    asyncio.run(run_inbound_service(parser.parse_args().config))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--opt-in", action="store_true", required=True)
    parser.add_argument("--home", type=Path, required=True)
    parser.add_argument("--codex", type=Path, required=True)
    parser.add_argument("--start-record", type=Path, required=True)
    parser.add_argument("--synthetic-delivery-file", type=Path)
    parser.add_argument("--recover-child", action="store_true")
    args = parser.parse_args()
    # An exclusive nonblocking lock on the exact existing generation token rejects a second probe.
    with args.start_record.open("rb") as token, projection_lock(args.home):
        try:
            fcntl.flock(token, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("UNAVAILABLE: simultaneous probe")
            return
        try:
            client = QueueClient(args.codex, args.home)
        except OSError:
            print("UNAVAILABLE")
            return
        try:
            binding = bind(client, args.home, args.start_record)
            if isinstance(binding, str):
                print(binding)
                return
            projection = Projection(args.home, binding)
            sources: list[WakeSourceRef] = []
            if args.synthetic_delivery_file:
                sources.append(inbound(PendingMessage.model_validate_json(
                    read_private_bytes(args.synthetic_delivery_file))))
            if args.recover_child:
                thread = client.call("thread/read", {"threadId": binding.thread_id,
                                     "includeTurns": True})["thread"]
                sources.extend(children(thread))
            for source in sources:
                result = projection.admit(client, source)
                if result == "UNKNOWN":
                    result = projection.admit(client, source)
                print(wake_id(binding, source), result)
        finally:
            client.close()


if __name__ == "__main__":
    main()
