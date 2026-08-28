from __future__ import annotations

import threading
from typing import Any, Mapping
import unittest

from switchstand.current_target import ExactCurrentTarget
from switchstand.native_board import NativeBoard
from switchstand.native_contracts import NativeSelectionPair


def record(thread_id: str, *, parent: str | None, updated: float) -> dict[str, Any]:
    return {
        "id": thread_id,
        "sessionId": f"session-{thread_id}",
        "parentThreadId": parent,
        "source": "cli" if parent is None else {"subAgent": "review"},
        "createdAt": 90.0,
        "updatedAt": updated,
        "status": {"type": "active", "activeFlags": []},
    }


class TreeClient:
    def __init__(self, child_id: str, updated: float) -> None:
        self.root = record("raw-root", parent=None, updated=updated)
        self.child = record(child_id, parent="raw-root", updated=updated)

    def thread_read(
        self, thread_id: str, *, include_turns: bool = True
    ) -> Mapping[str, Any]:
        return {"thread": self.root}

    def thread_list(self, params: Mapping[str, Any]) -> Mapping[str, Any]:
        return {"data": [self.child], "nextCursor": None}

    def stop_request(
        self,
        method: str,
        params: Mapping[str, Any],
        *,
        max_response_bytes: int = 256 * 1024,
        timeout_seconds: float = 3.0,
        _close_after: bool = True,
    ) -> tuple[str, Mapping[str, Any] | None]:
        return "ok", {"data": [], "nextCursor": None}

    def close(self) -> None:
        pass


class GatedTurnProbeClient(TreeClient):
    def __init__(
        self,
        child_id: str,
        updated: float,
        entered: threading.Event,
        release: threading.Event,
    ) -> None:
        super().__init__(child_id, updated)
        self.entered = entered
        self.release = release

    def stop_request(
        self,
        method: str,
        params: Mapping[str, Any],
        *,
        max_response_bytes: int = 256 * 1024,
        timeout_seconds: float = 3.0,
        _close_after: bool = True,
    ) -> tuple[str, Mapping[str, Any] | None]:
        self.entered.set()
        if not self.release.wait(2.0):
            raise TimeoutError("test turn-probe gate was not released")
        return super().stop_request(
            method,
            params,
            max_response_bytes=max_response_bytes,
            timeout_seconds=timeout_seconds,
            _close_after=_close_after,
        )


class SequenceFactory:
    def __init__(self, clients: list[TreeClient]) -> None:
        self.clients = clients

    def __call__(self) -> TreeClient:
        return self.clients.pop(0)


class MutableClock:
    value = 100.0

    def __call__(self) -> float:
        return self.value


def pair_from(board: NativeBoard, agent_ref: Any, now: float) -> NativeSelectionPair:
    result = board.browser_selection(
        agent_ref, now=now, maximum_observation_age_seconds=100.0
    )
    if result["selection"] is None:
        raise AssertionError("expected selection pair")
    return result["selection"]


def error_code(result: object) -> str:
    if not isinstance(result, dict) or "code" not in result:
        raise AssertionError("expected safe selection error")
    return str(result["code"])


