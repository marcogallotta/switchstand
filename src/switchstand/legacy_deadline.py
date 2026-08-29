"""Legacy external-wait deadlines and closed phase outcomes."""
from __future__ import annotations

from dataclasses import dataclass
import math
import time
from typing import Any, Callable, Literal, Mapping


DEFAULT_STARTUP_DEADLINE_SECONDS = 10.0
DEFAULT_OPERATION_DEADLINE_SECONDS = 5.0
MAXIMUM_DEADLINE_SECONDS = 300.0

PhaseDisposition = Literal["not_sent", "acknowledged", "rejected", "ambiguous"]
NotificationDisposition = Literal["not_sent", "sent", "ambiguous"]


class LegacyDeadlineExceeded(RuntimeError):
    """The outer legacy operation expired before its first durable acceptance."""


class PersistenceUnavailable(RuntimeError):
    """The process-lifetime persistence failure latch is set."""


@dataclass(frozen=True)
class LegacyDeadline:
    cutoff: float
    clock: Callable[[], float] = time.monotonic

    @classmethod
    def after(
        cls,
        seconds: float,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> LegacyDeadline:
        return cls(clock() + seconds, clock)

    def remaining(self) -> float:
        return max(0.0, self.cutoff - self.clock())

    def expired(self) -> bool:
        return self.remaining() <= 0.0


@dataclass(frozen=True)
class PhaseResult:
    disposition: PhaseDisposition
    phase: str
    result: Mapping[str, Any] | None = None
    code: str | None = None


@dataclass(frozen=True)
class SetupResult:
    client: Any | None
    phase: PhaseResult
    initialized: NotificationDisposition


def parse_deadline_seconds(raw: object, *, option: str) -> float:
    """Parse one required, finite legacy deadline without accepting bools."""
    if isinstance(raw, bool) or not isinstance(raw, (str, int, float)):
        raise ValueError(f"{option} must be a number greater than 0 and at most 300")
    if isinstance(raw, str) and not raw.strip():
        raise ValueError(f"{option} must be a number greater than 0 and at most 300")
    try:
        value = float(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{option} must be a number greater than 0 and at most 300") from exc
    if not math.isfinite(value) or value <= 0.0 or value > MAXIMUM_DEADLINE_SECONDS:
        raise ValueError(f"{option} must be a number greater than 0 and at most 300")
    return value
