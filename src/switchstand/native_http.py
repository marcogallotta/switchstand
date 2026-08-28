"""Dependency-free, fail-closed HTTP composition for native workbench ports."""
from __future__ import annotations

from dataclasses import dataclass
import json
import types
from types import MappingProxyType
from typing import (
    Any,
    Literal,
    NotRequired,
    Required,
    get_args,
    get_origin,
    get_type_hints,
    is_typeddict,
)

from .native_contracts import (
    NativeBrowserSelectionResult,
    NativeEvidenceRequest,
    NativeEvidenceResult,
    NativeInputResult,
    NativeStopCommitResult,
    NativeStopPrepareResult,
    NativeStopStatusResult,
    NativeWorkbenchSnapshot,
)
from .native_http_contract import (
    CONTROL_HEADER_NAME,
    CONTROL_REQUEST_REJECTED_BODY,
    INVALID_REQUEST_BODY,
    MAX_BODY_BYTES,
    METHOD_NOT_ALLOWED_BODY,
    NATIVE_EVIDENCE_CONTROL_VALUE,
    NATIVE_INPUT_CONTROL_VALUE,
    NATIVE_SELECTION_CONTROL_VALUE,
    NATIVE_STOP_CONTROL_VALUE,
    NOT_FOUND_BODY,
    SERVICE_UNAVAILABLE_BODY,
    is_loopback_host,
    is_same_origin_http,
)
from .native_workbench import NativeWorkbench


Action = Literal[
    "workbench",
    "resolve_selection",
    "send_input",
    "prepare_stop",
    "commit_stop",
    "stop_status",
    "record_evidence",
]
FieldKind = Literal[
    "any",
    "string",
    "nonempty-string",
    "native-input-version",
    "native-evidence-version",
    "native-evidence-event",
]


@dataclass(frozen=True)
class RequestField:
    name: str
    kind: FieldKind


@dataclass(frozen=True)
class NativeRoute:
    method: Literal["GET", "POST"]
    path: str
    control_value: str | None
    request_fields: tuple[RequestField, ...]
    action: Action
    result_type: object


NATIVE_ROUTES = (
    NativeRoute("GET", "/api/workbench", None, (), "workbench", NativeWorkbenchSnapshot),
    NativeRoute(
        "POST",
        "/api/native-selection/resolve",
        NATIVE_SELECTION_CONTROL_VALUE,
        (RequestField("agentRef", "nonempty-string"),),
        "resolve_selection",
        NativeBrowserSelectionResult,
    ),
    NativeRoute(
        "POST",
        "/api/native-input",
        NATIVE_INPUT_CONTROL_VALUE,
        (
            RequestField("version", "native-input-version"),
            RequestField("observationRunRef", "string"),
            RequestField("agentRef", "string"),
            RequestField("text", "string"),
        ),
        "send_input",
        NativeInputResult,
    ),
    NativeRoute(
        "POST",
        "/api/native-evidence",
        NATIVE_EVIDENCE_CONTROL_VALUE,
        (
            RequestField("version", "native-evidence-version"),
            RequestField("event", "native-evidence-event"),
        ),
        "record_evidence",
        NativeEvidenceResult,
    ),
    NativeRoute(
        "POST",
        "/api/native-stop/prepare",
        NATIVE_STOP_CONTROL_VALUE,
        (RequestField("agentRef", "any"),),
        "prepare_stop",
        NativeStopPrepareResult,
    ),
    NativeRoute(
        "POST",
        "/api/native-stop/commit",
        NATIVE_STOP_CONTROL_VALUE,
        (RequestField("confirmationRef", "any"),),
        "commit_stop",
        NativeStopCommitResult,
    ),
    NativeRoute(
        "POST",
        "/api/native-stop/status",
        NATIVE_STOP_CONTROL_VALUE,
        (RequestField("operationRef", "any"),),
        "stop_status",
        NativeStopStatusResult,
    ),
)
_ROUTE_BY_PATH = MappingProxyType({route.path: route for route in NATIVE_ROUTES})
_SECURITY_HEADERS = frozenset({"host", "origin", CONTROL_HEADER_NAME.lower()})


@dataclass(frozen=True)
class NativeHttpRequest:
    method: str
    path: str
    headers: tuple[tuple[str, str], ...]
    body: bytes = b""


@dataclass(frozen=True)
class NativeHttpResponse:
    status: int
    headers: tuple[tuple[str, str], ...]
    body: bytes


class _DuplicateJsonField(ValueError):
    pass


def _header_values(request: NativeHttpRequest) -> dict[str, list[str]]:
    values: dict[str, list[str]] = {}
    for name, value in request.headers:
        if not isinstance(name, str) or not isinstance(value, str):
            raise ValueError("invalid header")
        values.setdefault(name.lower(), []).append(value)
    return values


def _one_header(headers: dict[str, list[str]], name: str) -> str | None:
    values = headers.get(name.lower(), [])
    if len(values) > 1:
        raise ValueError("duplicate header")
    return values[0] if values else None


def _security_valid(
    route: NativeRoute,
    headers: dict[str, list[str]],
) -> bool:
    try:
        if any(len(headers.get(name, [])) > 1 for name in _SECURITY_HEADERS):
            return False
        host = _one_header(headers, "Host")
        origin = _one_header(headers, "Origin")
        control = _one_header(headers, CONTROL_HEADER_NAME)
        return (
            is_loopback_host(host)
            and is_same_origin_http(origin, host)
            and control == route.control_value
        )
    except (TypeError, UnicodeError, ValueError):
        return False


