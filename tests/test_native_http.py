from __future__ import annotations

import json
from typing import TypeVar, cast
import unittest

from switchstand.native_contracts import (
    NativeBoardSnapshot,
    NativeBrowserSelectionResult,
    NativeInputResult,
    NativeStopCommitResult,
    NativeStopPrepareResult,
    NativeStopStatusResult,
)
from switchstand.native_http import (
    NATIVE_ROUTES,
    NativeHttpDispatcher,
    NativeHttpRequest,
    NativeHttpResponse,
    RequestField,
)
from switchstand.native_workbench import NativeWorkbench


def snapshot() -> NativeBoardSnapshot:
    return {
        "mode": "native",
        "observation": {
            "connected": True,
            "available": True,
            "historical": False,
            "errorCode": None,
            "completedAt": 10.0,
            "passAgeSeconds": 0.0,
            "kind": "completed_multi_request_pass",
        },
        "agents": [],
        "trail": [],
        "trailLimit": 50,
        "disclosure": "Observed differences only.",
    }


T = TypeVar("T")


class FakePorts:
    def __init__(self) -> None:
        self.calls: list[tuple[object, ...]] = []
        self.failure: object | None = None
        self.overrides: dict[str, object] = {}

    def _result(self, value: T) -> T:
        if isinstance(self.failure, BaseException):
            raise self.failure
        return value if self.failure is None else cast(T, self.failure)

    def snapshot(self) -> NativeBoardSnapshot:
        self.calls.append(("workbench",))
        return self._result(cast(NativeBoardSnapshot, self.overrides.get("workbench", snapshot())))

    def browser_selection(
        self,
        agent_ref: object,
        *,
        now: object,
        maximum_observation_age_seconds: object,
    ) -> NativeBrowserSelectionResult:
        self.calls.append((
            "selection", agent_ref, now, maximum_observation_age_seconds
        ))
        value = {
            "selection": {"observationRunRef": "run-1", "agentRef": "agent-1"},
            "snapshot": {
                "version": "native-selection-v1",
                "observationRunRef": "run-1",
                "agentRef": "agent-1",
                "connected": True,
                "present": True,
            },
        }
        return self._result(cast(
            NativeBrowserSelectionResult, self.overrides.get("selection", value)
        ))

    def send(self, request: object) -> NativeInputResult:
        self.calls.append(("input", request))
        value = self.overrides.get(
            "input", {"code": "input_sent", "outcome": "sent", "mode": "start"}
        )
        return self._result(cast(NativeInputResult, value))

    def prepare_stop(self, agent_ref: object) -> NativeStopPrepareResult:
        self.calls.append(("prepare", agent_ref))
        value = self.overrides.get(
            "prepare", {"code": "target_unavailable", "outcome": "not_sent"}
        )
        return self._result(cast(NativeStopPrepareResult, value))

    def commit_stop(self, confirmation_ref: object) -> NativeStopCommitResult:
        self.calls.append(("commit", confirmation_ref))
        value = self.overrides.get(
            "commit", {"code": "stop_result", "operationRef": "op-1", "outcome": "requested"}
        )
        return self._result(cast(NativeStopCommitResult, value))

    def stop_status(self, operation_ref: object) -> NativeStopStatusResult:
        self.calls.append(("status", operation_ref))
        value = self.overrides.get(
            "status", {"code": "stop_result", "operationRef": "op-1", "outcome": "confirmed"}
        )
        return self._result(cast(NativeStopStatusResult, value))


def request(
    path: str,
    body: object | None = None,
    *,
    method: str | None = None,
    control: str | None = None,
    headers: tuple[tuple[str, str], ...] = (),
) -> NativeHttpRequest:
    encoded = b"" if body is None else json.dumps(body, separators=(",", ":")).encode()
    base = [("Host", "127.0.0.1:4180"), ("Origin", "http://127.0.0.1:4180")]
    if body is not None:
        base.extend((("Content-Type", "application/json"), ("Content-Length", str(len(encoded)))))
    if control is not None:
        base.append(("X-Switchstand-Control", control))
    return NativeHttpRequest(method or ("GET" if body is None else "POST"), path, tuple(base) + headers, encoded)


