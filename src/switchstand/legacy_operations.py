"""Compound redirect and reconciliation operations for the legacy engine."""
from __future__ import annotations

from typing import Any, TYPE_CHECKING

from .legacy_deadline import LegacyDeadlineExceeded, PhaseResult

if TYPE_CHECKING:
    from .engine import Engine, _Operation


def persist_thread_identity(
    engine: Engine,
    attempt: dict[str, Any],
    thread_id: str,
    operation: _Operation,
) -> None:
    attempt["thread_id"] = thread_id
    attempt["status"] = "waiting"
    attempt["started_at"] = engine_module_now()

    def prepare_name_admission() -> dict[str, Any]:
        if operation.deadline.expired():
            attempt["error"] = "thread_name_not_sent"
            operation.cutoff_finalized = True
            return {"name_disposition": "not_sent"}
        attempt["error"] = "thread_name_pending"
        return {"name_disposition": "pending"}

    engine._save(
        "attempt_waiting",
        attempt_id=attempt["id"],
        thread_id=thread_id,
        _prepare=prepare_name_admission,
    )
    if not operation.cutoff_finalized and operation.deadline.expired():
        attempt["error"] = "thread_name_not_sent"
        operation.cutoff_finalized = True
        engine._save(
            "attempt_waiting_cutoff_finalized",
            attempt_id=attempt["id"],
            thread_id=thread_id,
            name_disposition="not_sent",
        )


def _finalize_redirect_cutoff(
    engine: Engine,
    old: dict[str, Any],
    replacement: dict[str, Any],
    operation: _Operation,
) -> None:
    if operation.cutoff_finalized:
        return
    if old["status"] == "stop_pending":
        old["status"] = "unknown"
        old["error"] = "interrupt_not_sent/setup_cutoff"
    if replacement["status"] == "starting":
        replacement["status"] = "failed"
        replacement["error"] = "thread_start_not_sent/setup_cutoff"
    operation.cutoff_finalized = True
    engine._save(
        "redirect_cutoff_finalized",
        previous_attempt_id=old["id"],
        attempt_id=replacement["id"],
    )


def _close_redirect_interrupt(
    engine: Engine,
    old: dict[str, Any],
    replacement: dict[str, Any],
    operation: _Operation,
) -> bool:
    receipt = engine._adapter_target(
        "interrupt_bounded",
        "interrupt",
        operation,
        thread_id=old["thread_id"],
        turn_id=old["turn_id"],
    )
    if receipt.phase.disposition == "acknowledged":
        old["status"] = "stopped"
        old["finished_at"] = engine_module_now()
        old["error"] = None
    else:
        old["status"] = "unknown"
        old["error"] = engine._phase_error("interrupt", receipt.phase)
    if operation.deadline.expired():
        _finalize_redirect_cutoff(engine, old, replacement, operation)
        return False
    engine._save("redirect_interrupt_closed", attempt_id=old["id"], disposition=old.get("error"))
    if operation.deadline.expired():
        _finalize_redirect_cutoff(engine, old, replacement, operation)
        return False
    return True


def _start_prepared_replacement(
    engine: Engine,
    role: dict[str, Any],
    replacement: dict[str, Any],
    operation: _Operation,
) -> None:
    receipt = engine._adapter_create(role, replacement, operation)
    if receipt.phase.disposition != "acknowledged":
        replacement["status"] = "failed" if receipt.phase.disposition in {"not_sent", "rejected"} else "unknown"
        replacement["error"] = engine._phase_error("thread_start", receipt.phase)
        engine._save(
            "redirect_replacement_closed",
            attempt_id=replacement["id"],
            disposition=replacement["error"],
        )
        if operation.deadline.expired():
            operation.cutoff_finalized = True
        return
    if receipt.name_disposition and not operation.cutoff_finalized:
        replacement["error"] = (
            None if receipt.name_disposition == "acknowledged"
            else f"thread_name_{receipt.name_disposition}"
        )
        if operation.deadline.expired():
            operation.cutoff_finalized = True
        engine._save(
            "redirect_replacement_name_closed",
            attempt_id=replacement["id"],
            disposition=replacement["error"],
        )


