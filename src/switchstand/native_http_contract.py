"""Shared dependency-free constants and predicates for native loopback HTTP."""
from __future__ import annotations

import ipaddress
from typing import Final, Literal, TypedDict


MAX_BODY_BYTES: Final = 64 * 1024
CONTROL_HEADER_NAME: Final = "X-Switchstand-Control"
NATIVE_SELECTION_CONTROL_VALUE: Final = "native-selection-v1"
NATIVE_INPUT_CONTROL_VALUE: Final = "native-input-v1"
NATIVE_STOP_CONTROL_VALUE: Final = "native-stop-v1"
NATIVE_EVIDENCE_CONTROL_VALUE: Final = "native-evidence-v1"


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


def _valid_port(value: str) -> bool:
    return (
        1 <= len(value) <= 5
        and value.isascii()
        and value.isdecimal()
        and int(value) <= 65535
    )


def _loopback_authority(value: str | None) -> bool:
    if not value or not value.isascii() or any(character.isspace() for character in value):
        return False
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        return False

    if value.startswith("["):
        closing_bracket = value.find("]")
        if closing_bracket < 0:
            return False
        hostname = value[1:closing_bracket]
        remainder = value[closing_bracket + 1 :]
        if remainder and (not remainder.startswith(":") or not _valid_port(remainder[1:])):
            return False
        if not hostname or "]" in remainder or any(
            character not in "0123456789abcdefABCDEF:." for character in hostname
        ):
            return False
        try:
            address = ipaddress.ip_address(hostname)
        except ValueError:
            return False
        return isinstance(address, ipaddress.IPv6Address) and address.is_loopback

    if any(character in value for character in "@/?#[]\\"):
        return False
    hostname, separator, port = value.partition(":")
    if separator and (":" in port or not _valid_port(port)):
        return False
    if hostname.lower() == "localhost":
        return True
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        return False
    return isinstance(address, ipaddress.IPv4Address) and address.is_loopback


def is_loopback_host(value: str | None) -> bool:
    """Return whether *value* is one exact, valid loopback HTTP authority."""
    return _loopback_authority(value)


def is_same_origin_http(origin: str | None, host: str | None) -> bool:
    """Require an absent Origin or the exact HTTP origin for a valid Host."""
    if not _loopback_authority(host):
        return False
    if origin is None:
        return True
    return origin == f"http://{host}"
