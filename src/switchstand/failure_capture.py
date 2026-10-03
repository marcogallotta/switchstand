"""Best-effort durable failure capture that never delays mitigation."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

from .failure_journal import EffectState, FailureRecord
from .pending_failures import PendingFailureQueue, PendingFailureRegistry, RegistrationState


def capture_failure(
    directory: Path,
    *,
    attempted_claim: str,
    observed_result: str,
    clearing_action: str = "Inspect evidence, establish exact state, and retry only if safe.",
    effect_state: EffectState,
    owner: UUID | str = "COORDINATOR",
    attempt_id: UUID | None = None,
    operation_id: UUID | None = None,
    evidence: tuple[str, ...] = (),
) -> RegistrationState:
    """Queue one redacted record locally; capture failure remains non-blocking."""
    attempt = attempt_id or uuid4()
    try:
        registry = PendingFailureRegistry(directory)
        registry.register(attempt)
        result = PendingFailureQueue(directory).enqueue(FailureRecord(
            attempt_id=attempt,
            operation_id=operation_id or uuid4(),
            attempted_claim=attempted_claim,
            observed_result=observed_result,
            clearing_action=clearing_action,
            effect_state=effect_state,
            owner=str(owner),
            occurred_at=datetime.now(UTC),
            evidence=evidence,
        ))
        if result == "PENDING_SYNC":
            registry.mark(attempt, RegistrationState.PENDING_SYNC)
        _, blocked = registry.closure_gate()
        return RegistrationState.UNRECORDED if blocked else RegistrationState.PENDING_SYNC
    except (OSError, TypeError, ValueError):
        return RegistrationState.UNRECORDED