def redirect_locked(engine: Engine, attempt_id: str, text: str, operation: _Operation) -> str:
    old, role = engine._stoppable_target_locked(attempt_id)
    if not operation.can_admit():
        raise LegacyDeadlineExceeded("legacy deadline exceeded")
    correction = engine._new_message(str(role["id"]), text, "correction")
    replacement = engine._new_attempt(role)
    replacement["generation"] = int(role["generation"]) + 1
    if not operation.can_admit():
        raise LegacyDeadlineExceeded("legacy deadline exceeded")
    role["checkpoint"]["latest_correction"] = text
    role["checkpoint"]["updated_at"] = engine_module_now()
    role["generation"] += 1
    old["fence_closed"] = True
    if old.get("turn_id"):
        old["status"] = "stop_pending"
    else:
        old["status"] = "stopped"
        old["finished_at"] = engine_module_now()
    engine.state["messages"].append(correction)
    engine.state["attempts"].append(replacement)
    role["current_attempt_id"] = replacement["id"]
    engine._save(
        "redirect_prepared",
        previous_attempt_id=old["id"],
        attempt_id=replacement["id"],
        message_id=correction["id"],
        generation=role["generation"],
    )
    operation.accepted = True

    if operation.deadline.expired():
        _finalize_redirect_cutoff(engine, old, replacement, operation)
        return str(replacement["id"])
    if old.get("turn_id") and not _close_redirect_interrupt(engine, old, replacement, operation):
        return str(replacement["id"])
    if not operation.can_admit():
        _finalize_redirect_cutoff(engine, old, replacement, operation)
        return str(replacement["id"])
    _start_prepared_replacement(engine, role, replacement, operation)
    if operation.can_admit() and replacement["status"] == "waiting":
        engine._dispatch_locked(role, operation)
    return str(replacement["id"])


def _observation_closed(
    engine: Engine,
    attempt: dict[str, Any],
    phase: PhaseResult,
    *,
    event: str,
) -> bool:
    if phase.disposition == "not_sent":
        return False
    if phase.disposition in {"rejected", "ambiguous"}:
        attempt["status"] = "unknown"
        attempt["error"] = engine._phase_error("observation", phase)
        engine._save(event, attempt_id=attempt["id"], disposition=attempt["error"])
        return False
    return True


def reconcile_locked(engine: Engine, operation: _Operation) -> None:
    observed_attempts: set[str] = set()
    message_ids = [str(message["id"]) for message in engine.state["messages"]]
    for message_id in message_ids:
        if not operation.can_admit():
            return
        message = engine._message(message_id)
        if not operation.can_admit():
            return
        if message["status"] != "unknown" or not message.get("attempt_id"):
            continue
        attempt = engine._attempt(message["attempt_id"])
        if not operation.can_admit():
            return
        if not attempt.get("thread_id"):
            continue
        observed_attempts.add(str(attempt["id"]))
        receipt = engine._adapter_target(
            "inspect_message_bounded",
            "inspect_message",
            operation,
            thread_id=attempt["thread_id"],
            message_id=message["id"],
        )
        if not _observation_closed(engine, attempt, receipt.phase, event="message_observation_unknown"):
            if operation.deadline.expired():
                return
            continue
        observed = receipt.phase.result or {}
        if not observed.get("found") and observed.get("absence_proven"):
            message["status"] = "queued"
            attempt["status"] = "waiting"
            attempt["error"] = None
            engine._save("delivery_proven_absent", message_id=message["id"], attempt_id=attempt["id"])
            if operation.can_admit():
                engine._dispatch_locked(engine._role(attempt["role_id"]), operation)
        elif observed.get("found"):
            raw_turn_id = observed.get("turn_id")
            if type(raw_turn_id) is not str or not raw_turn_id:
                attempt["status"] = "unknown"
                attempt["error"] = "observation_ambiguous"
                engine._save(
                    "message_observation_unknown",
                    message_id=message["id"],
                    attempt_id=attempt["id"],
                    disposition=attempt["error"],
                )
                if operation.deadline.expired():
                    return
                continue
            message["status"] = "delivered"
            message["delivered_at"] = message["delivered_at"] or engine_module_now()
            message["turn_id"] = raw_turn_id
            attempt["turn_id"] = message["turn_id"]
            status = str(observed.get("status") or "unknown")
            if status in engine_terminal_states():
                accept_completion_locked(
                    engine,
                    attempt,
                    message,
                    status,
                    observed.get("output"),
                    operation,
                )
            else:
                attempt["status"] = (
                    "running" if status in {"inProgress", "in_progress", "running"} else "unknown"
                )
                attempt["error"] = None
                engine._save(
                    "delivery_reconciled",
                    message_id=message["id"],
                    attempt_id=attempt["id"],
                    turn_id=attempt["turn_id"],
                )

    attempt_ids = [str(attempt["id"]) for attempt in engine.state["attempts"]]
    for attempt_id in attempt_ids:
        if not operation.can_admit():
            return
        attempt = engine._attempt(attempt_id)
        if not operation.can_admit():
            return
        if attempt_id in observed_attempts or not attempt.get("turn_id") or attempt.get("terminal_observed"):
            continue
        if attempt["status"] not in {"running", "stopped", "stop_pending", "unknown"}:
            continue
        message = engine._message(attempt["message_id"])
        if not operation.can_admit():
            return
        receipt = engine._adapter_target(
            "inspect_turn_bounded",
            "inspect_turn",
            operation,
            thread_id=attempt["thread_id"],
            turn_id=attempt["turn_id"],
        )
        if not _observation_closed(engine, attempt, receipt.phase, event="turn_observation_unknown"):
            if operation.deadline.expired():
                return
            continue
        observed = receipt.phase.result or {}
        status = str(observed.get("status") or "unknown")
        if status in {"inProgress", "in_progress", "running"}:
            if not attempt["fence_closed"]:
                attempt["status"] = "running"
                attempt["error"] = None
            engine._save(
                "turn_observed_in_progress",
                attempt_id=attempt["id"],
                turn_id=attempt["turn_id"],
            )
        elif status in engine_terminal_states():
            accept_completion_locked(engine, attempt, message, status, observed.get("output"), operation)
        else:
            attempt["status"] = "unknown"
            engine._save(
                "turn_observed_unknown",
                attempt_id=attempt["id"],
                turn_id=attempt["turn_id"],
            )

    role_ids = [str(role_id) for role_id in engine.state["roles"]]
    for role_id in role_ids:
        if not operation.can_admit():
            return
        role = engine._role(role_id)
        queued = any(item["status"] == "queued" for item in engine._messages_for(role_id))
        if not operation.can_admit():
            return
        creation_eligible = False
        if queued and role.get("current_attempt_id") is None:
            creation_eligible = engine._creation_eligible(role)
        if not operation.can_admit():
            return
        if creation_eligible:
            engine._create_attempt_locked(role, operation, mode="enqueue")
        if operation.can_admit():
            engine._dispatch_locked(role, operation)


