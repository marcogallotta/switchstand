"""Exact-current-target native input callable with fixed privacy-safe results."""
from __future__ import annotations

import math
import time
from types import MappingProxyType
from typing import Any, Callable, Mapping, Protocol, cast

from .native_contracts import NativeInputResult
from .native_turns import MAX_TURN_ID_CHARACTERS, project_exact_turn_list


INPUT_VERSION = "native-input-v1"
MAX_INPUT_BYTES = 16 * 1024
_REQUEST_FIELDS = frozenset({"version", "observationRunRef", "agentRef", "text"})
_NOT_SENT = {"code": "input_unavailable", "outcome": "not_sent"}


class ResolveCurrentTarget(Protocol):
    """The exact-current-target port owned by the B1 observation boundary."""

    def __call__(
        self,
        selection: Mapping[str, str],
        *,
        now: float,
        maximum_observation_age_seconds: float,
    ) -> object: ...


class TargetTransport(Protocol):
    """Bounded transport operations over an opaque exact-current-target handle."""

    def turns_list(
        self,
        target: object,
        *,
        max_response_bytes: int,
        timeout_seconds: float,
    ) -> tuple[str, Mapping[str, Any] | None]:
        """Return content-free newest-turn evidence rebound to *target*."""
        ...

    def turn_start(
        self,
        target: object,
        text: str,
        *,
        max_response_bytes: int,
        timeout_seconds: float,
    ) -> tuple[str, Mapping[str, Any] | None]: ...

    def turn_steer(
        self,
        target: object,
        expected_turn_id: str,
        text: str,
        *,
        max_response_bytes: int,
        timeout_seconds: float,
    ) -> tuple[str, Mapping[str, Any] | None]: ...


def validate_native_input_request(value: Any) -> bool:
    """Validate the closed request before resolution or transport activity."""
    if not isinstance(value, Mapping) or set(value) != _REQUEST_FIELDS:
        return False
    if value.get("version") != INPUT_VERSION:
        return False
    observation_run_ref = value.get("observationRunRef")
    agent_ref = value.get("agentRef")
    text = value.get("text")
    if (
        type(observation_run_ref) is not str
        or not observation_run_ref
        or type(agent_ref) is not str
        or not agent_ref
        or type(text) is not str
        or not text
        or text.isspace()
    ):
        return False
    try:
        encoded = text.encode("utf-8", errors="strict")
    except UnicodeError:
        return False
    if len(encoded) > MAX_INPUT_BYTES:
        return False
    return True


def _valid_turn_id(value: Any) -> bool:
    return type(value) is str and bool(value) and len(value) <= MAX_TURN_ID_CHARACTERS


def _start_acknowledged(response: Any) -> bool:
    if not isinstance(response, Mapping) or set(response) != {"turn"}:
        return False
    turn = response.get("turn")
    return isinstance(turn, Mapping) and _valid_turn_id(turn.get("id"))


def _steer_acknowledged(response: Any, expected_turn_id: str) -> bool:
    return (
        isinstance(response, Mapping)
        and set(response) == {"turnId"}
        and response.get("turnId") == expected_turn_id
    )


class NativeInput:
    """Send input only to one twice-resolved opaque current target."""

    def __init__(
        self,
        resolve_current_target: ResolveCurrentTarget,
        transport: TargetTransport,
        *,
        maximum_observation_age_seconds: float,
        clock: Callable[[], float] = time.time,
        timeout_seconds: float = 3.0,
        max_response_bytes: int = 256 * 1024,
    ) -> None:
        if (
            isinstance(maximum_observation_age_seconds, bool)
            or not isinstance(maximum_observation_age_seconds, (int, float))
            or not math.isfinite(maximum_observation_age_seconds)
            or maximum_observation_age_seconds < 0
        ):
            raise ValueError("maximum observation age must be finite and non-negative")
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not math.isfinite(timeout_seconds)
            or timeout_seconds <= 0
        ):
            raise ValueError("timeout must be finite and positive")
        if type(max_response_bytes) is not int or max_response_bytes <= 0:
            raise ValueError("response byte cap must be positive")
        self._resolve_current_target = resolve_current_target
        self._transport = transport
        self._maximum_observation_age_seconds = float(maximum_observation_age_seconds)
        self._clock = clock
        self._timeout_seconds = float(timeout_seconds)
        self._max_response_bytes = max_response_bytes

    def _resolve(self, selection: Mapping[str, str]) -> object | None:
        try:
            target = self._resolve_current_target(
                selection,
                now=self._clock(),
                maximum_observation_age_seconds=self._maximum_observation_age_seconds,
            )
        except Exception:
            return None
        if target is None or isinstance(target, Mapping):
            return None
        return target

    def send(self, request: Any) -> NativeInputResult:
        """Consume one closed native-input-v1 request and return one closed result."""
        try:
            validated = validate_native_input_request(request)
        except Exception:
            validated = False
        if not validated:
            return cast(NativeInputResult, dict(_NOT_SENT))
        fields = cast(Mapping[str, str], request)
        text = fields["text"]
        selection = MappingProxyType({
            "observationRunRef": fields["observationRunRef"],
            "agentRef": fields["agentRef"],
        })
        first_target = self._resolve(selection)
        if first_target is None:
            return cast(NativeInputResult, dict(_NOT_SENT))
        try:
            classification, response = self._transport.turns_list(
                first_target,
                max_response_bytes=self._max_response_bytes,
                timeout_seconds=self._timeout_seconds,
            )
        except Exception:
            return cast(NativeInputResult, dict(_NOT_SENT))
        try:
            projection = (
                project_exact_turn_list(response, first_target)
                if classification == "ok" and response is not None
                else None
            )
        except Exception:
            projection = None
        response = None
        if projection is None:
            return cast(NativeInputResult, dict(_NOT_SENT))
        second_target = self._resolve(selection)
        try:
            targets_match = second_target is not None and second_target == first_target
        except Exception:
            targets_match = False
        if not targets_match:
            return cast(NativeInputResult, dict(_NOT_SENT))
        try:
            if projection.status != "inProgress":
                classification, acknowledgement = self._transport.turn_start(
                    second_target,
                    text,
                    max_response_bytes=self._max_response_bytes,
                    timeout_seconds=self._timeout_seconds,
                )
                acknowledged = classification == "ok" and _start_acknowledged(acknowledgement)
                mode = "start"
            else:
                expected_turn_id = projection.turn_id
                if not _valid_turn_id(expected_turn_id):
                    return cast(NativeInputResult, dict(_NOT_SENT))
                expected_turn_id = cast(str, expected_turn_id)
                classification, acknowledgement = self._transport.turn_steer(
                    second_target,
                    expected_turn_id,
                    text,
                    max_response_bytes=self._max_response_bytes,
                    timeout_seconds=self._timeout_seconds,
                )
                acknowledged = classification == "ok" and _steer_acknowledged(
                    acknowledgement, expected_turn_id
                )
                mode = "steer"
        except Exception:
            return cast(NativeInputResult, dict(_NOT_SENT))
        acknowledgement = None
        if not acknowledged:
            return cast(NativeInputResult, dict(_NOT_SENT))
        return {"code": "input_sent", "outcome": "sent", "mode": mode}
