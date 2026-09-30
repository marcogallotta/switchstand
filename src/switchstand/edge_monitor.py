"""Inert, dependency-injected health monitor for the authenticated MCP edge."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import ClassVar, Protocol

from .wakeful import EventSeverity, WakeEvent, WakefulStore


class EdgeCondition(StrEnum):
    HEALTHY = "healthy"
    TRANSPORT = "transport_failure"
    OAUTH = "oauth_failure"
    PROVIDER = "provider_failure"
    FUNCTIONAL = "functional_failure"
    MISSING_CAPABILITY = "missing_capability"


@dataclass(frozen=True)
class SystemdObservation:
    active: bool
    main_pid: int


@dataclass(frozen=True)
class HttpObservation:
    transport_ok: bool
    status_code: int | None
    valid_auth_challenge: bool = False


@dataclass(frozen=True)
class JournalBatch:
    next_cursor: str
    messages: tuple[str, ...]


class FunctionalStatus(StrEnum):
    OK = "ok"
    PROVIDER_ERROR = "provider_error"
    FAILED = "failed"


@dataclass(frozen=True)
class FunctionalObservation:
    status: FunctionalStatus


@dataclass(frozen=True)
class CanaryTarget:
    identity: str
    work_id: str


class SystemdProbe(Protocol):
    def observe(self) -> SystemdObservation: ...


class JournalProbe(Protocol):
    def read_after(self, cursor: str | None) -> JournalBatch: ...


class HttpProbe(Protocol):
    def observe(self) -> HttpObservation: ...


class FunctionalProbe(Protocol):
    def observe(self, target: CanaryTarget) -> FunctionalObservation: ...


@dataclass(frozen=True)
class MonitorResult:
    condition: EdgeCondition | None
    emitted: bool
    lease_busy: bool = False


def _utc_now() -> datetime:
    return datetime.now(UTC)


class EdgeMonitor:
    """Classify one bounded cycle and durably enqueue sanitized transitions."""

    _OAUTH_MARKERS = (
        "bad_refresh_token",
        "upstream token refresh failed",
    )

    _SUMMARIES: ClassVar[dict[EdgeCondition, str]] = {
        EdgeCondition.HEALTHY: "Authenticated edge checks recovered",
        EdgeCondition.TRANSPORT: "Edge transport is unavailable",
        EdgeCondition.OAUTH: "OAuth token exchange or refresh is failing",
        EdgeCondition.PROVIDER: "Authenticated provider read failed",
        EdgeCondition.FUNCTIONAL: "Authenticated edge read failed",
        EdgeCondition.MISSING_CAPABILITY: "Authenticated canary is not configured",
    }

    def __init__(
        self,
        *,
        monitor_id: str,
        subject: str,
        store: WakefulStore,
        systemd: SystemdProbe,
        journal: JournalProbe,
        http: HttpProbe,
        functional: FunctionalProbe,
        canary: CanaryTarget | None,
        clock: Callable[[], datetime] = _utc_now,
    ):
        self.monitor_id = monitor_id
        self.subject = subject
        self.store = store
        self.systemd = systemd
        self.journal = journal
        self.http = http
        self.functional = functional
        self.canary = canary
        self.clock = clock

    def run_once(self, *, now: datetime | None = None) -> MonitorResult:
        observed_at = now or self.clock()
        lease = self.store.acquire_cycle(monitor=self.monitor_id, now=self.clock())
        if lease is None:
            return MonitorResult(condition=None, emitted=False, lease_busy=True)
        try:
            batch = self.journal.read_after(lease.cursor)
            condition = self._classify(batch)
            event = WakeEvent.create(
                source="switchstand.edge-monitor",
                subject=self.subject,
                kind=(
                    "edge.recovered"
                    if condition is EdgeCondition.HEALTHY
                    else f"edge.{condition.value}"
                ),
                severity=(
                    EventSeverity.INFO
                    if condition is EdgeCondition.HEALTHY
                    else EventSeverity.WARNING
                    if condition is EdgeCondition.MISSING_CAPABILITY
                    else EventSeverity.CRITICAL
                ),
                summary=self._SUMMARIES[condition],
                observed_at=observed_at,
            )
            emitted = self.store.complete_cycle(
                lease=lease,
                cursor=batch.next_cursor,
                condition=condition.value,
                healthy=condition is EdgeCondition.HEALTHY,
                event=event,
                now=self.clock(),
            )
        except BaseException:
            self.store.abandon_cycle(lease)
            raise
        return MonitorResult(condition=condition, emitted=emitted)

    def _classify(self, batch: JournalBatch) -> EdgeCondition:
        service = self.systemd.observe()
        http = self.http.observe()
        if not service.active or service.main_pid <= 0 or not http.transport_ok:
            return EdgeCondition.TRANSPORT

        messages = "\n".join(batch.messages).casefold()
        if any(marker in messages for marker in self._OAUTH_MARKERS):
            return EdgeCondition.OAUTH

        if http.status_code == 401 and http.valid_auth_challenge:
            pass
        elif http.status_code != 200:
            return EdgeCondition.FUNCTIONAL

        if self.canary is None:
            return EdgeCondition.MISSING_CAPABILITY
        functional = self.functional.observe(self.canary)
        if functional.status is FunctionalStatus.PROVIDER_ERROR:
            return EdgeCondition.PROVIDER
        if functional.status is not FunctionalStatus.OK:
            return EdgeCondition.FUNCTIONAL
        return EdgeCondition.HEALTHY
