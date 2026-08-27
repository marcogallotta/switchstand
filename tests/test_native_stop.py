from __future__ import annotations

import json
import threading
import unittest

from switchstand.native_stop import NativeStop, _project


SENTINEL = "PRIVATE-TRANSCRIPT-SENTINEL"


def read_result(turn: str = "turn-1", status: str = "inProgress", *, thread="thread-1"):
    return {
        "thread": {
            "id": thread,
            "status": {"type": "active" if status == "inProgress" else "idle"},
            "turns": [{"id": turn, "status": status, "items": [{"text": SENTINEL}]}],
            "preview": SENTINEL,
        }
    }


class Replies:
    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls = []
        self.lock = threading.Lock()

    def __call__(self):
        owner = self

        class Client:
            def stop_request(self, method, params, **_limits):
                with owner.lock:
                    owner.calls.append((method, dict(params)))
                    value = owner.replies.pop(0)
                if isinstance(value, Exception):
                    raise value
                return value

        return Client()


class Clock:
    value = 10.0

    def __call__(self):
        return self.value


class NativeStopTests(unittest.TestCase):
    def prepared(self, replies, **kwargs):
        stop = NativeStop(replies, lambda ref: "thread-1" if ref == "agent-1" else None, **kwargs)
        result = stop.prepare("agent-1")
        self.assertEqual(result["code"], "prepared")
        return stop, result["confirmationRef"]

    def test_exact_projection_rejects_coercion_duplicates_and_inconsistent_state(self):
        valid = read_result()
        projected = _project(valid, "thread-1")
        self.assertIsNotNone(projected)
        self.assertEqual(projected[1] if projected else None, {"turn-1": "inProgress"})
        mutations = []
        for value in (1, "", "x" * 257):
            case = read_result()
            case["thread"]["turns"][0]["id"] = value
            mutations.append(case)
        case = read_result()
        case["thread"]["id"] = 1
        mutations.append(case)
        case = read_result()
        case["thread"]["status"] = {"type": "busy"}
        mutations.append(case)
        case = read_result()
        case["thread"]["turns"].append({"id": "turn-2", "status": "inProgress"})
        mutations.append(case)
        case = read_result(status="completed")
        case["thread"]["status"] = {"type": "active"}
        mutations.append(case)
        case = read_result()
        case["thread"]["turns"].append({"id": "turn-1", "status": "completed"})
        mutations.append(case)
        case = read_result()
        del case["thread"]["turns"]
        mutations.append(case)
        case = read_result()
        case["thread"]["turns"][0]["status"] = "cancelled"
        mutations.append(case)
        case = read_result()
        case["thread"]["turns"] *= 257
        mutations.append(case)
        self.assertTrue(all(_project(case, "thread-1") is None for case in mutations))

    def test_prepare_commit_and_later_status_keep_content_out_of_all_retained_surfaces(self):
        replies = Replies(
            ("ok", read_result()),
            ("ok", read_result()),
            ("ok", {}),
            ("ok", read_result(status="interrupted")),
        )
        stop, reference = self.prepared(replies)
        requested = stop.commit(reference)
        confirmed = stop.status(requested["operationRef"])

        self.assertEqual(requested["outcome"], "requested")
        self.assertEqual(confirmed["outcome"], "confirmed")
        self.assertEqual(
            [method for method, _ in replies.calls],
            ["thread/read", "thread/read", "turn/interrupt", "thread/read"],
        )
        self.assertEqual(replies.calls[2][1], {"threadId": "thread-1", "turnId": "turn-1"})
        retained = json.dumps([requested, confirmed, replies.calls]) + repr(stop._receipts)
        self.assertNotIn(SENTINEL, retained)

    def test_old_to_new_turn_race_is_not_sent_and_never_retargets(self):
        replies = Replies(("ok", read_result()), ("ok", read_result("turn-2")))
        stop, reference = self.prepared(replies)

        result = stop.commit(reference)

        self.assertEqual(result["outcome"], "not_sent")
        self.assertNotIn("turn/interrupt", [method for method, _ in replies.calls])

    def test_later_exact_evidence_maps_every_terminal_and_active_outcome(self):
        for status, expected in (("inProgress", "requested"), ("completed", "not_confirmed"),
                ("failed", "not_confirmed"), ("interrupted", "confirmed")):
            with self.subTest(status=status):
                replies = Replies(("ok", read_result()), ("ok", read_result()), ("ok", {}),
                    ("ok", read_result(status=status)))
                stop, reference = self.prepared(replies)
                operation = stop.commit(reference)["operationRef"]
                self.assertEqual(stop.status(operation)["outcome"], expected)
        replies = Replies(("ok", read_result()), ("ok", read_result()), ("ok", {}),
            ("malformed", None))
        stop, reference = self.prepared(replies)
        operation = stop.commit(reference)["operationRef"]
        self.assertEqual(stop.status(operation)["outcome"], "unknown")

    def test_concurrent_double_commit_consumes_before_io(self):
        entered = threading.Event()
        release = threading.Event()
        replies = Replies(("ok", read_result()))
        stop, reference = self.prepared(replies)
        original = stop._read

        def blocked(thread_id):
            entered.set()
            release.wait(2)
            return original(thread_id)

        stop._read = blocked
        replies.replies.extend([("ok", read_result()), ("ok", {})])
        results = []
        first = threading.Thread(target=lambda: results.append(stop.commit(reference)))
        first.start()
        self.assertTrue(entered.wait(1))
        second = threading.Thread(target=lambda: results.append(stop.commit(reference)))
        second.start()
        second.join(1)
        release.set()
        first.join(2)

        self.assertEqual(sorted(value["outcome"] for value in results), ["not_sent", "requested"])
        self.assertEqual([method for method, _ in replies.calls].count("turn/interrupt"), 1)

    def test_acknowledgement_classifications_are_truthful_and_never_retried(self):
        expected = {
            "rejected": "rejected",
            "ambiguous": "unknown",
            "malformed": "unknown",
            "oversize": "unknown",
        }
        for classification, outcome in expected.items():
            with self.subTest(classification=classification):
                replies = Replies(
                    ("ok", read_result()), ("ok", read_result()), (classification, None)
                )
                stop, reference = self.prepared(replies)
                self.assertEqual(stop.commit(reference)["outcome"], outcome)
                self.assertEqual([method for method, _ in replies.calls].count("turn/interrupt"), 1)
        replies = Replies(("ok", read_result()), ("ok", read_result()), ("ok", {"extra": True}))
        stop, reference = self.prepared(replies)
        self.assertEqual(stop.commit(reference)["outcome"], "unknown")
        replies = Replies(("ok", read_result()), ("ok", read_result()))
        def fails_before_interrupt():
            if len(replies.calls) == 2:
                raise OSError(SENTINEL)
            return replies()
        stop, reference = self.prepared(fails_before_interrupt)
        self.assertEqual(stop.commit(reference)["outcome"], "not_sent")

    def test_expiry_capacity_missing_target_and_exception_are_fixed_safe_failures(self):
        clock = Clock()
        replies = Replies(("ok", read_result()), RuntimeError(SENTINEL))
        stop, reference = self.prepared(replies, clock=clock, ttl_seconds=1, capacity=1)
        self.assertEqual(stop.prepare("agent-1"), {"code": "stop_capacity", "outcome": "not_sent"})
        clock.value = 12
        result = stop.commit(reference)
        self.assertEqual(result, {"code": "confirmation_unavailable", "outcome": "not_sent"})
        self.assertEqual(stop.prepare("agent-1"), {"code": "target_unavailable", "outcome": "not_sent"})
        self.assertNotIn(SENTINEL, json.dumps(result) + repr(stop._receipts))


if __name__ == "__main__":
    unittest.main()
