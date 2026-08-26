from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from switchstand.engine import CodexAdapter, Engine, _message_marker, _submitted_message_text


class FakeAdapter:
    def __init__(self) -> None:
        self.thread_count = 0
        self.turn_count = 0
        self.deliveries: list[tuple[str, str, int, str]] = []
        self.turns: dict[str, dict[str, object]] = {}
        self.messages: dict[tuple[str, str], str] = {}
        self.interrupts: list[tuple[str, str]] = []
        self.contexts: list[dict[str, object]] = []
        self.fail_create = False
        self.fail_start_once = False
        self.thread_status = "idle"
        self.start_calls = 0

    def create_attempt(self, *, role, context):
        if self.fail_create:
            raise RuntimeError("adapter unavailable")
        self.thread_count += 1
        self.contexts.append(context)
        return f"thread-{self.thread_count}"

    def start_message(self, *, thread_id, message):
        self.start_calls += 1
        if self.fail_start_once:
            self.fail_start_once = False
            raise RuntimeError("turn start acknowledgement unavailable")
        self.turn_count += 1
        turn_id = f"turn-{self.turn_count}"
        self.deliveries.append((thread_id, message["id"], message["sequence"], message["text"]))
        self.turns[turn_id] = {"status": "inProgress", "output": None}
        self.messages[(thread_id, message["id"])] = turn_id
        return turn_id

    def interrupt(self, *, thread_id, turn_id):
        self.interrupts.append((thread_id, turn_id))

    def inspect_turn(self, *, thread_id, turn_id):
        return dict(self.turns[turn_id])

    def inspect_message(self, *, thread_id, message_id):
        turn_id = self.messages.get((thread_id, message_id))
        if not turn_id:
            return {
                "found": False,
                "absence_proven": self.thread_status == "idle",
                "thread_status": self.thread_status,
            }
        return {"found": True, "turn_id": turn_id, **self.turns[turn_id]}

    def finish(self, turn_id, output, *, status="completed"):
        self.turns[turn_id] = {"status": status, "output": output}


class OfficialHistoryAdapter(CodexAdapter):
    """Exercise real marker inspection against documented app-server item shapes."""

    def __init__(self, *, status="idle", accept_before_lost_ack=False, incomplete=False):
        self.status = status
        self.accept_before_lost_ack = accept_before_lost_ack
        self.incomplete = incomplete
        self.start_calls = 0
        self.submissions = []
        self.turns = []

    def create_attempt(self, *, role, context):
        return "thread-official"

    def start_message(self, *, thread_id, message):
        self.start_calls += 1
        submitted = _submitted_message_text(message)
        self.submissions.append(submitted)
        if self.start_calls == 1:
            if self.accept_before_lost_ack:
                marker = _message_marker(message["id"])
                self.turns.append(
                    {
                        "id": "turn-accepted",
                        "status": "completed",
                        "items": [
                            {
                                "type": "userMessage",
                                "id": "user-1",
                                "content": [{"type": "text", "text": submitted}],
                            },
                            {
                                "type": "agentMessage",
                                "id": "agent-1",
                                "phase": "final_answer",
                                "text": f"accepted result {marker}",
                            },
                        ],
                    }
                )
            raise RuntimeError("turn start acknowledgement unavailable")
        turn_id = f"turn-{self.start_calls}"
        self.turns.append(
            {
                "id": turn_id,
                "status": "inProgress",
                "items": [
                    {
                        "type": "userMessage",
                        "id": f"user-{self.start_calls}",
                        "content": [{"type": "text", "text": submitted}],
                    }
                ],
            }
        )
        return turn_id

    def interrupt(self, *, thread_id, turn_id):
        raise AssertionError("not used by marker recovery tests")

    def _read(self, thread_id):
        turns = [{"id": "incomplete"}] if self.incomplete else list(self.turns)
        return {"thread": {"id": thread_id, "status": {"type": self.status}, "turns": turns}}


def role(state, role_id):
    return state["roles"][role_id]


def current_attempt(state, role_id):
    attempt_id = role(state, role_id)["current_attempt_id"]
    return next(item for item in state["attempts"] if item["id"] == attempt_id)


class EngineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_checkpoint_order_fencing_and_restart_restoration(self):
        adapter = FakeAdapter()
        state_path = self.root / "state.json"
        engine = Engine(state_path, adapter, role_names=("Design", "Review"))

        first_a = engine.enqueue("role-a", "Plan the narrow slice")
        first_b = engine.enqueue("role-b", "Challenge the state seam")
        correction = engine.enqueue("role-a", "Correction: preserve direct access", kind="correction")
        before_stop = engine.snapshot()
        old_a = current_attempt(before_stop, "role-a")
        b_attempt = current_attempt(before_stop, "role-b")
        self.assertEqual(role(before_stop, "role-a")["status"], "busy")
        self.assertEqual(role(before_stop, "role-a")["queued_count"], 1)
        self.assertEqual([item[1] for item in adapter.deliveries], [first_a["id"], first_b["id"]])

        engine.stop(old_a["id"])
        replacement_id = engine.replace(old_a["id"])
        after_replace = engine.snapshot()
        replacement = current_attempt(after_replace, "role-a")
        self.assertEqual(replacement["id"], replacement_id)
        self.assertNotEqual(replacement["generation"], old_a["generation"])
        self.assertEqual(
            [item[1] for item in adapter.deliveries],
            [first_a["id"], first_b["id"], correction["id"]],
        )
        context = adapter.contexts[-1]
        self.assertEqual([item["id"] for item in context["accepted_messages"]], [first_a["id"], correction["id"]])

        adapter.finish(old_a["turn_id"], "obsolete pre-stop output")
        adapter.finish(b_attempt["turn_id"], "role B accepted result")
        adapter.finish(replacement["turn_id"], "corrected role A result")
        engine.reconcile()

        accepted = engine.snapshot()
        old_record = next(item for item in accepted["attempts"] if item["id"] == old_a["id"])
        self.assertEqual(old_record["status"], "stale")
        self.assertEqual(old_record["stale_output"], "obsolete pre-stop output")
        self.assertEqual(role(accepted, "role-a")["checkpoint"]["latest_result"], "corrected role A result")
        self.assertEqual(role(accepted, "role-b")["checkpoint"]["latest_result"], "role B accepted result")

        restarted = Engine(state_path, adapter)
        restarted.reconcile()
        restored = restarted.snapshot()
        self.assertEqual(role(restored, "role-a")["checkpoint"], role(accepted, "role-a")["checkpoint"])
        self.assertEqual(len(adapter.deliveries), 3)
        self.assertIn((old_a["thread_id"], old_a["turn_id"]), adapter.interrupts)

    def test_redirect_is_exact_correction_stop_and_replace(self):
        adapter = FakeAdapter()
        engine = Engine(self.root / "state.json", adapter)
        engine.enqueue("role-a", "initial direction")
        old = current_attempt(engine.snapshot(), "role-a")

        replacement_id = engine.redirect(old["id"], "correct the direction")
        state = engine.snapshot()
        replacement = current_attempt(state, "role-a")
        self.assertEqual(replacement["id"], replacement_id)
        self.assertTrue(next(item for item in state["attempts"] if item["id"] == old["id"])["fence_closed"])
        self.assertEqual(role(state, "role-a")["checkpoint"]["latest_correction"], "correct the direction")
        self.assertEqual([item[3] for item in adapter.deliveries], ["initial direction", "correct the direction"])

    def test_invalid_redirect_targets_leave_all_state_and_adapter_calls_unchanged(self):
        adapter = FakeAdapter()
        state_path = self.root / "state.json"
        engine = Engine(state_path, adapter)
        engine.enqueue("role-a", "initial direction")
        old = current_attempt(engine.snapshot(), "role-a")
        engine.stop(old["id"])

        def assert_redirect_has_no_effect(attempt_id, expected_error):
            before = engine.snapshot()
            state_bytes = state_path.read_bytes()
            event_bytes = engine.events_path.read_bytes()
            deliveries = list(adapter.deliveries)
            interrupts = list(adapter.interrupts)
            contexts = list(adapter.contexts)
            with self.assertRaisesRegex(ValueError, expected_error):
                engine.redirect(attempt_id, "must not be recorded")
            after = engine.snapshot()
            self.assertEqual(after, before)
            self.assertEqual(after["messages"], before["messages"])
            self.assertEqual(after["attempts"], before["attempts"])
            self.assertEqual(after["roles"]["role-a"]["checkpoint"], before["roles"]["role-a"]["checkpoint"])
            self.assertEqual(state_path.read_bytes(), state_bytes)
            self.assertEqual(engine.events_path.read_bytes(), event_bytes)
            self.assertEqual(adapter.deliveries, deliveries)
            self.assertEqual(adapter.interrupts, interrupts)
            self.assertEqual(adapter.contexts, contexts)

        assert_redirect_has_no_effect(old["id"], "not stoppable")
        engine.replace(old["id"])
        assert_redirect_has_no_effect(old["id"], "not the role's selected current attempt")

    def test_adapter_failure_is_unknown_not_success(self):
        adapter = FakeAdapter()
        adapter.fail_create = True
        engine = Engine(self.root / "state.json", adapter)
        engine.enqueue("role-a", "message with unavailable adapter")
        state = engine.snapshot()
        self.assertEqual(current_attempt(state, "role-a")["status"], "unknown")
        self.assertEqual(role(state, "role-a")["status"], "unknown")
        self.assertEqual(state["messages"][0]["status"], "queued")

    def test_restart_reconciles_interrupted_dispatch_without_replay(self):
        adapter = FakeAdapter()
        state_path = self.root / "state.json"
        engine = Engine(state_path, adapter)
        message = engine.enqueue("role-a", "accepted before process exit")
        value = json.loads(state_path.read_text(encoding="utf-8"))
        value["messages"][0]["status"] = "dispatching"
        value["attempts"][0]["status"] = "waiting"
        state_path.write_text(json.dumps(value), encoding="utf-8")

        restarted = Engine(state_path, adapter)
        self.assertEqual(restarted.snapshot()["messages"][0]["status"], "unknown")
        restarted.reconcile()
        restored = restarted.snapshot()
        self.assertEqual(restored["messages"][0]["status"], "delivered")
        self.assertEqual(restored["messages"][0]["id"], message["id"])
        self.assertEqual(len(adapter.deliveries), 1)

    def test_official_history_marker_recovers_completed_turn_without_replay(self):
        adapter = OfficialHistoryAdapter(status="idle", accept_before_lost_ack=True)
        engine = Engine(self.root / "state.json", adapter)
        message = engine.enqueue("role-a", "operator text stays clean")
        self.assertEqual(engine.snapshot()["messages"][0]["status"], "unknown")

        engine.reconcile()
        engine.reconcile()
        state = engine.snapshot()

        self.assertEqual(adapter.start_calls, 1)
        self.assertEqual(state["messages"][0]["text"], "operator text stays clean")
        self.assertEqual(state["messages"][0]["status"], "completed")
        self.assertEqual(state["messages"][0]["result"], "accepted result")
        self.assertEqual(role(state, "role-a")["checkpoint"]["latest_result"], "accepted result")
        self.assertIn(_message_marker(message["id"]), adapter.submissions[0])
        self.assertNotIn(_message_marker(message["id"]), state["messages"][0]["result"])

    def test_official_history_ambiguous_absence_never_replays(self):
        for status in ("active", "notLoaded", "unknown"):
            with self.subTest(status=status):
                adapter = OfficialHistoryAdapter(status=status)
                engine = Engine(self.root / status / "state.json", adapter)
                engine.enqueue("role-a", "ambiguous accepted turn")
                engine.reconcile()
                engine.reconcile()
                self.assertEqual(engine.snapshot()["messages"][0]["status"], "unknown")
                self.assertEqual(adapter.start_calls, 1)

    def test_official_idle_complete_history_marker_absence_retries_once(self):
        adapter = OfficialHistoryAdapter(status="idle")
        engine = Engine(self.root / "state.json", adapter)
        message = engine.enqueue("role-a", "turn proven absent")
        engine.reconcile()
        engine.reconcile()
        self.assertEqual(adapter.start_calls, 2)
        self.assertEqual(engine.snapshot()["messages"][0]["status"], "delivered")
        self.assertIn(_message_marker(message["id"]), adapter.submissions[-1])

    def test_idle_incomplete_history_never_proves_marker_absence(self):
        adapter = OfficialHistoryAdapter(status="idle", incomplete=True)
        engine = Engine(self.root / "state.json", adapter)
        engine.enqueue("role-a", "history is incomplete")
        engine.reconcile()
        engine.reconcile()
        self.assertEqual(adapter.start_calls, 1)
        self.assertEqual(engine.snapshot()["messages"][0]["status"], "unknown")

    def test_sequential_messages_share_one_attempt_and_preserve_fifo(self):
        adapter = FakeAdapter()
        engine = Engine(self.root / "state.json", adapter)
        first = engine.enqueue("role-a", "first turn")
        second = engine.enqueue("role-a", "second turn")
        initial = engine.snapshot()
        attempt_id = role(initial, "role-a")["current_attempt_id"]
        first_turn = current_attempt(initial, "role-a")["turn_id"]

        adapter.finish(first_turn, "first result")
        engine.reconcile()
        current = current_attempt(engine.snapshot(), "role-a")
        self.assertEqual(current["id"], attempt_id)
        self.assertNotEqual(current["turn_id"], first_turn)
        adapter.finish(current["turn_id"], "second result")
        engine.reconcile()
        final = engine.snapshot()
        self.assertEqual(role(final, "role-a")["checkpoint"]["accepted_message_ids"], [first["id"], second["id"]])
        self.assertEqual([item[1] for item in adapter.deliveries], [first["id"], second["id"]])


if __name__ == "__main__":
    unittest.main()
