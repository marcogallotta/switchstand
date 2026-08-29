"""Durable two-role legacy engine with bounded external-wait admission."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import threading
import time
from typing import Any, Callable, cast, Mapping, Protocol
import uuid

from .legacy_adapter import (
    AdapterReceipt,
    CodexAdapter,
    message_marker as _message_marker,
    submitted_message_text as _submitted_message_text,
)
from .legacy_deadline import (
    DEFAULT_OPERATION_DEADLINE_SECONDS,
    DEFAULT_STARTUP_DEADLINE_SECONDS,
    LegacyDeadline,
    LegacyDeadlineExceeded,
    PersistenceUnavailable,
    PhaseResult,
)
from .legacy_persistence import (
    append_private_json as _append_private_json,
    atomic_json as _atomic_json,
    read_private_json as _read_private_json,
    require_posix_persistence as _require_posix_persistence,
)

__all__ = ["CodexAdapter", "Engine", "_message_marker", "_submitted_message_text"]


STATE_SCHEMA = "switchstand-state-v1"
EVENT_SCHEMA = "switchstand-event-v1"
ROLE_CONTEXT_SCHEMA = "switchstand-role-context-v1"
TERMINAL_TURN_STATES = frozenset({"completed", "failed", "interrupted", "cancelled"})


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex}"


class Adapter(Protocol):
    def create_attempt(self, *, role: Mapping[str, Any], context: Mapping[str, Any]) -> str: ...
    def start_message(self, *, thread_id: str, message: Mapping[str, Any]) -> str: ...
    def interrupt(self, *, thread_id: str, turn_id: str) -> None: ...
    def inspect_turn(self, *, thread_id: str, turn_id: str) -> Mapping[str, Any]: ...
    def inspect_message(self, *, thread_id: str, message_id: str) -> Mapping[str, Any]: ...


@dataclass
class _Operation:
    deadline: LegacyDeadline
    accepted: bool = False
    cutoff_finalized: bool = False

    def can_admit(self) -> bool:
        return not self.cutoff_finalized and not self.deadline.expired()


class Engine:
    def __init__(
        self,
        state_path: Path | str,
        adapter: Adapter,
        *,
        role_names: tuple[str, str] = ("Role A", "Role B"),
        startup_deadline_seconds: float = DEFAULT_STARTUP_DEADLINE_SECONDS,
        operation_deadline_seconds: float = DEFAULT_OPERATION_DEADLINE_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.state_path = Path(state_path)
        self.events_path = self.state_path.with_suffix(".jsonl")
        self.adapter = adapter
        self.startup_deadline_seconds = startup_deadline_seconds
        self.operation_deadline_seconds = operation_deadline_seconds
        self._clock = clock
        self._lock = threading.RLock()
        self.persistence_failed = False
        _require_posix_persistence()
        try:
            value = _read_private_json(self.state_path)
        except FileNotFoundError:
            value = None
        if value is not None:
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

    def _deadline(self, seconds: float | None = None) -> LegacyDeadline:
        return LegacyDeadline.after(seconds or self.operation_deadline_seconds, clock=self._clock)

    def _ensure_persistence(self) -> None:
        if self.persistence_failed:
            raise PersistenceUnavailable("legacy persistence is unavailable")

    def _save(
        self,
        event: str,
        *,
        _prepare: Callable[[], Mapping[str, Any] | None] | None = None,
        **values: Any,
    ) -> None:
        self._ensure_persistence()
        try:
            if _prepare is not None:
                values.update(_prepare() or {})
            self.state["updated_at"] = _now()
            _atomic_json(self.state_path, self.state)
            record = {"schema": EVENT_SCHEMA, "at": _now(), "event": event, **values}
            self.events_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            _append_private_json(self.events_path, record)
        except BaseException as exc:
            self.persistence_failed = True
            raise PersistenceUnavailable("legacy persistence is unavailable") from exc

    def _acquire(self, operation: _Operation, *, mode: str) -> bool:
        self._ensure_persistence()
        remaining = operation.deadline.remaining()
        acquired = remaining > 0.0 and self._lock.acquire(timeout=remaining)
        if acquired:
            try:
                self._ensure_persistence()
                if operation.deadline.expired():
                    if mode in {"startup", "background"}:
                        self._lock.release()
                        return False
                    raise LegacyDeadlineExceeded("legacy deadline exceeded")
            except BaseException:
                self._lock.release()
                raise
            return True
        if mode in {"startup", "background"}:
            return False
        raise LegacyDeadlineExceeded("legacy deadline exceeded")

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
        statuses = {"running": "busy", "waiting": "queued" if queued else "waiting", "starting": "busy"}
        return {**statuses, "stop_pending": "busy", "stopped": "dead"}.get(
            str(attempt["status"]), str(attempt["status"])
        )

    def _snapshot_locked(self) -> dict[str, Any]:
        value = deepcopy(self.state)
        for role in value["roles"].values():
            source = self._role(str(role["id"]))
            role["status"] = self._role_status(source)
            role["queued_count"] = sum(
                item["status"] == "queued" for item in self._messages_for(str(role["id"]))
            )
        return value

    def snapshot(self) -> dict[str, Any]:
        operation = _Operation(self._deadline())
        self._acquire(operation, mode="explicit")
        try:
            return self._snapshot_locked()
        finally:
            self._lock.release()

    def _new_message(self, role_id: str, text: str, kind: str) -> dict[str, Any]:
        return {
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

    def _enqueue_locked(self, role_id: str, text: str, kind: str, operation: _Operation) -> dict[str, Any]:
        role = self._role(role_id)
        if not operation.can_admit():
            raise LegacyDeadlineExceeded("legacy deadline exceeded")
        message = self._new_message(role_id, text, kind)
        if not operation.can_admit():
            raise LegacyDeadlineExceeded("legacy deadline exceeded")
        self.state["messages"].append(message)
        if kind == "correction":
            role["checkpoint"]["latest_correction"] = text
            role["checkpoint"]["updated_at"] = _now()
        self._save("message_queued", message_id=message["id"], role_id=role_id, sequence=message["sequence"])
        operation.accepted = True
        creation_eligible = False
        if operation.can_admit() and role.get("current_attempt_id") is None:
            creation_eligible = self._creation_eligible(role)
        if operation.can_admit() and creation_eligible:
            self._create_attempt_locked(role, operation, mode="enqueue")
        if operation.can_admit():
            self._dispatch_locked(role, operation)
        return message

    def _enqueue_operation(self, role_id: str, text: str, kind: str) -> tuple[dict[str, Any], dict[str, Any]]:
        text = str(text).strip()
        if not text:
            raise ValueError("message text is required")
        if kind not in {"message", "correction"}:
            raise ValueError("message kind must be message or correction")
        operation = _Operation(self._deadline())
        self._acquire(operation, mode="explicit")
        try:
            message = self._enqueue_locked(role_id, text, kind, operation)
            return deepcopy(message), self._snapshot_locked()
        finally:
            self._lock.release()

    def enqueue(self, role_id: str, text: str, *, kind: str = "message") -> dict[str, Any]:
        return self._enqueue_operation(role_id, text, kind)[0]

    def enqueue_snapshot(self, role_id: str, text: str, *, kind: str = "message") -> dict[str, Any]:
        return self._enqueue_operation(role_id, text, kind)[1]

    def _new_attempt(self, role: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "id": _id("attempt"), "role_id": role["id"], "generation": role["generation"],
            "thread_id": None, "turn_id": None, "message_id": None, "status": "starting",
            "fence_closed": False, "created_at": _now(), "started_at": None, "finished_at": None,
            "output": None, "stale_output": None, "error": None, "terminal_observed": False,
        }

    def _creation_eligible(self, role: Mapping[str, Any]) -> bool:
        current_generation = [
            attempt for attempt in self.state["attempts"]
            if attempt["role_id"] == role["id"] and int(attempt["generation"]) == int(role["generation"])
        ]
        if not current_generation:
            return True
        latest = current_generation[-1]
        return latest.get("error") == "thread_start_not_sent/setup_cutoff" and not role.get("current_attempt_id")

    def _adapter_create(
        self, role: dict[str, Any], attempt: dict[str, Any], operation: _Operation
    ) -> AdapterReceipt:
        self._ensure_persistence()
        bounded = getattr(self.adapter, "create_attempt_bounded", None)

        def on_thread_id(thread_id: str) -> None:
            if type(thread_id) is not str or not thread_id:
                raise ValueError("missing exact thread id")
            from .legacy_operations import persist_thread_identity

            persist_thread_identity(self, attempt, thread_id, operation)

        owns_bounded = "create_attempt_bounded" in type(self.adapter).__dict__
        configured = hasattr(self.adapter, "socket_path") and hasattr(self.adapter, "cwd")
        role_value = deepcopy(role)
        context = self._context(role)
        if not operation.can_admit():
            return AdapterReceipt(PhaseResult("not_sent", "thread/start", code="setup_cutoff"), "not_sent")
        if callable(bounded) and (owns_bounded or configured):
            return cast(AdapterReceipt, bounded(
                role=role_value, context=context,
                deadline=operation.deadline, on_thread_id=on_thread_id,
            ))
        try:
            on_thread_id(self.adapter.create_attempt(role=role_value, context=context))
            return AdapterReceipt(PhaseResult("acknowledged", "thread/start", {}), "sent", "acknowledged")
        except PersistenceUnavailable:
            raise
        except Exception:
            return AdapterReceipt(
                PhaseResult("ambiguous", "thread/start", code="acknowledgement_unavailable"), "ambiguous"
            )

    @staticmethod
    def _phase_error(prefix: str, phase: PhaseResult) -> str:
        if phase.disposition == "not_sent":
            return f"{prefix}_not_sent/setup_cutoff"
        if phase.phase in {"connect", "setup", "initialize", "initialized", "thread/resume"}:
            return f"setup_{phase.disposition}"
        return f"{prefix}_{phase.disposition}"

    def _create_attempt_locked(
        self,
        role: dict[str, Any],
        operation: _Operation,
        *,
        mode: str,
        advance_generation: bool = False,
    ) -> dict[str, Any] | None:
        if not operation.can_admit():
            return None
        attempt = self._new_attempt(role)
        if not operation.can_admit():
            return None
        if advance_generation:
            role["generation"] += 1
            attempt["generation"] = role["generation"]
        self.state["attempts"].append(attempt)
        role["current_attempt_id"] = attempt["id"]
        self._save("attempt_starting", attempt_id=attempt["id"], role_id=role["id"], generation=attempt["generation"])
        receipt = (
            self._adapter_create(role, attempt, operation)
            if operation.can_admit()
            else AdapterReceipt(PhaseResult("not_sent", "thread/start", code="setup_cutoff"), "not_sent")
        )
        if receipt.phase.disposition != "acknowledged":
            attempt["status"] = "failed" if receipt.phase.disposition in {"not_sent", "rejected"} else "unknown"
            attempt["error"] = self._phase_error("thread_start", receipt.phase)
            if mode == "enqueue" and receipt.phase.disposition == "not_sent":
                role["current_attempt_id"] = None
            self._save("attempt_start_closed", attempt_id=attempt["id"], disposition=attempt["error"])
        elif receipt.name_disposition and not operation.cutoff_finalized:
            attempt["error"] = (
                None if receipt.name_disposition == "acknowledged"
                else f"thread_name_{receipt.name_disposition}"
            )
            cutoff = operation.deadline.expired()
            if cutoff:
                operation.cutoff_finalized = True
            event = "attempt_waiting_cutoff_finalized" if cutoff else "attempt_name_closed"
            self._save(
                event, attempt_id=attempt["id"], thread_id=attempt["thread_id"],
                disposition=attempt["error"],
            )
        return attempt

    def _adapter_target(
        self, bounded_name: str, fallback_name: str, operation: _Operation, **kwargs: Any
    ) -> AdapterReceipt:
        self._ensure_persistence()
        bounded = getattr(self.adapter, bounded_name, None)
        owns_bounded = bounded_name in type(self.adapter).__dict__
        configured = hasattr(self.adapter, "socket_path") and hasattr(self.adapter, "cwd")
        if not operation.can_admit():
            return AdapterReceipt(PhaseResult("not_sent", fallback_name, code="setup_cutoff"), "not_sent")
        if callable(bounded) and (owns_bounded or configured):
            return cast(AdapterReceipt, bounded(deadline=operation.deadline, **kwargs))
        try:
            result = getattr(self.adapter, fallback_name)(**kwargs)
            mapping = {"value": result} if isinstance(result, str) else (
                dict(result) if isinstance(result, Mapping) else {}
            )
            return AdapterReceipt(PhaseResult("acknowledged", fallback_name, mapping), "sent")
        except Exception:
            return AdapterReceipt(
                PhaseResult("ambiguous", fallback_name, code="acknowledgement_unavailable"), "ambiguous"
            )

    def _dispatch_locked(self, role: dict[str, Any], operation: _Operation) -> None:
        attempt = self._current_attempt(role)
        if (
            not operation.can_admit()
            or attempt is None
            or attempt["status"] != "waiting"
            or not attempt.get("thread_id")
        ):
            return
        queued = [item for item in self._messages_for(role["id"]) if item["status"] == "queued"]
        if not queued:
            return
        if not operation.can_admit():
            return
        message = queued[0]
        message["status"] = "dispatching"
        message["attempt_id"] = attempt["id"]
        attempt["message_id"] = message["id"]
        attempt["terminal_observed"] = False
        attempt["finished_at"] = None
        self._save("message_dispatching", message_id=message["id"], attempt_id=attempt["id"])
        receipt = (
            self._adapter_target(
                "start_message_bounded", "start_message", operation,
                thread_id=attempt["thread_id"], message=deepcopy(message),
            )
            if operation.can_admit()
            else AdapterReceipt(PhaseResult("not_sent", "turn/start", code="setup_cutoff"), "not_sent")
        )
        phase = receipt.phase
        if phase.disposition == "acknowledged":
            result = phase.result or {}
            raw_turn = result.get("turn")
            turn = raw_turn if isinstance(raw_turn, Mapping) else {}
            raw_turn_id = turn.get("id") if isinstance(raw_turn, Mapping) else result.get("value")
            turn_id = raw_turn_id if type(raw_turn_id) is str else ""
            if turn_id:
                message["turn_id"] = turn_id
                message["status"] = "delivered"
                message["delivered_at"] = _now()
                attempt["turn_id"] = turn_id
                attempt["status"] = "running"
                attempt["error"] = None
                self._save("message_delivered", message_id=message["id"], attempt_id=attempt["id"], turn_id=turn_id)
                return
            phase = PhaseResult("ambiguous", "turn/start", code="missing_exact_acknowledgement")
        error = self._phase_error("turn_start", phase)
        attempt["error"] = error
        if phase.disposition == "not_sent":
            message["status"] = "queued"
            attempt["status"] = "waiting"
        elif phase.disposition == "rejected":
            message["status"] = "queued"
            attempt["status"] = "failed"
        else:
            message["status"] = "unknown"
            attempt["status"] = "unknown"
        self._save("delivery_closed", message_id=message["id"], attempt_id=attempt["id"], disposition=error)

    def _stoppable_target_locked(self, attempt_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
        attempt = self._attempt(attempt_id)
        role = self._role(attempt["role_id"])
        if role.get("current_attempt_id") != attempt_id:
            raise ValueError("stop target is not the role's selected current attempt")
        if attempt["status"] not in {"running", "waiting"}:
            raise ValueError("selected attempt is not stoppable")
        return attempt, role

    def _stop_locked(self, attempt_id: str, operation: _Operation) -> None:
        attempt, role = self._stoppable_target_locked(attempt_id)
        if not operation.can_admit():
            raise LegacyDeadlineExceeded("legacy deadline exceeded")
        role["generation"] += 1
        attempt["fence_closed"] = True
        if not attempt.get("turn_id"):
            attempt["status"] = "stopped"
            attempt["finished_at"] = _now()
            self._save("attempt_stopped_local", attempt_id=attempt_id, generation=role["generation"])
            operation.accepted = True
            return
        attempt["status"] = "stop_pending"
        self._save("attempt_stop_requested", attempt_id=attempt_id, generation=role["generation"])
        operation.accepted = True
        receipt = (
            self._adapter_target(
                "interrupt_bounded", "interrupt", operation,
                thread_id=attempt["thread_id"], turn_id=attempt["turn_id"],
            )
            if operation.can_admit()
            else AdapterReceipt(PhaseResult("not_sent", "turn/interrupt", code="setup_cutoff"), "not_sent")
        )
        if receipt.phase.disposition == "acknowledged":
            attempt["status"] = "stopped"
            attempt["finished_at"] = _now()
            attempt["error"] = None
            event = "attempt_stopped"
        else:
            attempt["status"] = "unknown"
            attempt["error"] = self._phase_error("interrupt", receipt.phase)
            event = "attempt_stop_unknown"
        self._save(event, attempt_id=attempt_id, disposition=attempt.get("error"))

    def _stop_operation(self, attempt_id: str) -> dict[str, Any]:
        operation = _Operation(self._deadline())
        self._acquire(operation, mode="explicit")
        try:
            self._stop_locked(attempt_id, operation)
            return self._snapshot_locked()
        finally:
            self._lock.release()

    def stop(self, attempt_id: str) -> None:
        self._stop_operation(attempt_id)

    def stop_snapshot(self, attempt_id: str) -> dict[str, Any]:
        return self._stop_operation(attempt_id)

    def _replace_locked(self, attempt_id: str, operation: _Operation) -> str:
        previous = self._attempt(attempt_id)
        role = self._role(previous["role_id"])
        if role.get("current_attempt_id") != attempt_id:
            raise ValueError("replace target is not the role's selected current attempt")
        if previous["status"] in {"running", "waiting", "starting", "stop_pending"}:
            raise ValueError("stop the selected live attempt before replacement")
        if not operation.can_admit():
            raise LegacyDeadlineExceeded("legacy deadline exceeded")
        replacement = self._create_attempt_locked(
            role,
            operation,
            mode="replace",
            advance_generation=int(previous["generation"]) == int(role["generation"]),
        )
        if replacement is None:
            raise LegacyDeadlineExceeded("legacy deadline exceeded")
        operation.accepted = True
        if operation.can_admit():
            self._dispatch_locked(role, operation)
        if operation.can_admit():
            self._save(
                "attempt_replaced", previous_attempt_id=attempt_id,
                attempt_id=replacement["id"], role_id=role["id"],
            )
        return str(replacement["id"])

    def _replace_operation(self, attempt_id: str) -> tuple[str, dict[str, Any]]:
        operation = _Operation(self._deadline())
        self._acquire(operation, mode="explicit")
        try:
            replacement_id = self._replace_locked(attempt_id, operation)
            return replacement_id, self._snapshot_locked()
        finally:
            self._lock.release()

    def replace(self, attempt_id: str) -> str:
        return self._replace_operation(attempt_id)[0]

    def replace_snapshot(self, attempt_id: str) -> dict[str, Any]:
        return self._replace_operation(attempt_id)[1]

    def redirect(self, attempt_id: str, text: str) -> str:
        return self._redirect_operation(attempt_id, text)[0]

    def redirect_snapshot(self, attempt_id: str, text: str) -> dict[str, Any]:
        return self._redirect_operation(attempt_id, text)[1]

    def _redirect_operation(self, attempt_id: str, text: str) -> tuple[str, dict[str, Any]]:
        from .legacy_operations import redirect_locked

        text = str(text).strip()
        if not text:
            raise ValueError("message text is required")
        operation = _Operation(self._deadline())
        self._acquire(operation, mode="explicit")
        try:
            replacement_id = redirect_locked(self, attempt_id, text, operation)
            return replacement_id, self._snapshot_locked()
        finally:
            self._lock.release()

    def reconcile(self) -> None:
        self._reconcile_operation(mode="explicit", want_snapshot=False)

    def reconcile_snapshot(self) -> dict[str, Any]:
        value = self._reconcile_operation(mode="explicit", want_snapshot=True)
        assert isinstance(value, dict)
        return value

    def reconcile_startup(self, deadline: LegacyDeadline) -> str:
        return str(self._reconcile_operation(mode="startup", want_snapshot=False, deadline=deadline))

    def reconcile_background(self) -> str:
        return str(self._reconcile_operation(mode="background", want_snapshot=False))

    def _reconcile_operation(
        self, *, mode: str, want_snapshot: bool, deadline: LegacyDeadline | None = None
    ) -> dict[str, Any] | str | None:
        from .legacy_operations import reconcile_locked

        operation = _Operation(deadline or self._deadline())
        if not self._acquire(operation, mode=mode):
            return "skipped_lock_timeout"
        try:
            reconcile_locked(self, operation)
            return self._snapshot_locked() if want_snapshot else "completed"
        finally:
            self._lock.release()

    def _accept_completion_locked(
        self, attempt: dict[str, Any], message: dict[str, Any], status: str, output: Any,
        operation: _Operation,
    ) -> None:
        from .legacy_operations import accept_completion_locked

        accept_completion_locked(self, attempt, message, status, output, operation)