def _content_length(headers: dict[str, list[str]], *, required: bool) -> int | None:
    raw = _one_header(headers, "Content-Length")
    if raw is None:
        if required:
            raise ValueError("missing length")
        return None
    if not raw or not raw.isascii() or not raw.isdecimal():
        raise ValueError("invalid length")
    length = int(raw)
    if length > MAX_BODY_BYTES:
        raise ValueError("oversize")
    return length


def _decode_object(body: bytes) -> dict[str, Any]:
    def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise _DuplicateJsonField
            result[key] = value
        return result

    def reject_constant(_value: str) -> None:
        raise ValueError("non-finite JSON number")

    value = json.loads(
        body.decode("utf-8", errors="strict"),
        object_pairs_hook=unique,
        parse_constant=reject_constant,
    )
    if not isinstance(value, dict):
        raise ValueError("request is not an object")
    return value


def _request_body(route: NativeRoute, request: NativeHttpRequest, headers: dict[str, list[str]]) -> dict[str, Any]:
    if route.method == "GET":
        length = _content_length(headers, required=False)
        if request.body or length not in {None, 0}:
            raise ValueError("GET body is forbidden")
        return {}
    if _one_header(headers, "Content-Type") != "application/json":
        raise ValueError("invalid media type")
    length = _content_length(headers, required=True)
    if length != len(request.body):
        raise ValueError("length mismatch")
    body = _decode_object(request.body)
    if set(body) != {field.name for field in route.request_fields}:
        raise ValueError("invalid fields")
    for field in route.request_fields:
        value = body[field.name]
        if field.kind == "string" and type(value) is not str:
            raise ValueError("invalid string")
        if field.kind == "nonempty-string" and (type(value) is not str or not value):
            raise ValueError("invalid reference")
        if field.kind == "native-input-version" and value != NATIVE_INPUT_CONTROL_VALUE:
            raise ValueError("invalid version")
        if field.kind == "native-evidence-version" and value != NATIVE_EVIDENCE_CONTROL_VALUE:
            raise ValueError("invalid version")
        if field.kind == "native-evidence-event" and value not in {
            "focus_preservation_failed", "refresh_coalesced", "stop_cancelled"
        }:
            raise ValueError("invalid evidence event")
    return body


def _matches_contract(value: object, annotation: object) -> bool:
    if is_typeddict(annotation):
        if not isinstance(value, dict):
            return False
        hints = get_type_hints(annotation, include_extras=True)
        total = bool(getattr(annotation, "__total__", True))
        required = {
            key
            for key, hint in hints.items()
            if get_origin(hint) is Required
            or (total and get_origin(hint) is not NotRequired)
        }
        if not required <= value.keys() or not value.keys() <= hints.keys():
            return False
        return all(_matches_contract(item, hints[key]) for key, item in value.items())
    origin = get_origin(annotation)
    if origin is types.UnionType:
        return any(_matches_contract(value, member) for member in get_args(annotation))
    if origin is Literal:
        return any(
            type(value) is type(member) and value == member
            for member in get_args(annotation)
        )
    if origin is NotRequired:
        return _matches_contract(value, get_args(annotation)[0])
    if origin is list:
        (member,) = get_args(annotation)
        return isinstance(value, list) and all(_matches_contract(item, member) for item in value)
    if annotation is None or annotation is type(None):
        return value is None
    if annotation in {bool, int, float, str}:
        return type(value) is annotation
    return False


def _response(status: int, value: object) -> NativeHttpResponse:
    body = json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return NativeHttpResponse(
        status,
        (
            ("Content-Type", "application/json; charset=utf-8"),
            ("Content-Length", str(len(body))),
            ("Cache-Control", "no-store"),
            ("X-Content-Type-Options", "nosniff"),
        ),
        body,
    )


def _failure(status: int, value: object) -> NativeHttpResponse:
    return _response(status, value)


class NativeHttpDispatcher:
    def __init__(self, workbench: NativeWorkbench) -> None:
        self._workbench = workbench

    @staticmethod
    def _invoke(workbench: NativeWorkbench, route: NativeRoute, body: dict[str, Any]) -> object:
        if route.action == "workbench":
            return workbench.workbench()
        if route.action == "resolve_selection":
            return workbench.resolve_selection(body["agentRef"])
        if route.action == "send_input":
            return workbench.send_input(body)
        if route.action == "record_evidence":
            return workbench.record_browser_evidence(body)  # type: ignore[arg-type]
        if route.action == "prepare_stop":
            return workbench.prepare_stop(body["agentRef"])
        if route.action == "commit_stop":
            return workbench.commit_stop(body["confirmationRef"])
        return workbench.stop_status(body["operationRef"])

    def dispatch(self, request: NativeHttpRequest) -> NativeHttpResponse | None:
        route = _ROUTE_BY_PATH.get(request.path)
        if route is None:
            if request.path.startswith("/api/native-") or request.path.startswith(
                "/api/workbench?"
            ):
                return _failure(404, NOT_FOUND_BODY)
            return None
        if request.method != route.method:
            return _failure(405, METHOD_NOT_ALLOWED_BODY)
        try:
            headers = _header_values(request)
        except ValueError:
            return _failure(403, CONTROL_REQUEST_REJECTED_BODY)
        if not _security_valid(route, headers):
            return _failure(403, CONTROL_REQUEST_REJECTED_BODY)
        try:
            body = _request_body(route, request, headers)
        except (UnicodeError, ValueError, json.JSONDecodeError, RecursionError):
            return _failure(400, INVALID_REQUEST_BODY)
        try:
            result = self._invoke(self._workbench, route, body)
            if not _matches_contract(result, route.result_type):
                raise ValueError("domain result violates its frozen contract")
            return _response(200, result)
        except Exception:
            return _failure(503, SERVICE_UNAVAILABLE_BODY)
