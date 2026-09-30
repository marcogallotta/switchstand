from dataclasses import dataclass
from datetime import UTC, datetime
from threading import Barrier, Thread

import pytest

from switchstand.edge_monitor import (
    CanaryTarget,
    EdgeCondition,
    EdgeMonitor,
    FunctionalObservation,
    FunctionalStatus,
    HttpObservation,
    JournalBatch,
    SystemdObservation,
)
from switchstand.wakeful import WakefulStore

NOW = datetime(2026, 9, 30, 12, 0, tzinfo=UTC)
LIVE_SYSTEMD = SystemdObservation(True, 1234)
HTTP_OK = HttpObservation(True, 200)
EMPTY_JOURNAL = JournalBatch("cursor-new", ())
FIXED_CANARY = CanaryTarget("fixed-read-only-principal", "fixed-work-id")


@dataclass
class FixedSystemd:
    observation: SystemdObservation

    def observe(self):
        return self.observation


@dataclass
class FixedHttp:
    observation: HttpObservation

    def observe(self):
        return self.observation


class FixedJournal:
    def __init__(self, batch: JournalBatch):
        self.batch = batch
        self.after = []

    def read_after(self, cursor):
        self.after.append(cursor)
        return self.batch


class FixedFunctional:
    def __init__(self, status: FunctionalStatus):
        self.status = status
        self.targets = []

    def observe(self, target):
        self.targets.append(target)
        return FunctionalObservation(self.status)


def monitor(
    tmp_path,
    *,
    systemd=LIVE_SYSTEMD,
    http=HTTP_OK,
    ingress=None,
    journal=EMPTY_JOURNAL,
    functional=FunctionalStatus.OK,
    canary=FIXED_CANARY,
):
    journal_probe = FixedJournal(journal)
    functional_probe = FixedFunctional(functional)
    instance = EdgeMonitor(
        monitor_id="chatgpt-edge",
        subject="switchstand-chatgpt-mcp.service",
        store=WakefulStore(tmp_path / "wakeful.sqlite3"),
        systemd=FixedSystemd(systemd),
        journal=journal_probe,
        http=FixedHttp(http),
        ingress=None if ingress is None else FixedHttp(ingress),
        functional=functional_probe,
        canary=canary,
        clock=lambda: NOW,
    )
    return instance, journal_probe, functional_probe


def test_live_pid_and_http_200_with_bad_refresh_token_is_oauth_critical(tmp_path):
    instance, _, _ = monitor(
        tmp_path,
        journal=JournalBatch(
            "cursor-current",
            (
                "POST /token 401",
                "TokenError error=bad_refresh_token Upstream token refresh failed",
            ),
        ),
    )

    result = instance.run_once(now=NOW)

    assert result.condition is EdgeCondition.OAUTH
    assert result.emitted
    queued = instance.store.pending()
    assert len(queued) == 1
    assert queued[0].kind == "edge.oauth_failure"
    assert queued[0].severity == "critical"
    assert "bad_refresh_token" not in repr(queued[0])


def test_historical_docker_crash_before_cursor_does_not_fire(tmp_path):
    instance, journal, _ = monitor(tmp_path)
    lease = instance.store.acquire_cycle(monitor="chatgpt-edge", now=NOW)
    assert lease is not None
    instance.store.complete_cycle(
        lease=lease,
        cursor="after-ipaddress-crash",
        condition=EdgeCondition.HEALTHY.value,
        healthy=True,
        event=None,
        now=NOW,
    )

    result = instance.run_once(now=NOW)

    assert journal.after == ["after-ipaddress-crash"]
    assert result.condition is EdgeCondition.HEALTHY
    assert not result.emitted
    assert instance.store.pending() == ()


def test_expected_unauthenticated_401_challenge_does_not_fire(tmp_path):
    instance, _, functional = monitor(
        tmp_path,
        http=HttpObservation(True, 401, valid_auth_challenge=True),
    )

    result = instance.run_once(now=NOW)

    assert result.condition is EdgeCondition.HEALTHY
    assert not result.emitted
    assert functional.targets == [FIXED_CANARY]


