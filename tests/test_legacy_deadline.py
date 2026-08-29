from __future__ import annotations

from pathlib import Path
import tempfile
import threading
import time
from typing import Any, Callable, cast, Mapping
import unittest
from unittest.mock import patch

from switchstand.engine import Engine
from switchstand.legacy_adapter import AdapterReceipt
from switchstand.legacy_deadline import (
    LegacyDeadline,
    LegacyDeadlineExceeded,
    PhaseDisposition,
    PhaseResult,
    parse_deadline_seconds,
)
from switchstand.service import Runtime


class ScriptedAdapter:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.create = PhaseResult("acknowledged", "thread/start", {"thread": {"id": "thread-1"}})
        self.start = PhaseResult("acknowledged", "turn/start", {"turn": {"id": "turn-1"}})
        self.interrupt_result = PhaseResult("acknowledged", "turn/interrupt", {})
        self.observe_message = PhaseResult(
            "acknowledged", "thread/read", {"found": False, "absence_proven": False}
        )
        self.observe_turn = PhaseResult(
            "acknowledged", "thread/read", {"status": "inProgress", "output": None}
        )
        self.on_interrupt: Callable[[], None] | None = None
        self.on_inspect_message: Callable[[], None] | None = None
        self.on_inspect_turn: Callable[[], None] | None = None
        self.start_threads: list[str] = []

    def create_attempt_bounded(self, *, on_thread_id, **_kwargs):
        self.calls.append("create")
        if self.create.disposition == "acknowledged":
            result = self.create.result or {}
            raw_thread = result.get("thread")
            thread = raw_thread if isinstance(raw_thread, Mapping) else {}
            on_thread_id(thread.get("id") or "thread-1")
        return AdapterReceipt(self.create, "sent", "acknowledged")

    def start_message_bounded(self, **kwargs):
        self.calls.append("start")
        self.start_threads.append(str(kwargs.get("thread_id") or ""))
        return AdapterReceipt(self.start, "sent")

    def interrupt_bounded(self, **_kwargs):
        self.calls.append("interrupt")
        if self.on_interrupt is not None:
            self.on_interrupt()
        return AdapterReceipt(self.interrupt_result, "sent")

    def inspect_message_bounded(self, **_kwargs):
        self.calls.append("inspect_message")
        if self.on_inspect_message is not None:
            self.on_inspect_message()
        return AdapterReceipt(self.observe_message, "sent")

    def inspect_turn_bounded(self, **_kwargs):
        self.calls.append("inspect_turn")
        if self.on_inspect_turn is not None:
            self.on_inspect_turn()
        return AdapterReceipt(self.observe_turn, "sent")

    def create_attempt(self, *, role, context):
        raise AssertionError("unbounded create used")

    def start_message(self, *, thread_id, message):
        raise AssertionError("unbounded start used")

    def interrupt(self, *, thread_id, turn_id):
        raise AssertionError("unbounded interrupt used")

    def inspect_turn(self, *, thread_id, turn_id):
        raise AssertionError("unbounded turn read used")

    def inspect_message(self, *, thread_id, message_id):
        raise AssertionError("unbounded message read used")


def current_attempt(state: Mapping[str, Any], role_id: str = "role-a") -> dict[str, Any]:
    attempt_id = state["roles"][role_id]["current_attempt_id"]
    return next(item for item in state["attempts"] if item["id"] == attempt_id)


class LegacyDeadlineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_deadline_parser_is_closed_and_bounded(self) -> None:
        self.assertEqual(parse_deadline_seconds("5", option="deadline"), 5.0)
        self.assertEqual(parse_deadline_seconds(300, option="deadline"), 300.0)
        for value in ("", " ", "no", True, False, 0, -1, 301, float("nan"), float("inf"), object()):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "greater than 0"):
                parse_deadline_seconds(value, option="deadline")

    def test_prelock_cutoff_has_zero_mutation_and_background_skips(self) -> None:
        adapter = ScriptedAdapter()
        state_path = self.root / "state.json"
        engine = Engine(state_path, adapter, operation_deadline_seconds=0.03)
        before = state_path.read_bytes(), engine.events_path.read_bytes()
        held = threading.Event()
        release = threading.Event()

        def holder() -> None:
            with engine._lock:
                held.set()
                release.wait(2)

        thread = threading.Thread(target=holder)
        thread.start()
        self.assertTrue(held.wait(1))
        try:
            with self.assertRaises(LegacyDeadlineExceeded):
                engine.enqueue("role-a", "must not be accepted")
            self.assertEqual(engine.reconcile_background(), "skipped_lock_timeout")
            self.assertEqual((state_path.read_bytes(), engine.events_path.read_bytes()), before)
            self.assertEqual(adapter.calls, [])
        finally:
            release.set()
            thread.join(1)

    def test_expiry_as_lock_acquires_rejects_every_first_mutation(self) -> None:
        class ExpiringLock:
            def __init__(self, now: list[float]) -> None:
                self.now = now
                self.releases = 0

            def acquire(self, *, timeout: float) -> bool:
                self.now[0] = 6.0
                return True

            def release(self) -> None:
                self.releases += 1

        for action in ("enqueue", "stop", "replace", "redirect"):
            with self.subTest(action=action):
                now = [0.0]
                adapter = ScriptedAdapter()
                engine = Engine(
                    self.root / f"post-lock-{action}.json",
                    adapter,
                    operation_deadline_seconds=5.0,
                    clock=lambda now=now: now[0],
                )
                target_id = ""
                if action != "enqueue":
                    engine.enqueue("role-a", "running")
                    target_id = current_attempt(engine.state)["id"]
                if action == "replace":
                    engine.stop(target_id)
                adapter.calls.clear()
                before = (
                    __import__("json").dumps(engine.state, sort_keys=True),
                    engine.state_path.read_bytes(),
                    engine.events_path.read_bytes(),
                )
                lock = ExpiringLock(now)
                engine._lock = cast(Any, lock)
                with self.assertRaises(LegacyDeadlineExceeded):
                    if action == "enqueue":
                        engine.enqueue("role-a", "blocked")
                    elif action == "stop":
                        engine.stop(target_id)
                    elif action == "replace":
                        engine.replace(target_id)
                    else:
                        engine.redirect(target_id, "blocked")
                after = (
                    __import__("json").dumps(engine.state, sort_keys=True),
                    engine.state_path.read_bytes(),
                    engine.events_path.read_bytes(),
                )
                self.assertEqual(after, before)
                self.assertEqual(adapter.calls, [])
                self.assertEqual(lock.releases, 1)

    def test_expiry_after_validation_but_before_enqueue_mutation_is_zero_effect(self) -> None:
        now = [0.0]
        adapter = ScriptedAdapter()
        engine = Engine(
            self.root / "validation-expiry.json",
            adapter,
            operation_deadline_seconds=5.0,
            clock=lambda: now[0],
        )
        before = engine.state_path.read_bytes(), engine.events_path.read_bytes()
        original_role = engine._role

        def expiring_role(role_id: str) -> dict[str, Any]:
            role = original_role(role_id)
            now[0] = 6.0
            return role

        with patch.object(engine, "_role", side_effect=expiring_role):
            with self.assertRaises(LegacyDeadlineExceeded):
                engine.enqueue("role-a", "blocked")
        self.assertEqual((engine.state_path.read_bytes(), engine.events_path.read_bytes()), before)
        self.assertEqual(engine.state["messages"], [])
        self.assertEqual(adapter.calls, [])

    def test_expiry_inside_creation_eligibility_leaves_only_the_accepted_queue(self) -> None:
        now = [0.0]
        adapter = ScriptedAdapter()
        engine = Engine(
            self.root / "eligibility-overrun.json", adapter,
            operation_deadline_seconds=5.0, clock=lambda: now[0],
        )
        original = engine._creation_eligible

        def expire(role: Mapping[str, Any]) -> bool:
            value = original(role)
            now[0] = 6.0
            return value

        with patch.object(engine, "_creation_eligible", side_effect=expire):
            state = engine.enqueue_snapshot("role-a", "accepted only")
        self.assertEqual(state["messages"][0]["status"], "queued")
        self.assertEqual(state["attempts"], [])
        self.assertIsNone(state["roles"]["role-a"]["current_attempt_id"])
        self.assertEqual(adapter.calls, [])

    def test_expiry_inside_dispatch_queue_scan_starts_no_transition(self) -> None:
        now = [0.0]
        adapter = ScriptedAdapter()
        engine = Engine(
            self.root / "dispatch-scan-overrun.json", adapter,
            operation_deadline_seconds=5.0, clock=lambda: now[0],
        )
        original = engine._messages_for
        calls = 0

        def expire_third_scan(role_id: str) -> list[dict[str, Any]]:
            nonlocal calls
            calls += 1
            value = original(role_id)
            if calls == 3:
                now[0] = 6.0
            return value

        with patch.object(engine, "_messages_for", side_effect=expire_third_scan):
            state = engine.enqueue_snapshot("role-a", "queued after scan")
        self.assertEqual(state["messages"][0]["status"], "queued")
        self.assertEqual(current_attempt(state)["status"], "waiting")
        self.assertEqual(adapter.calls, ["create"])
        events = engine.events_path.read_text(encoding="utf-8")
        self.assertNotIn("message_dispatching", events)
        self.assertNotIn("delivery_closed", events)

    def test_reconcile_role_scan_expiry_creates_no_new_attempt(self) -> None:
        now = [0.0]
        adapter = ScriptedAdapter()
        adapter.create = PhaseResult("not_sent", "connect", code="setup_cutoff")
        engine = Engine(
            self.root / "reconcile-role-overrun.json", adapter,
            operation_deadline_seconds=5.0, clock=lambda: now[0],
        )
        engine.enqueue("role-a", "deferred creation")
        before_attempts = len(engine.state["attempts"])
        adapter.calls.clear()
        original = engine._messages_for

        def expire_role_scan(role_id: str) -> list[dict[str, Any]]:
            value = original(role_id)
            now[0] = 6.0
            return value

        with patch.object(engine, "_messages_for", side_effect=expire_role_scan):
            state = engine.reconcile_snapshot()
        self.assertEqual(len(state["attempts"]), before_attempts)
        self.assertIsNone(state["roles"]["role-a"]["current_attempt_id"])
        self.assertEqual(adapter.calls, [])

    def test_post_acquire_cutoff_keeps_startup_and_background_as_skips(self) -> None:
        class ExpiringLock:
            def acquire(self, *, timeout: float) -> bool:
                now[0] = 6.0
                return True

            def release(self) -> None:
                releases.append(True)

        now = [0.0]
        releases: list[bool] = []
        engine = Engine(
            self.root / "post-acquire-skips.json",
            ScriptedAdapter(),
            operation_deadline_seconds=5.0,
            clock=lambda: now[0],
        )
        engine._lock = cast(Any, ExpiringLock())
        self.assertEqual(engine.reconcile_background(), "skipped_lock_timeout")
        now[0] = 0.0
        self.assertEqual(engine.reconcile_startup(LegacyDeadline(5.0, lambda: now[0])), "skipped_lock_timeout")
        self.assertEqual(releases, [True, True])

    def test_startup_lock_cutoff_starts_observer_once_without_waiting_for_holder(self) -> None:
        engine = Engine(
            self.root / "startup.json",
            ScriptedAdapter(),
            startup_deadline_seconds=0.03,
            operation_deadline_seconds=0.03,
        )
        held = threading.Event()
        release = threading.Event()

        def holder() -> None:
            with engine._lock:
                held.set()
                release.wait(2)

        thread = threading.Thread(target=holder)
        thread.start()
        self.assertTrue(held.wait(1))
        runtime = Runtime(engine)
        started = time.monotonic()
        try:
            runtime.start()
            self.assertLess(time.monotonic() - started, 0.3)
            self.assertTrue(runtime.observer.is_alive())
        finally:
            runtime.close()
            release.set()
            thread.join(1)

    def test_post_acceptance_target_cutoff_returns_exact_partial_state(self) -> None:
        adapter = ScriptedAdapter()
        adapter.start = PhaseResult("not_sent", "turn/start", code="setup_cutoff")
        engine = Engine(self.root / "partial.json", adapter)
        state = engine.enqueue_snapshot("role-a", "accepted queue")
        attempt = current_attempt(state)
        self.assertEqual(state["messages"][0]["status"], "queued")
        self.assertEqual(attempt["status"], "waiting")
        self.assertEqual(attempt["error"], "turn_start_not_sent/setup_cutoff")
        self.assertEqual(adapter.calls, ["create", "start"])

    def test_creation_eligibility_retries_only_exact_setup_cutoff(self) -> None:
        adapter = ScriptedAdapter()
        adapter.create = PhaseResult("not_sent", "connect", code="setup_cutoff")
        engine = Engine(self.root / "eligible.json", adapter)
        first = engine.enqueue_snapshot("role-a", "queued")
        self.assertIsNone(first["roles"]["role-a"]["current_attempt_id"])
        self.assertEqual(len(first["attempts"]), 1)
        adapter.create = PhaseResult("rejected", "initialize", code="setup_rejected")
        second = engine.reconcile_snapshot()
        self.assertEqual(len(second["attempts"]), 2)
        selected = current_attempt(second)
        self.assertEqual(selected["status"], "failed")
        engine.reconcile()
        self.assertEqual(len(engine.state["attempts"]), 2)
        self.assertEqual(adapter.calls.count("create"), 2)

    def test_rejected_ambiguous_or_exact_creation_consumes_automatic_authority(self) -> None:
        for disposition, phase in (
            ("rejected", "initialize"),
            ("ambiguous", "setup"),
            ("acknowledged", "thread/start"),
        ):
            with self.subTest(disposition=disposition):
                adapter = ScriptedAdapter()
                if disposition == "acknowledged":
                    adapter.start = PhaseResult("not_sent", "turn/start", code="setup_cutoff")
                else:
                    adapter.create = PhaseResult(
                        cast(PhaseDisposition, disposition), phase, code=f"setup_{disposition}"
                    )
                engine = Engine(self.root / f"authority-{disposition}.json", adapter)
                engine.enqueue("role-a", "queued")
                engine.reconcile()
                self.assertEqual(adapter.calls.count("create"), 1)
                self.assertEqual(len(engine.state["attempts"]), 1)
                self.assertIsNotNone(engine.state["roles"]["role-a"]["current_attempt_id"])

    def test_waiting_stop_is_local_and_generation_advances_once(self) -> None:
        adapter = ScriptedAdapter()
        adapter.start = PhaseResult("not_sent", "turn/start", code="setup_cutoff")
        engine = Engine(self.root / "waiting-stop.json", adapter)
        state = engine.enqueue_snapshot("role-a", "queued")
        attempt = current_attempt(state)
        stopped = engine.stop_snapshot(attempt["id"])
        record = next(item for item in stopped["attempts"] if item["id"] == attempt["id"])
        self.assertEqual(record["status"], "stopped")
        self.assertTrue(record["fence_closed"])
        self.assertEqual(stopped["roles"]["role-a"]["generation"], 2)
        self.assertNotIn("interrupt", adapter.calls)

    def test_stop_closes_each_interrupt_disposition_once_without_retry(self) -> None:
        for disposition, expected_status, expected_error in (
            ("not_sent", "unknown", "interrupt_not_sent/setup_cutoff"),
            ("rejected", "unknown", "interrupt_rejected"),
            ("ambiguous", "unknown", "interrupt_ambiguous"),
            ("acknowledged", "stopped", None),
        ):
            with self.subTest(disposition=disposition):
                adapter = ScriptedAdapter()
                engine = Engine(self.root / f"stop-{disposition}.json", adapter)
                engine.enqueue("role-a", "running")
                target_id = current_attempt(engine.state)["id"]
                adapter.interrupt_result = PhaseResult(
                    cast(PhaseDisposition, disposition),
                    "turn/interrupt",
                    {} if disposition == "acknowledged" else None,
                    "setup_cutoff" if disposition == "not_sent" else None,
                )
                state = engine.stop_snapshot(target_id)
                record = next(item for item in state["attempts"] if item["id"] == target_id)
                self.assertEqual(record["status"], expected_status)
                self.assertEqual(record["error"], expected_error)
                self.assertTrue(record["fence_closed"])
                self.assertEqual(state["roles"]["role-a"]["generation"], 2)
                engine.reconcile()
                self.assertEqual(adapter.calls.count("interrupt"), 1)

    def test_waiting_redirect_never_interrupts_and_starts_one_replacement(self) -> None:
        adapter = ScriptedAdapter()
        adapter.start = PhaseResult("not_sent", "turn/start", code="setup_cutoff")
        engine = Engine(self.root / "waiting-redirect.json", adapter)
        waiting = current_attempt(engine.enqueue_snapshot("role-a", "queued"))
        adapter.start = PhaseResult("acknowledged", "turn/start", {"turn": {"id": "turn-2"}})
        state = engine.redirect_snapshot(waiting["id"], "corrected")
        old = next(item for item in state["attempts"] if item["id"] == waiting["id"])
        self.assertEqual(old["status"], "stopped")
        self.assertTrue(old["fence_closed"])
        self.assertNotIn("interrupt", adapter.calls)
        self.assertEqual(adapter.calls.count("create"), 2)
        self.assertEqual(current_attempt(state)["status"], "running")

    def test_target_rejection_is_queued_failed_and_never_auto_resent(self) -> None:
        adapter = ScriptedAdapter()
        adapter.start = PhaseResult("rejected", "turn/start", code="app_server_rejected")
        engine = Engine(self.root / "turn-rejected.json", adapter)
        state = engine.enqueue_snapshot("role-a", "rejected")
        self.assertEqual(state["messages"][0]["status"], "queued")
        self.assertEqual(current_attempt(state)["status"], "failed")
        self.assertEqual(current_attempt(state)["error"], "turn_start_rejected")
        engine.reconcile()
        self.assertEqual(adapter.calls.count("start"), 1)

    def test_numeric_turn_identity_is_ambiguous_and_never_persisted(self) -> None:
        adapter = ScriptedAdapter()
        adapter.start = PhaseResult("acknowledged", "turn/start", {"turn": {"id": 123}})
        engine = Engine(self.root / "numeric-turn.json", adapter)
        state = engine.enqueue_snapshot("role-a", "malformed acknowledgement")
        record = current_attempt(state)
        self.assertEqual(record["status"], "unknown")
        self.assertIsNone(record["turn_id"])
        self.assertEqual(record["error"], "turn_start_ambiguous")
        self.assertEqual(state["messages"][0]["status"], "unknown")

    def test_observation_rejection_is_unknown_and_a_later_pass_may_read_again(self) -> None:
        adapter = ScriptedAdapter()
        engine = Engine(self.root / "observe-later.json", adapter)
        engine.enqueue("role-a", "running")
        adapter.observe_turn = PhaseResult("rejected", "thread/read", code="app_server_rejected")
        engine.reconcile()
        self.assertEqual(current_attempt(engine.state)["status"], "unknown")
        first_count = adapter.calls.count("inspect_turn")
        adapter.observe_turn = PhaseResult(
            "acknowledged", "thread/read", {"status": "inProgress", "output": None}
        )
        engine.reconcile()
        self.assertEqual(adapter.calls.count("inspect_turn"), first_count + 1)
        self.assertEqual(current_attempt(engine.state)["status"], "running")

    def test_each_acknowledged_observation_transition_survives_restart(self) -> None:
        adapter = ScriptedAdapter()
        state_path = self.root / "observation-restart.json"
        engine = Engine(state_path, adapter)
        engine.enqueue("role-a", "running")
        adapter.observe_turn = PhaseResult("rejected", "thread/read", code="app_server_rejected")
        engine.reconcile()
        self.assertEqual(current_attempt(engine.state)["status"], "unknown")
        adapter.observe_turn = PhaseResult(
            "acknowledged", "thread/read", {"status": "inProgress", "output": None}
        )
        engine.reconcile()
        self.assertEqual(current_attempt(engine.state)["status"], "running")
        self.assertEqual(current_attempt(Engine(state_path, ScriptedAdapter()).state)["status"], "running")
        adapter.observe_turn = PhaseResult(
            "acknowledged", "thread/read", {"status": "futureState", "output": None}
        )
        engine.reconcile()
        self.assertEqual(current_attempt(engine.state)["status"], "unknown")
        self.assertEqual(current_attempt(Engine(state_path, ScriptedAdapter()).state)["status"], "unknown")

    def test_terminal_fact_from_unknown_message_is_closed_even_when_response_expires_cutoff(self) -> None:
        now = [0.0]
        adapter = ScriptedAdapter()
        adapter.start = PhaseResult("ambiguous", "turn/start", code="acknowledgement_unavailable")
        state_path = self.root / "unknown-terminal-cutoff.json"
        engine = Engine(
            state_path,
            adapter,
            operation_deadline_seconds=5.0,
            clock=lambda: now[0],
        )
        engine.enqueue("role-a", "unknown delivery")
        message = engine.state["messages"][0]
        self.assertEqual(message["status"], "unknown")
        adapter.observe_message = PhaseResult(
            "acknowledged",
            "thread/read",
            {
                "found": True,
                "turn_id": "turn-recovered",
                "status": "completed",
                "output": "exact result",
            },
        )
        adapter.on_inspect_message = lambda: now.__setitem__(0, 6.0)
        state = engine.reconcile_snapshot()
        self.assertEqual(state["messages"][0]["status"], "completed")
        self.assertEqual(state["messages"][0]["result"], "exact result")
        restarted = Engine(state_path, ScriptedAdapter()).snapshot()
        self.assertEqual(restarted["messages"][0]["status"], "completed")
        self.assertEqual(restarted["messages"][0]["result"], "exact result")

    def test_reconcile_reuses_one_shrinking_budget_across_multiple_records(self) -> None:
        now = [0.0]
        adapter = ScriptedAdapter()
        engine = Engine(
            self.root / "multi-record.json",
            adapter,
            operation_deadline_seconds=5.0,
            clock=lambda: now[0],
        )
        engine.enqueue("role-a", "first")
        engine.enqueue("role-b", "second")
        adapter.calls.clear()
        adapter.observe_turn = PhaseResult(
            "acknowledged", "thread/read", {"status": "completed", "output": "done"}
        )
        adapter.on_inspect_turn = lambda: now.__setitem__(0, 6.0)
        engine.reconcile()
        self.assertEqual(adapter.calls.count("inspect_turn"), 1)
        self.assertEqual(current_attempt(engine.state, "role-a")["status"], "waiting")
        self.assertEqual(current_attempt(engine.state, "role-b")["status"], "running")
        adapter.on_inspect_turn = None
        engine.reconcile()
        self.assertEqual(adapter.calls.count("inspect_turn"), 2)
        self.assertEqual(current_attempt(engine.state, "role-b")["status"], "waiting")

if __name__ == "__main__":
    unittest.main()
