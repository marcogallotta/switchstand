"""Exact-turn native emergency stop with opaque, process-local receipts."""
from __future__ import annotations

from dataclasses import dataclass
import secrets
import threading
import time
from typing import Any, Callable, Mapping, Protocol, cast

from .native_contracts import (
    NativeStopCommitResult,
    NativeStopPrepareResult,
    NativeStopStatusResult,
)
from .native_turns import NativeTurnProjection, project_native_turns


TERMINAL_OUTCOMES = frozenset({"confirmed", "not_confirmed", "rejected", "not_sent"})


class StopClient(Protocol):
    def stop_request(
        self,
        method: str,
        params: Mapping[str, Any],
        *,
        max_response_bytes: int = 256 * 1024,
        timeout_seconds: float = 3.0,
    ) -> tuple[str, Mapping[str, Any] | None]: ...


@dataclass
class _Receipt:
    agent_ref: str
    thread_id: str
    turn_id: str
    expires_at: float
    used: bool = False
    outcome: str = "not_sent"


class NativeStop:
    def __init__(self, client_factory: Callable[[], StopClient],
        resolve_agent: Callable[[str], str | None], *,
        clock: Callable[[], float] = time.monotonic, ttl_seconds: float = 30.0,
        operation_ttl_seconds: float = 300.0, capacity: int = 128) -> None:
        self._client_factory = client_factory
        self._resolve_agent = resolve_agent
        self._clock = clock
        self._ttl, self._operation_ttl = ttl_seconds, operation_ttl_seconds
        self._capacity, self._lock = capacity, threading.Lock()
        self._receipts: dict[str, _Receipt] = {}

    def _prune(self, now: float) -> None:
        self._receipts = {reference: receipt for reference, receipt in self._receipts.items()
            if receipt.expires_at > now}

    def _read(
        self, thread_id: str, *, terminal_turn_id: str | None = None
    ) -> tuple[str, NativeTurnProjection | None]:
        try:
            client = self._client_factory()
            classification, response = client.stop_request(
                "thread/read", {"threadId": thread_id, "includeTurns": True}
            )
        except Exception:
            return "unavailable", None
        if classification != "ok" or response is None:
            return classification, None
        projected = project_native_turns(
            response, thread_id, terminal_turn_id=terminal_turn_id
        )
        response = None
        return ("ok", projected) if projected is not None else ("malformed", None)

    @staticmethod
    def _sole_active(projection: NativeTurnProjection | None) -> str | None:
        if projection is None or projection.status != "active":
            return None
        return projection.active_turn_id

    def prepare(self, agent_ref: Any) -> NativeStopPrepareResult:
        if not isinstance(agent_ref, str) or not agent_ref:
            return {"code": "target_unavailable", "outcome": "not_sent"}
        with self._lock:
            self._prune(self._clock())
            if len(self._receipts) >= self._capacity:
                return {"code": "stop_capacity", "outcome": "not_sent"}
        thread_id = self._resolve_agent(agent_ref)
        if thread_id is None:
            return {"code": "target_unavailable", "outcome": "not_sent"}
        classification, projection = self._read(thread_id)
        turn_id = self._sole_active(projection)
        if classification != "ok" or turn_id is None:
            return {"code": "target_unavailable", "outcome": "not_sent"}
        now = self._clock()
        with self._lock:
            self._prune(now)
            if len(self._receipts) >= self._capacity:
                return {"code": "stop_capacity", "outcome": "not_sent"}
            reference = secrets.token_urlsafe(24)
            while reference in self._receipts:
                reference = secrets.token_urlsafe(24)
            self._receipts[reference] = _Receipt(agent_ref, thread_id, turn_id, now + self._ttl)
        return {"code": "prepared", "agentRef": agent_ref, "confirmationRef": reference}

    def commit(self, reference: Any) -> NativeStopCommitResult:
        now = self._clock()
        with self._lock:
            self._prune(now)
            receipt = self._receipts.get(reference) if isinstance(reference, str) else None
            if receipt is None or receipt.used:
                return {"code": "confirmation_unavailable", "outcome": "not_sent"}
            receipt.used, receipt.outcome = True, "in_flight"
            receipt.expires_at = now + self._operation_ttl
        classification, projection = self._read(receipt.thread_id)
        if classification != "ok" or self._sole_active(projection) != receipt.turn_id:
            outcome = "not_sent"
        else:
            try:
                client = self._client_factory()
            except Exception:
                classification, result = "not_sent", None
            else:
                try:
                    classification, result = client.stop_request("turn/interrupt",
                        {"threadId": receipt.thread_id, "turnId": receipt.turn_id})
                except Exception:
                    classification, result = "ambiguous", None
            if classification == "not_sent":
                outcome = "not_sent"
            elif classification == "rejected":
                outcome = "rejected"
            elif classification == "ok" and result == {}:
                outcome = "requested"
            else:
                outcome = "unknown"
            result = None
        with self._lock:
            receipt.outcome = outcome
        return cast(NativeStopCommitResult,
            {"code": "stop_result", "operationRef": reference, "outcome": outcome})

    def status(self, reference: Any) -> NativeStopStatusResult:
        now = self._clock()
        with self._lock:
            self._prune(now)
            receipt = self._receipts.get(reference) if isinstance(reference, str) else None
            if receipt is None or not receipt.used:
                return {"code": "operation_unavailable", "outcome": "unknown"}
            outcome = receipt.outcome
        if outcome == "in_flight":
            return cast(NativeStopStatusResult,
                {"code": "stop_pending", "operationRef": reference, "outcome": "unknown"})
        if outcome not in {"requested", "unknown"}:
            return cast(NativeStopStatusResult,
                {"code": "stop_result", "operationRef": reference, "outcome": outcome})
        classification, projection = self._read(
            receipt.thread_id, terminal_turn_id=receipt.turn_id
        )
        if projection is None:
            observed = None
        elif projection.active_turn_id == receipt.turn_id:
            observed = "inProgress"
        else:
            observed = projection.requested_terminal_status
        if classification != "ok" or observed is None:
            outcome = "unknown"
        elif observed == "interrupted":
            outcome = "confirmed"
        elif observed in {"completed", "failed"}:
            outcome = "not_confirmed"
        elif observed == "inProgress":
            outcome = "requested"
        else:
            outcome = "unknown"
        with self._lock:
            if receipt.outcome in TERMINAL_OUTCOMES:
                outcome = receipt.outcome
            else:
                receipt.outcome = outcome
        return cast(NativeStopStatusResult,
            {"code": "stop_result", "operationRef": reference, "outcome": outcome})