class NativeBoundedStateTests(unittest.TestCase):
    def test_overlapping_polls_cannot_fork_the_issuance_watermark(self):
        first_entered, first_release = threading.Event(), threading.Event()
        second_entered, second_release = threading.Event(), threading.Event()
        factory = SequenceFactory([
            TreeClient("raw-seed", 100.0),
            GatedTurnProbeClient(
                "raw-child-a", 101.0, first_entered, first_release
            ),
            GatedTurnProbeClient(
                "raw-child-b", 102.0, second_entered, second_release
            ),
        ])
        board = NativeBoard(factory, "raw-root", maximum_observation_age_seconds=100.0)
        board.poll_once()

        first = threading.Thread(target=board.poll_once)
        second_attempted = threading.Event()

        def second_poll() -> None:
            second_attempted.set()
            board.poll_once()

        second = threading.Thread(target=second_poll)
        first.start()
        self.assertTrue(first_entered.wait(1.0))
        second.start()
        self.assertTrue(second_attempted.wait(1.0))
        self.assertFalse(second_entered.wait(0.2))

        first_release.set()
        first.join(1.0)
        self.assertFalse(first.is_alive())
        self.assertTrue(second_entered.wait(1.0))
        old_pair = pair_from(board, "agent-3", board._wall_clock())
        old_target = board.resolve_current_target(
            old_pair,
            now=board._wall_clock(),
            maximum_observation_age_seconds=100.0,
        )
        self.assertIsInstance(old_target, ExactCurrentTarget)

        second_release.set()
        second.join(1.0)
        self.assertFalse(second.is_alive())
        self.assertEqual(
            [agent["agentRef"] for agent in board.snapshot()["agents"]],
            ["agent-1", "agent-4"],
        )
        self.assertEqual(error_code(board.resolve_current_target(
            old_pair,
            now=board._wall_clock(),
            maximum_observation_age_seconds=100.0,
        )), "AGENT_NOT_PRESENT")
        self.assertIsNone(board._with_current_native_target(old_target, lambda value: value))

    def test_high_churn_bounds_maps_and_preserves_issued_ref_semantics(self):
        clock = MutableClock()
        clients = [TreeClient("raw-child-old", 100.0)] + [
            TreeClient(f"raw-child-{number}", 100.0 + number)
            for number in range(1, 15)
        ] + [TreeClient("raw-child-old", 115.0)]
        board = NativeBoard(
            SequenceFactory(clients),
            "raw-root",
            wall_clock=clock,
            monotonic=clock,
            trail_limit=2,
            maximum_observation_age_seconds=100.0,
        )

        board.poll_once()
        old_pair = pair_from(board, "agent-2", 100.0)
        old_target = board.resolve_current_target(
            old_pair, now=100.0, maximum_observation_age_seconds=100.0
        )
        self.assertIsInstance(old_target, ExactCurrentTarget)
        stable_root_target = board._target_identities["raw-root"]

        for number in range(1, 15):
            clock.value = 100.0 + number
            board.poll_once()
            self.assertEqual(len(board._target_identities), 2)
            self.assertEqual(len(board._native_ids_by_target), 2)
            self.assertEqual(len(board._target_records), 2)
            self.assertEqual(board._target_identities["raw-root"], stable_root_target)

        self.assertNotIn(
            "agent-2", {entry["agentRef"] for entry in board.snapshot()["trail"]}
        )
        self.assertEqual(error_code(board.resolve_current_target(
            old_pair, now=114.0, maximum_observation_age_seconds=100.0
        )), "AGENT_NOT_PRESENT")
        for never_issued in (
            "agent-99", "agent-18", "agent-02", "agent-" + "9" * 100_000,
            "raw-child-old", [],
        ):
            snapshot = board.browser_selection(
                never_issued, now=114.0, maximum_observation_age_seconds=100.0
            )["snapshot"]
            self.assertEqual(error_code(snapshot), "INVALID_AGENT_REF")

        clock.value = 115.0
        board.poll_once()
        new_target = board.resolve_current_target(
            pair_from(board, "agent-17", 115.0),
            now=115.0,
            maximum_observation_age_seconds=100.0,
        )
        self.assertIsInstance(new_target, ExactCurrentTarget)
        self.assertNotEqual(new_target, old_target)
        self.assertEqual(error_code(board.resolve_current_target(
            old_pair, now=115.0, maximum_observation_age_seconds=100.0
        )), "AGENT_NOT_PRESENT")
        self.assertIsNone(board._with_current_native_target(old_target, lambda value: value))
        self.assertEqual(len(board._target_identities), 2)
        self.assertEqual(len(board._native_ids_by_target), 2)


if __name__ == "__main__":
    unittest.main()
