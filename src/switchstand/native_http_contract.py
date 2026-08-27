"""Shared dependency-free constants and predicates for native loopback HTTP."""
from __future__ import annotations

import ipaddress
from typing import Final, Literal, TypedDict
from urllib.parse import urlsplit


MAX_BODY_BYTES: Final = 64 * 1024
CONTROL_HEADER_NAME: Final = "X-Switchstand-Control"
NATIVE_SELECTION_CONTROL_VALUE: Final = "native-selection-v1"
NATIVE_INPUT_CONTROL_VALUE: Final = "native-input-v1"
NATIVE_STOP_CONTROL_VALUE: Final = "native-stop-v1"


class NativeHttpFailureBody(TypedDict):
    code: Literal[
        "method_not_allowed",
        "control_request_rejected",
        "invalid_request",
        "not_found",
        "service_unavailable",
    ]
    outcome: Literal["not_sent"]


METHOD_NOT_ALLOWED_BODY: Final[NativeHttpFailureBody] = {
    "code": "method_not_allowed",
    "outcome": "not_sent",
}
CONTROL_REQUEST_REJECTED_BODY: Final[NativeHttpFailureBody] = {
    "code": "control_request_rejected",
    "outcome": "not_sent",
}
INVALID_REQUEST_BODY: Final[NativeHttpFailureBody] = {
    "code": "invalid_request",
    "outcome": "not_sent",
}
NOT_FOUND_BODY: Final[NativeHttpFailureBody] = {
    "code": "not_found",
    "outcome": "not_sent",
}
SERVICE_UNAVAILABLE_BODY: Final[NativeHttpFailureBody] = {
    "code": "service_unavailable",
    "outcome": "not_sent",
}


def is_loopback_host(value: str | None) -> bool:
    """Apply the service's existing Host loopback predicate."""
    if not value:
        return False
    try:
        hostname = urlsplit(f"//{value}").hostname
        return hostname == "localhost" or (
            hostname is not None and ipaddress.ip_address(hostname).is_loopback
        )
    except ValueError:
        return False


def is_same_origin_http(origin: str | None, host: str | None) -> bool:
    """Apply the service's existing optional same-origin HTTP predicate."""
    if origin is None:
        return True
    parsed = urlsplit(origin)
    return (
        origin != "null"
        and parsed.scheme == "http"
        and parsed.netloc.lower() == (host or "").lower()
    )
