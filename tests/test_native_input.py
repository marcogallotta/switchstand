from __future__ import annotations

from dataclasses import dataclass
import io
import logging
from typing import Any
import unittest

from switchstand.native_input import NativeInput, validate_native_input_request


NOT_SENT = {"code": "input_unavailable", "outcome": "not_sent"}


@dataclass(frozen=True, repr=False)
class OpaqueTarget:
    value: str


def request(text: Any = "input", **changes):
    value = {
        "version": "native-input-v1",
        "observationRunRef": "run-ref",
        "agentRef": "agent-ref",
        "text": text,
    }
    value.update(changes)
    return value


def read_result(target, status="none", turns=None, **fields):
    data = [] if turns is None else [
        {**turn, "items": [], "itemsView": "notLoaded"} for turn in turns
    ]
    return {"target": target, "data": data, **fields}


class Resolver:
    def __init__(self, targets, events):
        self._targets = iter(targets)
        self._events = events
        self.calls = []

    def __call__(self, selection, *, now, maximum_observation_age_seconds):
        self._events.append("resolve")
        self.calls.append((dict(selection), now, maximum_observation_age_seconds))
        value = next(self._targets)
        if isinstance(value, BaseException):
            raise value
        return value


class Transport:
    def __init__(self, events, *, read=None, start=None, steer=None):
        self.events = events
        self.read = read
        self.start = {"turn": {"id": "new-turn"}} if start is None else start
        self.steer = {"turnId": "active-turn"} if steer is None else steer
        self.calls = []

    @staticmethod
    def _result(value):
        if isinstance(value, BaseException):
            raise value
        if isinstance(value, tuple):
            return value
        return "ok", value

    def turns_list(self, target, **limits):
        self.events.append("read")
        self.calls.append(("read", target, limits))
        value = self.read if self.read is not None else read_result(target)
        return self._result(value)

    def turn_start(self, target, text, **limits):
        self.events.append("start")
        self.calls.append(("start", target, text, limits))
        return self._result(self.start)

    def turn_steer(self, target, expected_turn_id, text, **limits):
        self.events.append("steer")
        self.calls.append(("steer", target, expected_turn_id, text, limits))
        return self._result(self.steer)


def service(targets, transport, events, clock_values=(10.0, 11.0)):
    resolver = Resolver(targets, events)
    values = iter(clock_values)
    native_input = NativeInput(
        resolver,
        transport,
        maximum_observation_age_seconds=4.5,
        clock=lambda: next(values),
        timeout_seconds=1.25,
        max_response_bytes=12345,
    )
    return native_input, resolver


class NativeInputValidationTests(unittest.TestCase):
    def test_closed_request_rejects_shape_version_and_references(self):
        valid = request()
        cases = [None, [], {}, {key: value for key, value in valid.items() if key != "text"}]
        cases += [dict(valid, extra=True), dict(valid, version="native-input-v2")]
        cases += [dict(valid, observationRunRef=value) for value in (None, "", 3, True)]
        cases += [dict(valid, agentRef=value) for value in (None, "", 3, True)]
        for value in cases:
            with self.subTest(value=value):
                self.assertFalse(validate_native_input_request(value))

    def test_text_utf8_boundaries_and_preservation(self):
        accepted = (
            "x",
            "x" * (16 * 1024),
            "é" * (8 * 1024),
            "  exact input\n",
        )
        rejected = (
            None,
            1,
            True,
            "",
            " \t\n",
            "x" * (16 * 1024 + 1),
            "é" * (8 * 1024) + "x",
            "\ud800",
        )
        for text in accepted:
            with self.subTest(accepted=len(text)):
                self.assertTrue(validate_native_input_request(request(text)))
        for text in rejected:
            with self.subTest(rejected=repr(text)):
                self.assertFalse(validate_native_input_request(request(text)))

    def test_invalid_request_calls_neither_resolver_nor_transport(self):
        events = []
        transport = Transport(events)
        native_input, resolver = service([OpaqueTarget("unused")], transport, events)
        self.assertEqual(native_input.send(request("\ud800")), NOT_SENT)
        self.assertEqual(events, [])
        self.assertEqual(resolver.calls, [])
        self.assertEqual(transport.calls, [])