def accept_completion_locked(
    engine: Engine,
    attempt: dict[str, Any],
    message: dict[str, Any],
    status: str,
    output: Any,
    operation: _Operation,
) -> None:
    role = engine._role(attempt["role_id"])
    current = role.get("current_attempt_id") == attempt["id"] and int(role["generation"]) == int(
        attempt["generation"]
    )
    current = current and not attempt["fence_closed"]
    attempt["finished_at"] = engine_module_now()
    attempt["terminal_observed"] = True
    if status == "completed" and current:
        attempt["status"] = "waiting"
        attempt["output"] = output
        message["status"] = "completed"
        message["completed_at"] = engine_module_now()
        message["result"] = output
        checkpoint = role["checkpoint"]
        if message["id"] not in checkpoint["accepted_message_ids"]:
            checkpoint["accepted_message_ids"].append(message["id"])
        checkpoint["latest_result"] = output
        checkpoint["updated_at"] = engine_module_now()
        engine._save(
            "result_accepted",
            attempt_id=attempt["id"],
            message_id=message["id"],
            turn_id=attempt["turn_id"],
        )
        if operation.can_admit():
            engine._dispatch_locked(role, operation)
    elif status == "completed":
        attempt["status"] = "stale"
        attempt["stale_output"] = output
        engine._save("result_stale", attempt_id=attempt["id"], message_id=message["id"], turn_id=attempt["turn_id"])
    elif status in {"interrupted", "cancelled"}:
        attempt["status"] = "stopped" if attempt["fence_closed"] else "failed"
        engine._save("turn_interrupted", attempt_id=attempt["id"], turn_id=attempt["turn_id"])
    elif status == "failed":
        attempt["status"] = "failed" if current else "stale"
        attempt["error"] = "Codex turn failed"
        engine._save("turn_failed", attempt_id=attempt["id"], turn_id=attempt["turn_id"])
    else:
        attempt["status"] = "unknown"
        engine._save("turn_unknown", attempt_id=attempt["id"], turn_id=attempt["turn_id"])


def engine_module_now() -> str:
    from .engine import _now

    return _now()


def engine_terminal_states() -> frozenset[str]:
    from .engine import TERMINAL_TURN_STATES

    return TERMINAL_TURN_STATES
