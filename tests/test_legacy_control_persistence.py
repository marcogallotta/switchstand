from __future__ import annotations

from pathlib import Path
import stat
import tempfile
from typing import Any, cast, Mapping
import unittest
from unittest.mock import patch

from switchstand.engine import Engine
from switchstand.legacy_deadline import PersistenceUnavailable, PhaseDisposition, PhaseResult
from tests.test_legacy_deadline import ScriptedAdapter, current_attempt


class LegacyControlAndPersistenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_redirect_preparation_is_atomic_and_cutoff_finalizes_once(self) -> None:
        now = [0.0]
        adapter = ScriptedAdapter()
        engine = Engine(
            self.root / "redirect-cutoff.json",
            adapter,
            operation_deadline_seconds=5.0,
            clock=lambda: now[0],
        )
        engine.enqueue("role-a", "old target")
        old = current_attempt(engine.state)
        adapter.interrupt_result = PhaseResult("ambiguous", "turn/interrupt", code="acknowledgement_unavailable")
        adapter.on_interrupt = lambda: now.__setitem__(0, 6.0)
        state = engine.redirect_snapshot(old["id"], "correction")
        replacement = current_attempt(state)
        old_record = next(item for item in state["attempts"] if item["id"] == old["id"])
        self.assertTrue(old_record["fence_closed"])
        self.assertEqual(old_record["status"], "unknown")
        self.assertEqual(replacement["status"], "failed")
        self.assertEqual(replacement["error"], "thread_start_not_sent/setup_cutoff")
        self.assertEqual(state["roles"]["role-a"]["generation"], 2)
        self.assertEqual(state["messages"][-1]["status"], "queued")
        self.assertEqual(adapter.calls.count("interrupt"), 1)
        self.assertEqual(adapter.calls.count("create"), 1)
        events = [__import__("json").loads(line)["event"] for line in engine.events_path.read_text().splitlines()]
        self.assertEqual(events.count("redirect_cutoff_finalized"), 1)
        self.assertNotIn("redirect_interrupt_closed", events)

    def test_redirect_closes_each_interrupt_replacement_and_delivery_partial_exactly_once(self) -> None:
        interrupt_cases = (
            ("not_sent", "interrupt_not_sent/setup_cutoff"),
            ("rejected", "interrupt_rejected"),
            ("ambiguous", "interrupt_ambiguous"),
            ("acknowledged", None),
        )
        for disposition, expected_error in interrupt_cases:
            with self.subTest(phase="interrupt", disposition=disposition):
                adapter = ScriptedAdapter()
                engine = Engine(self.root / f"redirect-interrupt-{disposition}.json", adapter)
                engine.enqueue("role-a", "old")
                old = current_attempt(engine.state)
                adapter.interrupt_result = PhaseResult(
                    cast(PhaseDisposition, disposition),
                    "turn/interrupt",
                    {} if disposition == "acknowledged" else None,
                    "setup_cutoff" if disposition == "not_sent" else None,
                )
                adapter.create = PhaseResult(
                    "acknowledged", "thread/start", {"thread": {"id": "thread-2"}}
                )
                state = engine.redirect_snapshot(old["id"], "correction")
                old_record = next(item for item in state["attempts"] if item["id"] == old["id"])
                self.assertEqual(old_record["error"], expected_error)
                self.assertEqual(adapter.calls.count("interrupt"), 1)
                self.assertEqual(state["roles"]["role-a"]["generation"], 2)
                self.assertEqual(adapter.start_threads[-1], "thread-2")

        for disposition, expected_status, expected_error in (
            ("not_sent", "failed", "thread_start_not_sent/setup_cutoff"),
            ("rejected", "failed", "thread_start_rejected"),
            ("ambiguous", "unknown", "thread_start_ambiguous"),
        ):
            with self.subTest(phase="replacement", disposition=disposition):
                adapter = ScriptedAdapter()
                engine = Engine(self.root / f"redirect-replacement-{disposition}.json", adapter)
                engine.enqueue("role-a", "old")
                old = current_attempt(engine.state)
                adapter.create = PhaseResult(
                    cast(PhaseDisposition, disposition),
                    "thread/start",
                    None,
                    "setup_cutoff" if disposition == "not_sent" else None,
                )
                state = engine.redirect_snapshot(old["id"], "correction")
                replacement = current_attempt(state)
                self.assertEqual(replacement["status"], expected_status)
                self.assertEqual(replacement["error"], expected_error)
                self.assertEqual(adapter.calls.count("interrupt"), 1)
                self.assertEqual(adapter.calls.count("create"), 2)
                self.assertEqual(adapter.calls.count("start"), 1)

        for disposition, expected_status, expected_message in (
            ("not_sent", "waiting", "queued"),
            ("rejected", "failed", "queued"),
            ("ambiguous", "unknown", "unknown"),
            ("acknowledged", "running", "delivered"),
        ):
            with self.subTest(phase="delivery", disposition=disposition):
                adapter = ScriptedAdapter()
                engine = Engine(self.root / f"redirect-delivery-{disposition}.json", adapter)
                engine.enqueue("role-a", "old")
                old = current_attempt(engine.state)
                adapter.create = PhaseResult(
                    "acknowledged", "thread/start", {"thread": {"id": "thread-2"}}
                )
                adapter.start = PhaseResult(
                    cast(PhaseDisposition, disposition),
                    "turn/start",
                    {"turn": {"id": "turn-2"}} if disposition == "acknowledged" else None,
                    "setup_cutoff" if disposition == "not_sent" else None,
                )
                state = engine.redirect_snapshot(old["id"], "correction")
                replacement = current_attempt(state)
                correction = state["messages"][-1]
                self.assertEqual(replacement["status"], expected_status)
                self.assertEqual(correction["status"], expected_message)
                self.assertEqual(correction["attempt_id"], replacement["id"])
                self.assertEqual(adapter.start_threads[-1], "thread-2")
                self.assertEqual(state["roles"]["role-a"]["generation"], 2)

    def test_intent_save_overrun_never_admits_the_following_external_phase(self) -> None:
        for event, forbidden in (
            ("attempt_starting", "create"),
            ("message_dispatching", "start"),
            ("attempt_stop_requested", "interrupt"),
        ):
            with self.subTest(event=event):
                now = [0.0]
                adapter = ScriptedAdapter()
                engine = Engine(
                    self.root / f"overrun-{event}.json",
                    adapter,
                    operation_deadline_seconds=5.0,
                    clock=lambda now=now: now[0],
                )
                if event == "attempt_stop_requested":
                    engine.enqueue("role-a", "running")
                    adapter.calls.clear()
                original_save = engine._save

                def overrun_save(
                    event: str,
                    *,
                    original_save=original_save,
                    watched_event=event,
                    now=now,
                    **values: Any,
                ) -> None:
                    original_save(event, **values)
                    if event == watched_event:
                        now[0] = 6.0

                engine._save = overrun_save
                if event == "attempt_stop_requested":
                    engine.stop(current_attempt(engine.state)["id"])
                else:
                    engine.enqueue("role-b", "accepted")
                self.assertNotIn(forbidden, adapter.calls)

    def test_persistence_failure_latches_and_blocks_snapshot_and_adapter(self) -> None:
        adapter = ScriptedAdapter()
        state_path = self.root / "latch.json"
        engine = Engine(state_path, adapter)
        with patch("switchstand.engine._append_private_json", side_effect=OSError("event failed")):
            with self.assertRaises(PersistenceUnavailable):
                engine.enqueue("role-a", "intent reaches authoritative snapshot")
        calls = list(adapter.calls)
        with self.assertRaises(PersistenceUnavailable):
            engine.reconcile()
        with self.assertRaises(PersistenceUnavailable):
            engine.snapshot()
        self.assertEqual(adapter.calls, calls)
        restarted = Engine(state_path, ScriptedAdapter())
        self.assertEqual(restarted.snapshot()["messages"][0]["status"], "queued")

    def test_each_snapshot_persistence_boundary_latches_before_adapter(self) -> None:
        cases = ("file_fsync", "replace", "directory_fsync")
        for case in cases:
            with self.subTest(case=case):
                adapter = ScriptedAdapter()
                engine = Engine(self.root / f"{case}.json", adapter)
                real_fsync = __import__("os").fsync

                def fail_fsync(fd: int, case: str = case, real_fsync=real_fsync) -> None:
                    is_directory = stat.S_ISDIR(__import__("os").fstat(fd).st_mode)
                    if (case == "directory_fsync") == is_directory:
                        raise OSError(case)
                    real_fsync(fd)

                target = (
                    patch("switchstand.legacy_persistence.os.replace", side_effect=OSError(case))
                    if case == "replace"
                    else patch("switchstand.legacy_persistence.os.fsync", side_effect=fail_fsync)
                )
                with target, self.assertRaises(PersistenceUnavailable):
                    engine.enqueue("role-a", "must latch")
                self.assertTrue(engine.persistence_failed)
                self.assertEqual(adapter.calls, [])

    def test_each_slow_persistence_stage_may_overrun_but_blocks_following_adapter(self) -> None:
        import json
        import os

        for stage in ("serialize", "file_fsync", "replace", "directory_fsync", "event_append"):
            with self.subTest(stage=stage):
                now = [0.0]
                adapter = ScriptedAdapter()
                state_path = self.root / f"slow-{stage}.json"
                engine = Engine(
                    state_path,
                    adapter,
                    operation_deadline_seconds=5.0,
                    clock=lambda now=now: now[0],
                )
                real_dump = json.dump
                real_fsync = os.fsync
                real_replace = os.replace
                real_append = __import__(
                    "switchstand.legacy_persistence", fromlist=["append_private_json"]
                ).append_private_json
                fsync_calls = [0]

                def slow_dump(
                    *args: Any, real_dump=real_dump, now=now, **kwargs: Any
                ) -> None:
                    real_dump(*args, **kwargs)
                    now[0] = 6.0

                def slow_fsync(
                    fd: int,
                    *,
                    real_fsync=real_fsync,
                    stage=stage,
                    now=now,
                    fsync_calls=fsync_calls,
                ) -> None:
                    real_fsync(fd)
                    is_directory = stat.S_ISDIR(os.fstat(fd).st_mode)
                    if stage == "directory_fsync" and is_directory:
                        now[0] = 6.0
                    elif stage == "file_fsync" and not is_directory and fsync_calls[0] == 0:
                        now[0] = 6.0
                    if not is_directory:
                        fsync_calls[0] += 1

                def slow_replace(
                    source: Any,
                    destination: Any,
                    *,
                    real_replace=real_replace,
                    now=now,
                ) -> None:
                    real_replace(source, destination)
                    now[0] = 6.0

                def slow_append(
                    path: Path,
                    value: Mapping[str, Any],
                    *,
                    real_append=real_append,
                    now=now,
                ) -> None:
                    real_append(path, value)
                    now[0] = 6.0

                target = {
                    "serialize": patch("switchstand.legacy_persistence.json.dump", side_effect=slow_dump),
                    "file_fsync": patch("switchstand.legacy_persistence.os.fsync", side_effect=slow_fsync),
                    "replace": patch("switchstand.legacy_persistence.os.replace", side_effect=slow_replace),
                    "directory_fsync": patch(
                        "switchstand.legacy_persistence.os.fsync", side_effect=slow_fsync
                    ),
                    "event_append": patch("switchstand.engine._append_private_json", side_effect=slow_append),
                }[stage]
                with target:
                    state = engine.enqueue_snapshot("role-a", "accepted")
                self.assertEqual(state["messages"][0]["status"], "queued")
                self.assertEqual(adapter.calls, [])
                self.assertFalse(engine.persistence_failed)
                restarted = Engine(state_path, ScriptedAdapter()).snapshot()
                self.assertEqual(restarted["messages"][0]["status"], "queued")

    def test_each_event_append_boundary_failure_latches_after_authoritative_snapshot(self) -> None:
        import os

        class FailingAppendHandle:
            def __init__(self, handle: Any, stage: str) -> None:
                self.handle = handle
                self.stage = stage

            def __enter__(self):
                self.handle.__enter__()
                return self

            def __exit__(self, *args: Any):
                return self.handle.__exit__(*args)

            def write(self, value: str) -> int:
                if self.stage == "write":
                    raise OSError("event write failed")
                return self.handle.write(value)

            def flush(self) -> None:
                if self.stage == "flush":
                    raise OSError("event flush failed")
                self.handle.flush()

            def fileno(self) -> int:
                return self.handle.fileno()

        for stage in ("write", "flush", "fsync"):
            with self.subTest(stage=stage):
                adapter = ScriptedAdapter()
                state_path = self.root / f"event-{stage}.json"
                engine = Engine(state_path, adapter)
                real_fdopen = os.fdopen
                real_fsync = os.fsync
                fsync_calls = [0]

                def wrapped_fdopen(
                    fd: int,
                    mode: str,
                    *,
                    real_fdopen=real_fdopen,
                    stage=stage,
                    **kwargs: Any,
                ):
                    handle = real_fdopen(fd, mode, **kwargs)
                    return FailingAppendHandle(handle, stage) if mode == "a" else handle

                def fail_event_fsync(
                    fd: int,
                    *,
                    fsync_calls=fsync_calls,
                    stage=stage,
                    real_fsync=real_fsync,
                ) -> None:
                    fsync_calls[0] += 1
                    if stage == "fsync" and fsync_calls[0] == 3:
                        raise OSError("event fsync failed")
                    real_fsync(fd)

                with (
                    patch("switchstand.legacy_persistence.os.fdopen", side_effect=wrapped_fdopen),
                    patch("switchstand.legacy_persistence.os.fsync", side_effect=fail_event_fsync),
                    self.assertRaises(PersistenceUnavailable),
                ):
                    engine.enqueue("role-a", "authoritative queued")
                self.assertTrue(engine.persistence_failed)
                self.assertEqual(adapter.calls, [])
                restarted = Engine(state_path, ScriptedAdapter()).snapshot()
                self.assertEqual(restarted["messages"][0]["status"], "queued")

    def test_post_side_effect_event_failure_preserves_exact_thread_on_restart(self) -> None:
        adapter = ScriptedAdapter()
        state_path = self.root / "post-side-effect-event.json"
        engine = Engine(state_path, adapter)
        real_append = __import__(
            "switchstand.legacy_persistence", fromlist=["append_private_json"]
        ).append_private_json

        def fail_waiting_event(path: Path, value: Mapping[str, Any]) -> None:
            if value.get("event") == "attempt_waiting":
                raise OSError("event append failed after exact thread id")
            real_append(path, value)

        with (
            patch("switchstand.engine._append_private_json", side_effect=fail_waiting_event),
            self.assertRaises(PersistenceUnavailable),
        ):
            engine.enqueue("role-a", "accepted")
        self.assertTrue(engine.persistence_failed)
        self.assertEqual(adapter.calls.count("create"), 1)
        restarted = Engine(state_path, ScriptedAdapter()).snapshot()
        record = current_attempt(restarted)
        self.assertEqual(record["thread_id"], "thread-1")
        self.assertEqual(record["status"], "waiting")

if __name__ == "__main__":
    unittest.main()