def handled(response: NativeHttpResponse | None) -> NativeHttpResponse:
    if response is None:
        raise AssertionError("expected a handled native request")
    return response


def decoded(response: NativeHttpResponse) -> dict[str, object]:
    value = json.loads(response.body)
    if not isinstance(value, dict):
        raise AssertionError("expected a JSON object response")
    return value


class NativeHttpTests(unittest.TestCase):
    def setUp(self) -> None:
        self.ports = FakePorts()
        facade = NativeWorkbench(
            self.ports,
            self.ports,
            self.ports,
            self.ports,
            maximum_observation_age_seconds=5.0,
            clock=lambda: 12.0,
        )
        self.dispatcher = NativeHttpDispatcher(facade)

    def test_literal_route_and_schema_table_is_exact(self):
        self.assertEqual(
            [(route.method, route.path, route.control_value, route.request_fields) for route in NATIVE_ROUTES],
            [
                ("GET", "/api/workbench", None, ()),
                ("POST", "/api/native-selection/resolve", "native-selection-v1",
                    (RequestField("agentRef", "nonempty-string"),)),
                ("POST", "/api/native-input", "native-input-v1", (
                    RequestField("version", "native-input-version"),
                    RequestField("observationRunRef", "string"),
                    RequestField("agentRef", "string"),
                    RequestField("text", "string"),
                )),
                ("POST", "/api/native-evidence", "native-evidence-v1", (
                    RequestField("version", "native-evidence-version"),
                    RequestField("event", "native-evidence-event"),
                )),
                ("POST", "/api/native-stop/prepare", "native-stop-v1",
                    (RequestField("agentRef", "any"),)),
                ("POST", "/api/native-stop/commit", "native-stop-v1",
                    (RequestField("confirmationRef", "any"),)),
                ("POST", "/api/native-stop/status", "native-stop-v1",
                    (RequestField("operationRef", "any"),)),
            ],
        )

    def test_every_valid_route_calls_one_exact_port_and_returns_closed_result(self):
        cases = (
            request("/api/workbench"),
            request("/api/native-selection/resolve", {"agentRef": "agent-1"}, control="native-selection-v1"),
            request("/api/native-input", {
                "version": "native-input-v1", "observationRunRef": "run-1",
                "agentRef": "agent-1", "text": "exact text",
            }, control="native-input-v1"),
            request("/api/native-evidence", {
                "version": "native-evidence-v1", "event": "focus_preservation_failed",
            }, control="native-evidence-v1"),
            request("/api/native-stop/prepare", {"agentRef": None}, control="native-stop-v1"),
            request("/api/native-stop/commit", {"confirmationRef": "confirm-1"}, control="native-stop-v1"),
            request("/api/native-stop/status", {"operationRef": "op-1"}, control="native-stop-v1"),
        )
        for expected, item in zip(
            ("workbench", "selection", "input", "evidence", "prepare", "commit", "status"),
            cases,
            strict=True
        ):
            if expected == "workbench":
                item = NativeHttpRequest(
                    item.method,
                    item.path,
                    tuple(header for header in item.headers if header[0] != "Origin"),
                    item.body,
                )
            before = len(self.ports.calls)
            response = handled(self.dispatcher.dispatch(item))
            self.assertEqual(response.status, 200)
            expected_calls = before if expected == "evidence" else before + 1
            self.assertEqual(len(self.ports.calls), expected_calls)
            if expected != "evidence":
                self.assertEqual(self.ports.calls[-1][0], expected)
            self.assertEqual(dict(response.headers), {
                "Content-Type": "application/json; charset=utf-8",
                "Content-Length": str(len(response.body)),
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
            })
            self.assertNotIn("Access-Control-Allow-Origin", dict(response.headers))
            self.assertIsInstance(decoded(response), dict)

    def test_security_method_and_body_rejections_precede_every_port(self):
        valid_input = {
            "version": "native-input-v1", "observationRunRef": "run-1",
            "agentRef": "agent-1", "text": "text",
        }
        base = request("/api/native-input", valid_input, control="native-input-v1")
        duplicate_json = b'{"agentRef":"one","agentRef":"two"}'
        no_host = tuple((name, value) for name, value in base.headers if name != "Host")
        no_length = tuple((name, value) for name, value in base.headers if name != "Content-Length")
        malformed = b'{"version":'
        non_finite = b'{"agentRef":NaN}'
        rejected = (
            (request("/api/workbench", method="OPTIONS"), 405),
            (request("/api/native-input", valid_input, method="GET", control="native-input-v1"), 405),
            (NativeHttpRequest("POST", base.path, no_host, base.body), 403),
            (request(base.path, valid_input, control="wrong"), 403),
            (NativeHttpRequest("POST", base.path, tuple(
                (name, "example.com" if name == "Host" else value) for name, value in base.headers
            ), base.body), 403),
            (NativeHttpRequest("POST", base.path, tuple(
                (name, "null" if name == "Origin" else value) for name, value in base.headers
            ), base.body), 403),
            (NativeHttpRequest("POST", base.path, tuple(
                (name, "https://127.0.0.1:4180" if name == "Origin" else value)
                for name, value in base.headers
            ), base.body), 403),
            (NativeHttpRequest("POST", base.path, tuple(
                (name, "http://127.0.0.1:9" if name == "Origin" else value)
                for name, value in base.headers
            ), base.body), 403),
            (NativeHttpRequest("POST", base.path, tuple(
                (name, "text/plain" if name == "Content-Type" else value) for name, value in base.headers
            ), base.body), 400),
            (NativeHttpRequest("POST", base.path, tuple(
                (name, "application/json; charset=utf-8" if name == "Content-Type" else value)
                for name, value in base.headers
            ), base.body), 400),
            (NativeHttpRequest("POST", base.path, no_length, base.body), 400),
            (NativeHttpRequest("POST", base.path, tuple(
                (name, "1" if name == "Content-Length" else value) for name, value in base.headers
            ), base.body), 400),
            (NativeHttpRequest("POST", base.path, tuple(
                (name, "-1" if name == "Content-Length" else value) for name, value in base.headers
            ), base.body), 400),
            (NativeHttpRequest("POST", base.path, tuple(
                (name, str(64 * 1024 + 1) if name == "Content-Length" else value) for name, value in base.headers
            ), base.body), 400),
            (request(base.path, {**valid_input, "extra": True}, control="native-input-v1"), 400),
            (request(base.path, {"version": "native-input-v1"}, control="native-input-v1"), 400),
            (request(base.path, ["not", "object"], control="native-input-v1"), 400),
            (request(base.path, {**valid_input, "version": "native-input-v2"},
                control="native-input-v1"), 400),
            (request("/api/native-selection/resolve", {"agentRef": ""},
                control="native-selection-v1"), 400),
            (request("/api/native-evidence", {
                "version": "native-evidence-v1", "event": "arbitrary",
            }, control="native-evidence-v1"), 400),
            (request("/api/native-evidence", {
                "version": "wrong", "event": "focus_preservation_failed",
            }, control="native-evidence-v1"), 400),
            (request("/api/native-stop/prepare", {}, control="native-stop-v1"), 400),
            (request("/api/native-stop/prepare", {"agentRef": None, "extra": True},
                control="native-stop-v1"), 400),
            (NativeHttpRequest("POST", base.path, tuple(
                (name, str(len(malformed)) if name == "Content-Length" else value)
                for name, value in base.headers
            ), malformed), 400),
            (NativeHttpRequest("GET", "/api/workbench", (
                ("Host", "127.0.0.1:4180"), ("Content-Length", "1")
            ), b"x"), 400),
            (NativeHttpRequest("POST", "/api/native-selection/resolve", (
                ("Host", "127.0.0.1:4180"), ("Content-Type", "application/json"),
                ("Content-Length", str(len(duplicate_json))),
                ("X-Switchstand-Control", "native-selection-v1"),
            ), duplicate_json), 400),
            (NativeHttpRequest("POST", "/api/native-stop/prepare", (
                ("Host", "127.0.0.1:4180"), ("Content-Type", "application/json"),
                ("Content-Length", str(len(non_finite))),
                ("X-Switchstand-Control", "native-stop-v1"),
            ), non_finite), 400),
        )
        for item, status in rejected:
            with self.subTest(status=status, path=item.path):
                self.ports.calls.clear()
                response = handled(self.dispatcher.dispatch(item))
                self.assertEqual(response.status, status)
                self.assertEqual(self.ports.calls, [])
                self.assertIn(decoded(response)["code"], {
                    "method_not_allowed", "control_request_rejected", "invalid_request"
                })

    def test_every_known_route_rejects_options_before_security(self):
        for route in NATIVE_ROUTES:
            with self.subTest(path=route.path):
                self.ports.calls.clear()
                response = handled(self.dispatcher.dispatch(
                    NativeHttpRequest("OPTIONS", route.path, ())
                ))
                self.assertEqual(response.status, 405)
                self.assertEqual(self.ports.calls, [])

    def test_closed_domain_failure_variants_pass_through_without_remapping(self):
        cases = (
            ("selection", "/api/native-selection/resolve", {"agentRef": "agent-1"},
                "native-selection-v1", {
                    "selection": {"observationRunRef": "run-1", "agentRef": "agent-1"},
                    "snapshot": {"code": "OBSERVATION_STALE", "message": "stale"},
                }),
            ("input", "/api/native-input", {
                "version": "native-input-v1", "observationRunRef": "run-1",
                "agentRef": "agent-1", "text": "text",
            }, "native-input-v1", {"code": "input_unavailable", "outcome": "not_sent"}),
            ("prepare", "/api/native-stop/prepare", {"agentRef": "agent-1"},
                "native-stop-v1", {"code": "stop_capacity", "outcome": "not_sent"}),
            ("commit", "/api/native-stop/commit", {"confirmationRef": "missing"},
                "native-stop-v1", {"code": "confirmation_unavailable", "outcome": "not_sent"}),
            ("status", "/api/native-stop/status", {"operationRef": "op-1"},
                "native-stop-v1", {
                    "code": "stop_pending", "operationRef": "op-1", "outcome": "unknown",
                }),
        )
        for action, path, body, control, result in cases:
            with self.subTest(action=action):
                self.ports.overrides = {action: result}
                response = handled(self.dispatcher.dispatch(request(path, body, control=control)))
                self.assertEqual(response.status, 200)
                self.assertEqual(decoded(response), result)
        self.ports.overrides.clear()

    def test_native_unknown_is_closed_and_legacy_is_unhandled(self):
        response = handled(self.dispatcher.dispatch(request("/api/native-replace")))
        self.assertEqual(response.status, 404)
        self.assertEqual(decoded(response), {"code": "not_found", "outcome": "not_sent"})
        query = handled(self.dispatcher.dispatch(request("/api/workbench?unexpected=1")))
        self.assertEqual(query.status, 404)
        self.assertIsNone(self.dispatcher.dispatch(request("/api/workbench/roles/a/messages")))
        self.assertIsNone(self.dispatcher.dispatch(request("/health")))
        self.assertEqual(self.ports.calls, [])

    def test_exception_or_contract_violation_is_fixed_503_without_retention_or_leakage(self):
        sentinel = "PRIVATE-REQUEST-AND-ERROR-SENTINEL"
        for failure in (RuntimeError(sentinel), {"wrong": sentinel}, object()):
            with self.subTest(failure=type(failure).__name__):
                self.ports.failure = failure
                response = handled(self.dispatcher.dispatch(request("/api/native-input", {
                    "version": "native-input-v1", "observationRunRef": "run-1",
                    "agentRef": "agent-1", "text": sentinel,
                }, control="native-input-v1")))
                self.assertEqual(response.status, 503)
                self.assertEqual(decoded(response), {
                    "code": "service_unavailable", "outcome": "not_sent"
                })
                self.assertNotIn(sentinel, response.body.decode())
                self.assertNotIn(sentinel, repr(vars(self.dispatcher)))


if __name__ == "__main__":
    unittest.main()
