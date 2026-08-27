"""Durable two-role message and attempt engine for Switchstand.

The JSON snapshot is authoritative for the bounded local slice. JSONL records
transitions for inspection. A role generation plus exact attempt id fences every
result before it can become an accepted checkpoint.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import tempfile
import threading
from typing import Any, Mapping, Protocol
import uuid

from .app_server import CodexAppServer


STATE_SCHEMA = "switchstand-state-v1"
EVENT_SCHEMA = "switchstand-event-v1"
ROLE_CONTEXT_SCHEMA = "switchstand-role-context-v1"
TERMINAL_TURN_STATES = frozenset({"completed", "failed", "interrupted", "cancelled"})
MESSAGE_MARKER_PREFIX = "switchstand-message:"
MESSAGE_MARKER_PATTERN = re.compile(r"\[\[switchstand-message:[A-Za-z0-9._-]+\]\]")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex}"


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, raw = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    tmp = Path(raw)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()


def _message_text(item: Mapping[str, Any]) -> str:
    direct = item.get("text")
    if isinstance(direct, str):
        return direct
    pieces: list[str] = []
    for part in item.get("content") or []:
        if isinstance(part, Mapping) and isinstance(part.get("text"), str):
            pieces.append(str(part["text"]))
    return "\n".join(pieces)


def _message_marker(message_id: str) -> str:
    return f"[[{MESSAGE_MARKER_PREFIX}{message_id}]]"


def _submitted_message_text(message: Mapping[str, Any]) -> str:
    return f"{message['text']}\n\n{_message_marker(str(message['id']))}"


def _official_user_texts(item: Mapping[str, Any]) -> list[str] | None:
    """Return documented userMessage text content, or None for incomplete history."""
    if str(item.get("type") or "") != "userMessage":
        return []
    content = item.get("content")
    if not isinstance(content, list):
        return None
    texts: list[str] = []
    for part in content:
        if not isinstance(part, Mapping):
            return None
        part_type = str(part.get("type") or "")
        if part_type not in {"text", "image", "localImage"}:
            return None
        if part_type == "text":
            text = part.get("text")
            if not isinstance(text, str):
                return None
            texts.append(text)
    return texts


class Adapter(Protocol):
    def create_attempt(self, *, role: Mapping[str, Any], context: Mapping[str, Any]) -> str: ...
    def start_message(self, *, thread_id: str, message: Mapping[str, Any]) -> str: ...
    def interrupt(self, *, thread_id: str, turn_id: str) -> None: ...
    def inspect_turn(self, *, thread_id: str, turn_id: str) -> Mapping[str, Any]: ...
    def inspect_message(self, *, thread_id: str, message_id: str) -> Mapping[str, Any]: ...


class CodexAdapter:
    """Explicit adapter from Switchstand operations to Codex app-server calls."""

    def __init__(self, socket_path: Path | str, *, cwd: Path | str) -> None:
        self.socket_path = Path(socket_path)
        self.cwd = Path(cwd).resolve()

    def _client(self) -> CodexAppServer:
        return CodexAppServer(self.socket_path)

    def create_attempt(self, *, role: Mapping[str, Any], context: Mapping[str, Any]) -> str:
        client = self._client()
        try:
            response = client.thread_start(
                {
                    "cwd": str(self.cwd),
                    "ephemeral": False,
                    "serviceName": "Switchstand",
                    "developerInstructions": (
                        "You are one directly addressable Switchstand logical role. Work only from "
                        "the durable context below and the operator's direct messages. Do not "
                        "delegate, create tasks, mutate remote systems, release, deploy, or infer "
                        "authority outside a direct message. State uncertainty plainly.\n\n"
                        "DURABLE ROLE CONTEXT\n" + json.dumps(context, sort_keys=True)
                    ),
                }
            )
            thread = value if isinstance(value := response.get("thread"), Mapping) else {}
            thread_id = str(thread.get("id") or "")
            if not thread_id:
                raise RuntimeError("Codex thread/start returned no thread id")
            client.thread_name_set(thread_id, f"Switchstand — {role['name']}")
            return thread_id
        finally:
            client.close()

    def start_message(self, *, thread_id: str, message: Mapping[str, Any]) -> str:
        client = self._client()
        try:
            client.thread_resume(thread_id)
            response = client.turn_start_text(
                thread_id,
                _submitted_message_text(message),
                approval_policy="never",
                sandbox_policy={"type": "workspaceWrite", "writableRoots": [str(self.cwd)]},
            )
            turn = value if isinstance(value := response.get("turn"), Mapping) else {}
            turn_id = str(turn.get("id") or "")
            if not turn_id:
                raise RuntimeError("Codex turn/start returned no turn id")
            return turn_id
        finally:
            client.close()

    def interrupt(self, *, thread_id: str, turn_id: str) -> None:
        client = self._client()
        try:
            client.thread_resume(thread_id)
            client.turn_interrupt(thread_id, turn_id)
        finally:
            client.close()

    def _read(self, thread_id: str) -> Mapping[str, Any]:
        client = self._client()
        try:
            return client.thread_read(thread_id, include_turns=True)
        finally:
            client.close()

    @staticmethod
    def _turn_result(turn: Mapping[str, Any]) -> Mapping[str, Any]:
        markers: set[str] = set()
        for item in turn.get("items") or []:
            if not isinstance(item, Mapping):
                continue
            texts = _official_user_texts(item)
            if texts is not None:
                for text in texts:
                    markers.update(MESSAGE_MARKER_PATTERN.findall(text))
        messages = [
            _message_text(item)
            for item in turn.get("items") or []
            if isinstance(item, Mapping)
            and str(item.get("type") or "") in {"agentMessage", "agent_message"}
            and str(item.get("phase") or "final_answer") == "final_answer"
        ]
        output = messages[-1] if messages else None
        if output is not None:
            for marker in markers:
                output = output.replace(marker, "")
            output = output.strip()
        return {"status": str(turn.get("status") or "unknown"), "output": output}

    def inspect_turn(self, *, thread_id: str, turn_id: str) -> Mapping[str, Any]:
        response = self._read(thread_id)
        thread = value if isinstance(value := response.get("thread"), Mapping) else {}
        for turn in thread.get("turns") or []:
            if isinstance(turn, Mapping) and str(turn.get("id") or "") == turn_id:
                return self._turn_result(turn)
        return {"status": "unknown", "output": None}

    def inspect_message(self, *, thread_id: str, message_id: str) -> Mapping[str, Any]:
        response = self._read(thread_id)
        thread = value if isinstance(value := response.get("thread"), Mapping) else {}
        turns = thread.get("turns")
        complete_history = isinstance(turns, list)
        for turn in turns if isinstance(turns, list) else []:
            if not isinstance(turn, Mapping):
                complete_history = False
                continue
            turn_id = turn.get("id")
            if not isinstance(turn_id, str) or not turn_id or not isinstance(turn.get("status"), str):
                complete_history = False
            items = turn.get("items")
            if not isinstance(items, list):
                complete_history = False
                continue
            for item in items:
                if not isinstance(item, Mapping):
                    complete_history = False
                    continue
                if not isinstance(item.get("id"), str) or not item.get("id") or not isinstance(item.get("type"), str):
                    complete_history = False
                texts = _official_user_texts(item)
                if texts is None:
                    complete_history = False
                    continue
                if any(_message_marker(message_id) in text for text in texts) and isinstance(turn_id, str) and turn_id:
                    return {"found": True, "turn_id": turn_id, **self._turn_result(turn)}
        status = value if isinstance(value := thread.get("status"), Mapping) else {}
        status_type = str(status.get("type") or "unknown")
        return {
            "found": False,
            "absence_proven": status_type == "idle" and complete_history,
            "thread_status": status_type,
            "history_complete": complete_history,
        }


class Engine:
    def __init__(
        self,
        state_path: Path | str,
        adapter: Adapter,
        *,
        role_names: tuple[str, str] = ("Role A", "Role B"),
    ) -> None:
        self.state_path = Path(state_path)
        self.events_path = self.state_path.with_suffix(".jsonl")
        self.adapter = adapter
        self._lock = threading.RLock()
        if self.state_path.exists():
            value = json.loads(self.state_path.read_text(encoding="utf-8"))
            if not isinstance(value, dict) or value.get("schema") != STATE_SCHEMA:
                raise ValueError("Switchstand state has an unsupported schema")
            self.state: dict[str, Any] = value
            recovered = False
            for message in self.state.get("messages") or []:
                if message.get("status") == "dispatching":
                    message["status"] = "unknown"
                    recovered = True
            for attempt in self.state.get("attempts") or []:
                if attempt.get("status") in {"starting", "stop_pending"}:
                    attempt["status"] = "unknown"
                    recovered = True
            if recovered:
                self._save("interrupted_mutation_unknown")
        else:
            self.state = {
                "schema": STATE_SCHEMA,
                "work": {"id": "local-work", "name": "Local Work"},
                "roles": {
                    "role-a": self._new_role("role-a", role_names[0]),
                    "role-b": self._new_role("role-b", role_names[1]),
                },
                "messages": [],
                "attempts": [],
                "updated_at": _now(),
            }
            self._save("work_created", work_id="local-work")

    @staticmethod
    def _new_role(role_id: str, name: str) -> dict[str, Any]:
        return {
            "id": role_id,
            "name": name,
            "generation": 1,
            "current_attempt_id": None,
            "checkpoint": {
                "accepted_message_ids": [],
                "latest_correction": None,
                "latest_result": None,
                "updated_at": None,
            },
        }

    def _save(self, event: str, **values: Any) -> None:
        self.state["updated_at"] = _now()
        _atomic_json(self.state_path, self.state)
        record = {"schema": EVENT_SCHEMA, "at": _now(), "event": event, **values}
        self.events_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self.events_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())

    def _role(self, role_id: str) -> dict[str, Any]:
        role = self.state["roles"].get(role_id)
        if not isinstance(role, dict):
            raise KeyError(f"unknown role {role_id}")
        return role

    def _attempt(self, attempt_id: str) -> dict[str, Any]:
        for attempt in self.state["attempts"]:
            if attempt["id"] == attempt_id:
                return attempt
        raise KeyError(f"unknown attempt {attempt_id}")

    def _message(self, message_id: str) -> dict[str, Any]:
        for message in self.state["messages"]:
            if message["id"] == message_id:
                return message
        raise KeyError(f"unknown message {message_id}")

    def _messages_for(self, role_id: str) -> list[dict[str, Any]]:
        return sorted(
            (item for item in self.state["messages"] if item["role_id"] == role_id),
            key=lambda item: int(item["sequence"]),
        )

    def _context(self, role: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "schema": ROLE_CONTEXT_SCHEMA,
            "work": deepcopy(self.state["work"]),
            "role": {"id": role["id"], "name": role["name"], "generation": role["generation"]},
            "checkpoint": deepcopy(role["checkpoint"]),
            "accepted_messages": [
                {"id": item["id"], "sequence": item["sequence"], "kind": item["kind"], "text": item["text"]}
                for item in self._messages_for(str(role["id"]))
            ],
        }

    def _current_attempt(self, role: Mapping[str, Any]) -> dict[str, Any] | None:
        attempt_id = role.get("current_attempt_id")
        return self._attempt(str(attempt_id)) if attempt_id else None

    def _role_status(self, role: Mapping[str, Any]) -> str:
        attempt = self._current_attempt(role)
        queued = any(item["status"] == "queued" for item in self._messages_for(str(role["id"])))
        if attempt is None:
            return "queued" if queued else "idle"
        return {
            "running": "busy",
            "waiting": "queued" if queued else "waiting",
            "starting": "busy",
            "stop_pending": "busy",
            "stopped": "dead",
        }.get(str(attempt["status"]), str(attempt["status"]))

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            value = deepcopy(self.state)
            for role in value["roles"].values():
                source = self._role(str(role["id"]))
                role["status"] = self._role_status(source)
                role["queued_count"] = sum(
                    item["status"] == "queued" for item in self._messages_for(str(role["id"]))
                )
            return value

    def enqueue(self, role_id: str, text: str, *, kind: str = "message") -> dict[str, Any]:
        text = str(text).strip()
        if not text:
            raise ValueError("message text is required")
        if kind not in {"message", "correction"}:
            raise ValueError("message kind must be message or correction")
        with self._lock:
            role = self._role(role_id)
            message = {
                "id": _id("message"),
                "role_id": role_id,
                "sequence": len(self._messages_for(role_id)) + 1,
                "kind": kind,
                "text": text,
                "status": "queued",
                "accepted_at": _now(),
                "delivered_at": None,
                "completed_at": None,
                "attempt_id": None,
                "turn_id": None,
                "result": None,
            }
            self.state["messages"].append(message)
            if kind == "correction":
                role["checkpoint"]["latest_correction"] = text
                role["checkpoint"]["updated_at"] = _now()
            self._save("message_queued", message_id=message["id"], role_id=role_id, sequence=message["sequence"])
            if role.get("current_attempt_id") is None:
                self._create_attempt_locked(role)
            self._dispatch_locked(role)
            return deepcopy(message)

    def _create_attempt_locked(self, role: dict[str, Any]) -> dict[str, Any]:
        attempt = {
            "id": _id("attempt"),
            "role_id": role["id"],
            "generation": role["generation"],
            "thread_id": None,
            "turn_id": None,
            "message_id": None,
            "status": "starting",
            "fence_closed": False,
            "created_at": _now(),
            "started_at": None,
            "finished_at": None,
            "output": None,
            "stale_output": None,
            "error": None,
            "terminal_observed": False,
        }
        self.state["attempts"].append(attempt)
        role["current_attempt_id"] = attempt["id"]
        self._save("attempt_starting", attempt_id=attempt["id"], role_id=role["id"], generation=attempt["generation"])
        try:
            attempt["thread_id"] = self.adapter.create_attempt(role=deepcopy(role), context=self._context(role))
            attempt["status"] = "waiting"
            attempt["started_at"] = _now()
            self._save("attempt_waiting", attempt_id=attempt["id"], thread_id=attempt["thread_id"])
        except Exception as exc:
            attempt["status"] = "unknown"
            attempt["error"] = f"thread start acknowledgement unavailable: {exc}"
            self._save("attempt_unknown", attempt_id=attempt["id"], reason=attempt["error"])
        return attempt

    def _dispatch_locked(self, role: dict[str, Any]) -> None:
        attempt = self._current_attempt(role)
        if attempt is None or attempt["status"] != "waiting" or not attempt.get("thread_id"):
            return
        queued = [item for item in self._messages_for(role["id"]) if item["status"] == "queued"]
        if not queued:
            return
        message = queued[0]
        message["status"] = "dispatching"
        message["attempt_id"] = attempt["id"]
        attempt["message_id"] = message["id"]
        attempt["terminal_observed"] = False
        attempt["finished_at"] = None
        self._save("message_dispatching", message_id=message["id"], attempt_id=attempt["id"])
        try:
            turn_id = self.adapter.start_message(thread_id=attempt["thread_id"], message=deepcopy(message))
            message["turn_id"] = turn_id
            message["status"] = "delivered"
            message["delivered_at"] = _now()
            attempt["turn_id"] = turn_id
            attempt["status"] = "running"
            self._save("message_delivered", message_id=message["id"], attempt_id=attempt["id"], turn_id=turn_id)
        except Exception as exc:
            message["status"] = "unknown"
            attempt["status"] = "unknown"
            attempt["error"] = f"turn start acknowledgement unavailable: {exc}"
            self._save("delivery_unknown", message_id=message["id"], attempt_id=attempt["id"], reason=attempt["error"])

    def stop(self, attempt_id: str) -> None:
        with self._lock:
            attempt, role = self._stoppable_target_locked(attempt_id)
            role["generation"] += 1
            attempt["fence_closed"] = True
            attempt["status"] = "stop_pending"
            self._save("attempt_stop_requested", attempt_id=attempt_id, generation=role["generation"])
            try:
                if attempt.get("turn_id"):
                    self.adapter.interrupt(thread_id=attempt["thread_id"], turn_id=attempt["turn_id"])
                attempt["status"] = "stopped"
                attempt["finished_at"] = _now()
                self._save("attempt_stopped", attempt_id=attempt_id)
            except Exception as exc:
                attempt["status"] = "unknown"
                attempt["error"] = f"interrupt acknowledgement unavailable: {exc}"
                self._save("attempt_stop_unknown", attempt_id=attempt_id, reason=attempt["error"])

    def _stoppable_target_locked(self, attempt_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
        attempt = self._attempt(attempt_id)
        role = self._role(attempt["role_id"])
        if role.get("current_attempt_id") != attempt_id:
            raise ValueError("stop target is not the role's selected current attempt")
        if attempt["status"] not in {"running", "waiting"}:
            raise ValueError("selected attempt is not stoppable")
        return attempt, role

    def replace(self, attempt_id: str) -> str:
        with self._lock:
            previous = self._attempt(attempt_id)
            role = self._role(previous["role_id"])
            if role.get("current_attempt_id") != attempt_id:
                raise ValueError("replace target is not the role's selected current attempt")
            if previous["status"] in {"running", "waiting", "starting", "stop_pending"}:
                raise ValueError("stop the selected live attempt before replacement")
            if int(previous["generation"]) == int(role["generation"]):
                role["generation"] += 1
            replacement = self._create_attempt_locked(role)
            self._dispatch_locked(role)
            self._save("attempt_replaced", previous_attempt_id=attempt_id, attempt_id=replacement["id"], role_id=role["id"])
            return str(replacement["id"])

    def redirect(self, attempt_id: str, text: str) -> str:
        with self._lock:
            attempt, _role = self._stoppable_target_locked(attempt_id)
            role_id = str(attempt["role_id"])
            self.enqueue(role_id, text, kind="correction")
            self.stop(attempt_id)
            return self.replace(attempt_id)

    def _accept_completion_locked(
        self, attempt: dict[str, Any], message: dict[str, Any], status: str, output: Any
    ) -> None:
        role = self._role(attempt["role_id"])
        current = (
            role.get("current_attempt_id") == attempt["id"]
            and int(role["generation"]) == int(attempt["generation"])
            and not attempt["fence_closed"]
        )
        attempt["finished_at"] = _now()
        attempt["terminal_observed"] = True
        if status == "completed" and current:
            attempt["status"] = "waiting"
            attempt["output"] = output
            message["status"] = "completed"
            message["completed_at"] = _now()
            message["result"] = output
            checkpoint = role["checkpoint"]
            if message["id"] not in checkpoint["accepted_message_ids"]:
                checkpoint["accepted_message_ids"].append(message["id"])
            checkpoint["latest_result"] = output
            checkpoint["updated_at"] = _now()
            self._save("result_accepted", attempt_id=attempt["id"], message_id=message["id"], turn_id=attempt["turn_id"])
            self._dispatch_locked(role)
            return
        if status == "completed":
            attempt["status"] = "stale"
            attempt["stale_output"] = output
            self._save("result_stale", attempt_id=attempt["id"], message_id=message["id"], turn_id=attempt["turn_id"])
            return
        if status in {"interrupted", "cancelled"}:
            attempt["status"] = "stopped" if attempt["fence_closed"] else "failed"
            self._save("turn_interrupted", attempt_id=attempt["id"], turn_id=attempt["turn_id"])
            return
        if status == "failed":
            attempt["status"] = "failed" if current else "stale"
            attempt["error"] = "Codex turn failed"
            self._save("turn_failed", attempt_id=attempt["id"], turn_id=attempt["turn_id"])
            return
        attempt["status"] = "unknown"
        self._save("turn_unknown", attempt_id=attempt["id"], turn_id=attempt["turn_id"])

    def reconcile(self) -> None:
        """Reconcile ambiguous deliveries and observed turns without unsafe replay."""
        with self._lock:
            for message in list(self.state["messages"]):
                if message["status"] != "unknown" or not message.get("attempt_id"):
                    continue
                attempt = self._attempt(message["attempt_id"])
                if not attempt.get("thread_id"):
                    continue
                try:
                    observed = self.adapter.inspect_message(thread_id=attempt["thread_id"], message_id=message["id"])
                except Exception:
                    continue
                if not observed.get("found") and observed.get("absence_proven"):
                    message["status"] = "queued"
                    attempt["status"] = "waiting"
                    attempt["error"] = None
                    self._save("delivery_proven_absent", message_id=message["id"], attempt_id=attempt["id"])
                    self._dispatch_locked(self._role(attempt["role_id"]))
                    continue
                if not observed.get("found"):
                    continue
                message["status"] = "delivered"
                message["delivered_at"] = message["delivered_at"] or _now()
                message["turn_id"] = str(observed.get("turn_id") or "")
                attempt["turn_id"] = message["turn_id"]
                attempt["status"] = "running"
                self._save("delivery_reconciled", message_id=message["id"], attempt_id=attempt["id"], turn_id=attempt["turn_id"])

            for attempt in list(self.state["attempts"]):
                if (
                    not attempt.get("turn_id")
                    or attempt.get("terminal_observed")
                    or attempt["status"] not in {"running", "stopped", "stop_pending", "unknown"}
                ):
                    continue
                message = self._message(attempt["message_id"])
                try:
                    observed = self.adapter.inspect_turn(thread_id=attempt["thread_id"], turn_id=attempt["turn_id"])
                except Exception:
                    if attempt["status"] in {"running", "stop_pending"}:
                        attempt["status"] = "unknown"
                        self._save("turn_observation_unknown", attempt_id=attempt["id"], turn_id=attempt["turn_id"])
                    continue
                status = str(observed.get("status") or "unknown")
                if status in {"inProgress", "in_progress", "running"}:
                    if not attempt["fence_closed"]:
                        attempt["status"] = "running"
                    continue
                if status not in TERMINAL_TURN_STATES:
                    attempt["status"] = "unknown"
                    continue
                self._accept_completion_locked(attempt, message, status, observed.get("output"))

            for role in self.state["roles"].values():
                self._dispatch_locked(role)
