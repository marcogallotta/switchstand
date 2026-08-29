from __future__ import annotations

import json
from pathlib import Path
import tempfile
from typing import Any, Callable
import unittest
from unittest.mock import patch

import switchstand.engine as engine_module
from switchstand.engine import Engine
from switchstand.legacy_adapter import AdapterReceipt
from switchstand.legacy_deadline import LegacyDeadlineExceeded, PersistenceUnavailable, PhaseResult
from tests.test_legacy_deadline import ScriptedAdapter, current_attempt


def persisted_bytes(engine: Engine) -> tuple[str, bytes, bytes]:
    return (
        json.dumps(engine.state, sort_keys=True),
        engine.state_path.read_bytes(),
        engine.events_path.read_bytes(),
    )


class LateThreadAdapter(ScriptedAdapter):
    def __init__(
        self,
        before_callback: Callable[[], None] | None = None,
        *,
        name_disposition: str = "acknowledged",
    ) -> None:
        super().__init__()
        self.before_callback = before_callback
        self.before_name: Callable[[], None] | None = None
        self.after_name: Callable[[], None] | None = None
        self.name_disposition = name_disposition

    def create_attempt_bounded(self, *, deadline, on_thread_id, **_kwargs):
        self.calls.append("create")
        if self.before_callback is not None:
            self.before_callback()
        on_thread_id("thread-1")
        if self.before_name is not None:
            self.before_name()
        if not deadline.expired():
            self.calls.append("name")
            name_disposition = self.name_disposition
            if self.after_name is not None:
                self.after_name()
        else:
            name_disposition = "not_sent"
        return AdapterReceipt(self.create, "not_sent", name_disposition)

    def start_message_bounded(self, **kwargs):
        return super().start_message_bounded(**kwargs)

    def interrupt_bounded(self, **kwargs):
        return super().interrupt_bounded(**kwargs)


class LegacyCutoffSeamTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def assert_exact_thread_cutoff(
        self,
        state: dict[str, Any],
        restarted: dict[str, Any],
        saves: list[tuple[str, float]],
        adapter: LateThreadAdapter,
        *,
        waiting_events: list[str] | None = None,
    ) -> None:
        attempt = current_attempt(state)
        restarted_attempt = current_attempt(restarted)
        self.assertEqual((attempt["thread_id"], attempt["status"]), ("thread-1", "waiting"))
        self.assertEqual(attempt["error"], "thread_name_not_sent")
        self.assertEqual(
            (restarted_attempt["thread_id"], restarted_attempt["error"]),
            ("thread-1", "thread_name_not_sent"),
        )
        self.assertEqual(
            [event for event, _at in saves if event.startswith("attempt_waiting")],
            waiting_events or ["attempt_waiting"],
        )
        self.assertNotIn("attempt_name_closed", [event for event, _at in saves])
        self.assertEqual(adapter.calls, ["create"])

    def test_enqueue_message_construction_expiry_is_byte_for_byte_zero_effect(self) -> None:
        now = [0.0]
        adapter = ScriptedAdapter()
        engine = Engine(
            self.root / "enqueue-construction.json",
            adapter,
            operation_deadline_seconds=5.0,
            clock=lambda: now[0],
        )
        before = persisted_bytes(engine)
        original = engine._messages_for

        def expire_message_scan(role_id: str) -> list[dict[str, Any]]:
            value = original(role_id)
            now[0] = 6.0
            return value

        with (
            patch.object(engine, "_messages_for", side_effect=expire_message_scan),
            self.assertRaises(LegacyDeadlineExceeded),
        ):
            engine.enqueue("role-a", "must not be accepted")
        self.assertEqual(persisted_bytes(engine), before)
        self.assertEqual(adapter.calls, [])

    def test_redirect_message_construction_expiry_is_byte_for_byte_zero_effect(self) -> None:
        now = [0.0]
        adapter = ScriptedAdapter()
        engine = Engine(
            self.root / "redirect-construction.json",
            adapter,
            operation_deadline_seconds=5.0,
            clock=lambda: now[0],
        )
        target_id = current_attempt(engine.enqueue_snapshot("role-a", "running"))["id"]
        adapter.calls.clear()
        before = persisted_bytes(engine)
        original = engine._messages_for

        def expire_message_scan(role_id: str) -> list[dict[str, Any]]:
            value = original(role_id)
            now[0] = 6.0
            return value

        with (
            patch.object(engine, "_messages_for", side_effect=expire_message_scan),
            self.assertRaises(LegacyDeadlineExceeded),
        ):
            engine.redirect(target_id, "must not be accepted")
        self.assertEqual(persisted_bytes(engine), before)
        self.assertEqual(adapter.calls, [])

    def test_attempt_context_scan_expiry_admits_no_adapter_phase(self) -> None:
        now = [0.0]
        adapter = ScriptedAdapter()
        engine = Engine(
            self.root / "attempt-context.json",
            adapter,
            operation_deadline_seconds=5.0,
            clock=lambda: now[0],
        )
        original = engine._messages_for
        calls = 0

        def expire_second_scan(role_id: str) -> list[dict[str, Any]]:
            nonlocal calls
            calls += 1
            value = original(role_id)
            if calls == 2:
                now[0] = 6.0
            return value

        with patch.object(engine, "_messages_for", side_effect=expire_second_scan):
            state = engine.enqueue_snapshot("role-a", "accepted before context scan")
        attempt = state["attempts"][0]
        self.assertEqual(attempt["status"], "failed")
        self.assertEqual(attempt["error"], "thread_start_not_sent/setup_cutoff")
        self.assertIsNone(state["roles"]["role-a"]["current_attempt_id"])
        self.assertEqual(adapter.calls, [])

    def test_reconcile_message_lookup_expiry_admits_no_observation_or_mutation(self) -> None:
        now = [0.0]
        adapter = ScriptedAdapter()
        adapter.start = PhaseResult("ambiguous", "turn/start", code="acknowledgement_unavailable")
        adapter.observe_message = PhaseResult(
            "acknowledged",
            "thread/read",
            {"found": True, "turn_id": "turn-late", "status": "completed", "output": "late"},
        )
        engine = Engine(
            self.root / "reconcile-message-lookup.json",
            adapter,
            operation_deadline_seconds=5.0,
            clock=lambda: now[0],
        )
        engine.enqueue("role-a", "unknown delivery")
        adapter.calls.clear()
        before = persisted_bytes(engine)
        original = engine._message

        def expire_lookup(message_id: str) -> dict[str, Any]:
            value = original(message_id)
            now[0] = 6.0
            return value

        with patch.object(engine, "_message", side_effect=expire_lookup):
            engine.reconcile_snapshot()
        self.assertEqual(persisted_bytes(engine), before)
        self.assertEqual(adapter.calls, [])

    def test_reconcile_turn_lookup_expiry_admits_no_observation_or_mutation(self) -> None:
        now = [0.0]
        adapter = ScriptedAdapter()
        adapter.observe_turn = PhaseResult(
            "acknowledged", "thread/read", {"status": "completed", "output": "late"}
        )
        engine = Engine(
            self.root / "reconcile-turn-lookup.json",
            adapter,
            operation_deadline_seconds=5.0,
            clock=lambda: now[0],
        )
        engine.enqueue("role-a", "running")
        adapter.calls.clear()
        before = persisted_bytes(engine)
        original = engine._message
        calls = 0

        def expire_second_lookup(message_id: str) -> dict[str, Any]:
            nonlocal calls
            calls += 1
            value = original(message_id)
            if calls == 2:
                now[0] = 6.0
            return value

        with patch.object(engine, "_message", side_effect=expire_second_lookup):
            engine.reconcile_snapshot()
        self.assertEqual(persisted_bytes(engine), before)
        self.assertEqual(adapter.calls, [])

    def test_exact_thread_id_expiry_before_callback_finalizes_once(self) -> None:
        now = [0.0]
        adapter = LateThreadAdapter(lambda: now.__setitem__(0, 6.0))
        state_path = self.root / "late-thread-id.json"
        engine = Engine(
            state_path,
            adapter,
            operation_deadline_seconds=5.0,
            clock=lambda: now[0],
        )
        saves: list[tuple[str, float]] = []
        original_save = engine._save

        def record_save(event: str, **values: Any) -> None:
            saves.append((event, now[0]))
            original_save(event, **values)

        with patch.object(engine, "_save", side_effect=record_save):
            state = engine.enqueue_snapshot("role-a", "accepted")
        restarted = Engine(state_path, ScriptedAdapter()).snapshot()
        self.assert_exact_thread_cutoff(state, restarted, saves, adapter)
        self.assertEqual([event for event, at in saves if at > 5.0], ["attempt_waiting"])

    def test_exact_thread_id_expiry_inside_callback_timestamp_finalizes_once(self) -> None:
        now = [0.0]
        adapter = LateThreadAdapter()
        state_path = self.root / "thread-id-timestamp-cutoff.json"
        engine = Engine(
            state_path,
            adapter,
            operation_deadline_seconds=5.0,
            clock=lambda: now[0],
        )
        saves: list[tuple[str, float]] = []
        original_save = engine._save

        def expire_started_at() -> str:
            attempts = engine.state["attempts"]
            attempt = attempts[-1] if attempts else {}
            if attempt.get("thread_id") == "thread-1" and attempt.get("started_at") is None:
                now[0] = 6.0
            return "2026-08-29T00:00:00Z"

        def record_save(event: str, **values: Any) -> None:
            saves.append((event, now[0]))
            original_save(event, **values)

        with (
            patch("switchstand.engine._now", side_effect=expire_started_at),
            patch.object(engine, "_save", side_effect=record_save),
        ):
            state = engine.enqueue_snapshot("role-a", "accepted")
        restarted = Engine(state_path, ScriptedAdapter()).snapshot()
        self.assert_exact_thread_cutoff(state, restarted, saves, adapter)
        self.assertEqual([event for event, at in saves if at > 5.0], ["attempt_waiting"])

    def test_exact_thread_id_persistence_overrun_gets_one_cutoff_finalization_save(self) -> None:
        now = [0.0]
        adapter = LateThreadAdapter()
        state_path = self.root / "thread-id-persistence-overrun.json"
        engine = Engine(
            state_path,
            adapter,
            operation_deadline_seconds=5.0,
            clock=lambda: now[0],
        )
        saves: list[tuple[str, float]] = []
        original_save = engine._save

        def overrun_waiting_save(event: str, **values: Any) -> None:
            saves.append((event, now[0]))
            original_save(event, **values)
            if event == "attempt_waiting":
                now[0] = 6.0

        with patch.object(engine, "_save", side_effect=overrun_waiting_save):
            state = engine.enqueue_snapshot("role-a", "accepted")
        restarted = Engine(state_path, ScriptedAdapter()).snapshot()
        self.assert_exact_thread_cutoff(
            state,
            restarted,
            saves,
            adapter,
            waiting_events=["attempt_waiting", "attempt_waiting_cutoff_finalized"],
        )
        self.assertEqual(
            [event for event, at in saves if at > 5.0],
            ["attempt_waiting_cutoff_finalized"],
        )

    def test_exact_thread_id_expiry_after_sample_before_save_finalizes_once(self) -> None:
        now = [0.0]
        adapter = LateThreadAdapter()
        state_path = self.root / "thread-id-before-save-cutoff.json"
        engine = Engine(
            state_path,
            adapter,
            operation_deadline_seconds=5.0,
            clock=lambda: now[0],
        )
        saves: list[tuple[str, float]] = []
        original_save = engine._save

        def expire_before_waiting_save(event: str, **values: Any) -> None:
            if event == "attempt_waiting":
                now[0] = 6.0
            saves.append((event, now[0]))
            original_save(event, **values)

        with patch.object(engine, "_save", side_effect=expire_before_waiting_save):
            state = engine.enqueue_snapshot("role-a", "accepted")
        restarted = Engine(state_path, ScriptedAdapter()).snapshot()
        self.assert_exact_thread_cutoff(state, restarted, saves, adapter)
        self.assertEqual([event for event, at in saves if at > 5.0], ["attempt_waiting"])

    def test_enqueue_name_outcomes_are_exact_and_restart_stable(self) -> None:
        expected_errors = {
            "acknowledged": None,
            "rejected": "thread_name_rejected",
            "ambiguous": "thread_name_ambiguous",
            "not_sent": "thread_name_not_sent",
        }
        for disposition, expected_error in expected_errors.items():
            with self.subTest(disposition=disposition):
                now = [0.0]
                adapter = LateThreadAdapter(name_disposition=disposition)
                if disposition == "not_sent":
                    adapter.before_name = lambda current_now=now: current_now.__setitem__(0, 6.0)
                else:
                    adapter.after_name = lambda current_now=now: current_now.__setitem__(0, 6.0)
                state_path = self.root / f"enqueue-name-{disposition}.json"
                engine = Engine(
                    state_path,
                    adapter,
                    operation_deadline_seconds=5.0,
                    clock=lambda current_now=now: current_now[0],
                )
                state = engine.enqueue_snapshot("role-a", disposition)
                restarted = Engine(state_path, ScriptedAdapter()).snapshot()
                self.assertEqual(current_attempt(state)["error"], expected_error)
                self.assertEqual(current_attempt(restarted)["error"], expected_error)
                self.assertEqual(current_attempt(restarted)["thread_id"], "thread-1")
                self.assertEqual(adapter.calls.count("create"), 1)
                self.assertEqual(adapter.calls.count("name"), disposition != "not_sent")

    def test_redirect_name_outcomes_are_exact_and_restart_stable(self) -> None:
        expected_errors = {
            "acknowledged": None,
            "rejected": "thread_name_rejected",
            "ambiguous": "thread_name_ambiguous",
            "not_sent": "thread_name_not_sent",
        }
        for disposition, expected_error in expected_errors.items():
            with self.subTest(disposition=disposition):
                now = [0.0]
                adapter = LateThreadAdapter(name_disposition=disposition)
                state_path = self.root / f"redirect-name-{disposition}.json"
                engine = Engine(
                    state_path,
                    adapter,
                    operation_deadline_seconds=5.0,
                    clock=lambda current_now=now: current_now[0],
                )
                old_id = current_attempt(engine.enqueue_snapshot("role-a", "old"))["id"]
                adapter.calls.clear()
                if disposition == "not_sent":
                    adapter.before_name = lambda current_now=now: current_now.__setitem__(0, 6.0)
                else:
                    adapter.after_name = lambda current_now=now: current_now.__setitem__(0, 6.0)
                state = engine.redirect_snapshot(old_id, disposition)
                restarted = Engine(state_path, ScriptedAdapter()).snapshot()
                self.assertEqual(current_attempt(state)["error"], expected_error)
                self.assertEqual(current_attempt(restarted)["error"], expected_error)
                self.assertEqual(current_attempt(restarted)["thread_id"], "thread-1")
                self.assertEqual(adapter.calls.count("create"), 1)
                self.assertEqual(adapter.calls.count("name"), disposition != "not_sent")
                self.assertEqual(adapter.calls.count("interrupt"), 1)

    def test_enqueue_name_closure_write_failure_restarts_pending_without_retry(self) -> None:
        self.assert_name_closure_write_failure(redirect=False)

    def test_redirect_name_closure_write_failure_restarts_pending_without_retry(self) -> None:
        self.assert_name_closure_write_failure(redirect=True)

    def assert_name_closure_write_failure(self, *, redirect: bool) -> None:
        expected_errors = {
            "acknowledged": None,
            "rejected": "thread_name_rejected",
            "ambiguous": "thread_name_ambiguous",
            "not_sent": "thread_name_not_sent",
        }
        for disposition, closure_error in expected_errors.items():
            with self.subTest(redirect=redirect, disposition=disposition):
                now = [0.0]
                adapter = LateThreadAdapter(name_disposition=disposition)
                prefix = "redirect" if redirect else "enqueue"
                state_path = self.root / f"{prefix}-name-failure-{disposition}.json"
                engine = Engine(
                    state_path,
                    adapter,
                    operation_deadline_seconds=5.0,
                    clock=lambda current_now=now: current_now[0],
                )
                old_id: str | None = None
                if redirect:
                    old_id = current_attempt(engine.enqueue_snapshot("role-a", "old"))["id"]
                    adapter.calls.clear()
                if disposition == "not_sent":
                    adapter.before_name = lambda current_now=now: current_now.__setitem__(0, 6.0)
                failed = [False]
                real_atomic_json = engine_module._atomic_json

                def fail_exact_closure(
                    path: Path,
                    value: dict[str, Any],
                    *,
                    prior_atomic_json=real_atomic_json,
                    prior_attempt_id=old_id,
                    expected_error=closure_error,
                    failure_flag=failed,
                ) -> None:
                    attempt_id = value["roles"]["role-a"].get("current_attempt_id")
                    attempts = value.get("attempts") or []
                    attempt = next(
                        (item for item in attempts if item.get("id") == attempt_id),
                        None,
                    )
                    if attempt is None:
                        prior_atomic_json(path, value)
                        return
                    is_replacement = prior_attempt_id is None or attempt["id"] != prior_attempt_id
                    exact_waiting = (
                        attempt.get("thread_id") == "thread-1" and attempt.get("status") == "waiting"
                    )
                    if (
                        is_replacement
                        and exact_waiting
                        and attempt.get("error") == expected_error
                        and not failure_flag[0]
                    ):
                        failure_flag[0] = True
                        raise OSError("injected closure write failure")
                    prior_atomic_json(path, value)

                with (
                    patch("switchstand.engine._atomic_json", side_effect=fail_exact_closure),
                    self.assertRaises(PersistenceUnavailable),
                ):
                    if redirect:
                        assert old_id is not None
                        engine.redirect_snapshot(old_id, disposition)
                    else:
                        engine.enqueue_snapshot("role-a", disposition)
                self.assertTrue(failed[0])
                self.assertTrue(engine.persistence_failed)
                restarted = Engine(state_path, ScriptedAdapter()).snapshot()
                attempt = current_attempt(restarted)
                self.assertEqual((attempt["thread_id"], attempt["status"]), ("thread-1", "waiting"))
                self.assertEqual(attempt["error"], "thread_name_pending")
                self.assertEqual(adapter.calls.count("create"), 1)
                self.assertEqual(adapter.calls.count("name"), disposition != "not_sent")
                self.assertEqual(adapter.calls.count("interrupt"), int(redirect))


if __name__ == "__main__":
    unittest.main()