@pytest.mark.parametrize(
    ("systemd", "http", "functional", "expected"),
    [
        (
            SystemdObservation(False, 0),
            HttpObservation(False, None),
            FunctionalStatus.OK,
            EdgeCondition.TRANSPORT,
        ),
        (
            SystemdObservation(True, 1234),
            HttpObservation(True, 200),
            FunctionalStatus.PROVIDER_ERROR,
            EdgeCondition.PROVIDER,
        ),
        (
            SystemdObservation(True, 1234),
            HttpObservation(True, 200),
            FunctionalStatus.FAILED,
            EdgeCondition.FUNCTIONAL,
        ),
        (
            SystemdObservation(True, 1234),
            HttpObservation(True, 503),
            FunctionalStatus.OK,
            EdgeCondition.FUNCTIONAL,
        ),
    ],
)
def test_failure_classes_remain_distinct(tmp_path, systemd, http, functional, expected):
    instance, _, _ = monitor(
        tmp_path, systemd=systemd, http=http, functional=functional
    )

    result = instance.run_once(now=NOW)

    assert result.condition is expected
    assert result.emitted


def test_external_ingress_failure_is_distinct_from_healthy_local_edge(tmp_path):
    instance, _, functional = monitor(
        tmp_path,
        ingress=HttpObservation(False, None),
    )

    result = instance.run_once(now=NOW)

    assert result.condition is EdgeCondition.SHARED_INGRESS
    assert result.emitted
    assert functional.targets == []
    event = instance.store.pending()[0]
    assert event.kind == "edge.shared_ingress_failure"
    assert event.summary == "Public shared ingress is unavailable"


def test_local_semantic_failure_precedes_and_skips_failed_external_ingress(tmp_path):
    class FailedIngress:
        calls = 0

        def observe(self):
            self.calls += 1
            return HttpObservation(False, None)

    ingress = FailedIngress()
    instance, _, functional = monitor(
        tmp_path,
        http=HttpObservation(True, 503),
    )
    instance.ingress = ingress

    result = instance.run_once(now=NOW)

    assert result.condition is EdgeCondition.FUNCTIONAL
    assert result.emitted
    assert ingress.calls == 0
    assert functional.targets == []


def test_oauth_failure_precedes_local_semantics_and_external_ingress(tmp_path):
    class FailedIngress:
        calls = 0

        def observe(self):
            self.calls += 1
            return HttpObservation(False, None)

    ingress = FailedIngress()
    instance, _, functional = monitor(
        tmp_path,
        http=HttpObservation(True, 503),
        journal=JournalBatch("cursor-current", ("bad_refresh_token",)),
    )
    instance.ingress = ingress

    result = instance.run_once(now=NOW)

    assert result.condition is EdgeCondition.OAUTH
    assert result.emitted
    assert ingress.calls == 0
    assert functional.targets == []


def test_missing_fixed_canary_is_missing_capability_without_probe(tmp_path):
    instance, _, functional = monitor(tmp_path, canary=None)

    result = instance.run_once(now=NOW)

    assert result.condition is EdgeCondition.MISSING_CAPABILITY
    assert result.emitted
    assert functional.targets == []
    event = instance.store.pending()[0]
    assert event.kind == "edge.missing_capability"
    assert event.severity == "warning"


def test_concurrent_cycles_have_one_writer_and_cannot_regress_cursor(tmp_path):
    entered = Barrier(2)
    release = Barrier(2)

    class BlockingJournal:
        def __init__(self):
            self.calls = []

        def read_after(self, cursor):
            self.calls.append(cursor)
            entered.wait(timeout=2)
            release.wait(timeout=2)
            return JournalBatch(
                "cursor-current",
                ("POST /token 401 bad_refresh_token",),
            )

    journal = BlockingJournal()
    store = WakefulStore(tmp_path / "wakeful.sqlite3")
    instance = EdgeMonitor(
        monitor_id="chatgpt-edge",
        subject="switchstand-chatgpt-mcp.service",
        store=store,
        systemd=FixedSystemd(LIVE_SYSTEMD),
        journal=journal,
        http=FixedHttp(HTTP_OK),
        ingress=None,
        functional=FixedFunctional(FunctionalStatus.OK),
        canary=FIXED_CANARY,
        clock=lambda: NOW,
    )
    results = []

    thread = Thread(target=lambda: results.append(instance.run_once(now=NOW)))
    thread.start()
    entered.wait(timeout=2)
    results.append(instance.run_once(now=NOW))
    release.wait(timeout=2)
    thread.join(timeout=2)

    assert not thread.is_alive()
    assert sum(result.emitted for result in results) == 1
    assert sum(result.lease_busy for result in results) == 1
    assert journal.calls == [None]
    assert store.cursor("chatgpt-edge") == "cursor-current"
    assert len(store.pending()) == 1