class NativeInputDispatchTests(unittest.TestCase):
    def test_idle_fences_twice_then_starts_once_with_native_settings_preserved(self):
        events = []
        first = OpaqueTarget("same")
        transport = Transport(events, read=read_result(first))
        native_input, resolver = service([first, OpaqueTarget("same")], transport, events)

        result = native_input.send(request("  exact input\n"))

        self.assertEqual(result, {"code": "input_sent", "outcome": "sent", "mode": "start"})
        self.assertEqual(events, ["resolve", "read", "resolve", "start"])
        self.assertEqual([call[0] for call in transport.calls], ["read", "start"])
        self.assertEqual(transport.calls[1][2], "  exact input\n")
        self.assertEqual(transport.calls[1][3], {
            "max_response_bytes": 12345, "timeout_seconds": 1.25})
        self.assertEqual([call[1:] for call in resolver.calls], [(10.0, 4.5), (11.0, 4.5)])
        self.assertEqual(resolver.calls[0][0], {
            "observationRunRef": "run-ref", "agentRef": "agent-ref"})

    def test_active_fences_twice_then_steers_the_sole_exact_turn_once(self):
        events = []
        target = OpaqueTarget("same")
        turns = [{"id": "active-turn", "status": "inProgress"}]
        transport = Transport(events, read=read_result(target, turns=turns))
        native_input, _ = service([target, OpaqueTarget("same")], transport, events)

        result = native_input.send(request("focus"))

        self.assertEqual(result, {"code": "input_sent", "outcome": "sent", "mode": "steer"})
        self.assertEqual(events, ["resolve", "read", "resolve", "steer"])
        self.assertEqual(transport.calls[1][2:4], ("active-turn", "focus"))

    def test_resolution_failure_or_drift_sends_no_action_and_never_retries(self):
        failures = (
            ([{"code": "INVALID_AGENT_REF"}], ["resolve"]),
            ([RuntimeError("PRIVATE-FAILURE")], ["resolve"]),
            ([OpaqueTarget("one"), {"code": "OBSERVATION_STALE"}],
                ["resolve", "read", "resolve"]),
            ([OpaqueTarget("one"), OpaqueTarget("two")], ["resolve", "read", "resolve"]),
        )
        for targets, expected_events in failures:
            with self.subTest(targets=len(targets)):
                events = []
                first = targets[0] if isinstance(targets[0], OpaqueTarget) else OpaqueTarget("one")
                transport = Transport(events, read=read_result(first))
                native_input, _ = service(targets, transport, events)
                self.assertEqual(native_input.send(request()), NOT_SENT)
                self.assertEqual(events, expected_events)
                self.assertFalse(any(call[0] in {"start", "steer"} for call in transport.calls))

    def test_invalid_or_non_actionable_read_sends_no_action(self):
        target = OpaqueTarget("same")
        reads = (
            ("malformed", None),
            ("oversize", None),
            ("ambiguous", None),
            RuntimeError("TRANSPORT-SENTINEL"),
            {},
            read_result(OpaqueTarget("wrong")),
            {"target": target, "data": [], "nextCursor": 17},
            {"target": target, "data": [], "nextCursor": "older"},
            {"target": target, "data": [{"id": "turn", "status": "inProgress",
                "items": [{"text": "forbidden"}], "itemsView": "notLoaded"}]},
            {"target": target, "data": [{"id": "turn", "status": "unknown",
                "items": [], "itemsView": "notLoaded"}]},
            {"target": target, "data": [{"id": "one", "status": "inProgress",
                "items": [], "itemsView": "notLoaded"}, {"id": "two",
                "status": "inProgress", "items": [], "itemsView": "notLoaded"}]},
        )
        for read in reads:
            with self.subTest(read=type(read).__name__):
                events = []
                transport = Transport(events, read=read)
                native_input, _ = service([target], transport, events)
                self.assertEqual(native_input.send(request()), NOT_SENT)
                self.assertEqual([call[0] for call in transport.calls], ["read"])

    def test_rejection_race_or_malformed_ack_never_falls_back(self):
        target = OpaqueTarget("same")
        cases = (
            ("idle", [], {"error": "race"}),
            ("idle", [], ("rejected", None)),
            ("idle", [], {"turn": {"id": ""}}),
            ("idle", [], RuntimeError("ACK-SENTINEL")),
            ("active", [{"id": "active-turn", "status": "inProgress"}],
                {"turnId": "different-turn"}),
            ("active", [{"id": "active-turn", "status": "inProgress"}],
                {"turn": {"id": "active-turn"}}),
            ("active", [{"id": "active-turn", "status": "inProgress"}],
                ("ambiguous", None)),
        )
        for status, turns, acknowledgement in cases:
            with self.subTest(status=status, acknowledgement=type(acknowledgement).__name__):
                events = []
                kwargs = {"start": acknowledgement} if status == "idle" else {"steer": acknowledgement}
                transport = Transport(events, read=read_result(target, turns=turns), **kwargs)
                native_input, _ = service([target, target], transport, events)
                self.assertEqual(native_input.send(request()), NOT_SENT)
                action_calls = [call for call in transport.calls if call[0] in {"start", "steer"}]
                self.assertEqual(len(action_calls), 1)

    def test_private_values_do_not_escape_logs_exceptions_or_callable_state(self):
        sentinels = (
            "INPUT-PRIVACY-SENTINEL", "READ-CONTENT-SENTINEL",
            "RUN-PRIVACY-SENTINEL", "AGENT-PRIVACY-SENTINEL",
            "RAW-TARGET-SENTINEL", "RAW-FAILURE-SENTINEL", "/private/socket/path",
        )
        target = OpaqueTarget(sentinels[4])
        events = []
        transport = Transport(events, read=read_result(target))
        resolver = Resolver([target, target], events)
        native_input = NativeInput(
            resolver, transport, maximum_observation_age_seconds=3, clock=lambda: 1)
        request_value = {
            "version": "native-input-v1",
            "observationRunRef": sentinels[2],
            "agentRef": sentinels[3],
            "text": sentinels[0],
        }
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        root = logging.getLogger()
        root.addHandler(handler)
        try:
            result = native_input.send(request_value)
        finally:
            root.removeHandler(handler)
        retained_state = repr(vars(native_input))
        disclosure = repr(result) + stream.getvalue() + retained_state
        self.assertEqual(result, {"code": "input_sent", "outcome": "sent", "mode": "start"})
        for sentinel in sentinels:
            self.assertNotIn(sentinel, disclosure)


if __name__ == "__main__":
    unittest.main()
