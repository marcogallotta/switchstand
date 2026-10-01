"""Default-off Codex admission probe; no daemon or neutral outbox ownership."""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import selectors
import subprocess
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from switchstand.messages import PendingMessage
from switchstand.secure_file import atomic_replace_bytes, read_private_bytes


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


class SharedClient:
    """Explicit second client to an already-existing Codex-owned Unix socket."""

    def __init__(self, codex: Path, home: Path):
        endpoint = home / "app-server-control/app-server-control.sock"
        help_text = subprocess.check_output([str(codex), "app-server", "proxy", "--help"])
        if b"--sock" not in help_text or not endpoint.is_socket():
            raise OSError("UNAVAILABLE: unproved shared endpoint")
        self.process = subprocess.Popen(
            [str(codex), "app-server", "proxy", "--sock", str(endpoint)],
            env={**os.environ, "CODEX_HOME": str(home)}, stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        )
        self.sequence = 0
        self.buffer = b""
        try:
            self.call("initialize", {"clientInfo": {"name": "switchstand-wakeful-probe",
                      "version": "1"}, "capabilities": {"experimentalApi": True}})
            self.write({"method": "initialized", "params": {}})
        except Exception:
            self.close()
            raise

    def write(self, message: dict[str, Any]) -> None:
        assert self.process.stdin is not None
        self.process.stdin.write((json.dumps(message) + "\n").encode())
        self.process.stdin.flush()

    def call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        self.sequence += 1
        self.write({"id": self.sequence, "method": method, "params": params})
        assert self.process.stdout is not None
        with selectors.DefaultSelector() as selector:
            selector.register(self.process.stdout, selectors.EVENT_READ)
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                if b"\n" not in self.buffer:
                    if not selector.select(max(0, deadline - time.monotonic())):
                        break
                    data = os.read(self.process.stdout.fileno(), 65536)
                    if not data:
                        break
                    self.buffer += data
                    continue
                line, self.buffer = self.buffer.split(b"\n", 1)
                response = json.loads(line)
                if response.get("id") == self.sequence:
                    if "error" in response:
                        raise OSError("UNKNOWN: RPC rejected")
                    return response["result"]
        raise OSError("UNKNOWN: response lost")

    def close(self) -> None:
        # This terminates only our proxy, never the Codex-owned server.
        self.process.terminate()
        self.process.wait(timeout=5)


def bind(client: SharedClient, home: Path, token: Path) -> CodexBinding | str:
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


def current_state(thread: dict[str, Any]) -> str:
    status = thread["status"]["type"]
    if thread.get("canAcceptDirectInput") is False:
        return "ACTIVE_NOT_STEERABLE"
    if thread.get("canAcceptDirectInput") is not True:
        return "UNKNOWN"
    if status == "idle":
        return "IDLE"
    if status == "active":
        return "ACTIVE_NOT_STEERABLE" if thread["status"]["activeFlags"] else "ACTIVE_STEERABLE"
    return "UNAVAILABLE"


def children(thread: dict[str, Any]) -> list[WakeSourceRef]:
    calls = [item for turn in thread["turns"] for item in turn["items"]
             if item["type"] == "collabAgentToolCall"]
    latest = {child: state["status"] for call in calls
              for child, state in call["agentsStates"].items()}
    return [WakeSourceRef("child_completion", json.dumps([call["id"], child, latest[child]],
            separators=(",", ":"))) for call in calls if call["tool"] == "spawnAgent"
            for child in call["receiverThreadIds"]
            if latest.get(child) in {"completed", "errored", "shutdown"}]


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
        # A read cannot fence an in-flight request after a lost response.
        if record["attempted"] or thread.get("historyMode") != "legacy":
            return "UNKNOWN"
        return "PROVEN_ABSENT"

    def admit(self, client: SharedClient, source: WakeSourceRef) -> str:
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
            result = self.reconcile_thread(thread, identity)
            if result != "PROVEN_ABSENT":
                return result
            if source.source_kind == "child_completion" and source not in children(thread):
                return "STALE"
            state = current_state(thread)
            if state == "ACTIVE_NOT_STEERABLE":
                return "PENDING"
            if state not in {"IDLE", "ACTIVE_STEERABLE"}:
                return state
            record.update(attempted=True, timestamp=time.time())
            self.save()
            client.call("turn/start", {"threadId": self.binding.thread_id,
                "clientUserMessageId": identity, "input": [{"type": "text",
                "text": f'{source.source_kind} '
                        + ('delivery_id=' if source.source_kind == 'switchstand_inbound'
                           else 'parent_call_child_terminal=') + source.source_id + '. '
                        + source.reread_instruction}]})
            return "UNKNOWN"  # Only persisted clientId readback proves consumption.
        except (OSError, KeyError, ValueError, TypeError):
            return "UNKNOWN"


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
    with args.start_record.open("rb") as token:
        try:
            fcntl.flock(token, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("UNAVAILABLE: simultaneous probe")
            return
        try:
            client = SharedClient(args.codex, args.home)
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
