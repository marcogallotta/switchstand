from datetime import UTC, datetime, timedelta

from switchstand.wakeful import EventSeverity, WakeEvent, WakefulStore

NOW = datetime(2026, 9, 30, 12, 0, tzinfo=UTC)


def event(condition: str, *, at: datetime = NOW) -> WakeEvent:
    return WakeEvent.create(
        source="switchstand.test-monitor",
        subject="switchstand-chatgpt-mcp.service",
        kind=f"edge.{condition}",
        severity=EventSeverity.INFO if condition == "healthy" else EventSeverity.CRITICAL,
        summary="Recovered" if condition == "healthy" else "Failed",
        observed_at=at,
    )


def complete(
    store: WakefulStore,
    *,
    cursor: str,
    condition: str,
    wake_event: WakeEvent | None,
    now: datetime = NOW,
) -> bool:
    lease = store.acquire_cycle(monitor="edge", now=now)
    assert lease is not None
    return store.complete_cycle(
        lease=lease,
        cursor=cursor,
        condition=condition,
        healthy=condition == "healthy",
        event=wake_event,
        now=now,
    )


def test_sqlite_persists_cursor_and_sanitized_outbox_across_instances(tmp_path):
    path = tmp_path / "wakeful.sqlite3"
    first = WakefulStore(path)
    failure = event("oauth_failure")

    assert complete(
        first, cursor="cursor-2", condition="oauth_failure", wake_event=failure
    )

    reopened = WakefulStore(path)
    assert reopened.cursor("edge") == "cursor-2"
    assert reopened.pending() == (failure,)
    payload = path.read_bytes()
    assert b"bad_refresh_token" not in payload
    assert b"authorization" not in payload

    assert reopened.mark_delivered(failure.event_id, delivered_at=NOW + timedelta(seconds=1))
    assert not reopened.mark_delivered(failure.event_id, delivered_at=NOW + timedelta(seconds=2))
    assert reopened.pending() == ()


def test_repeated_failure_is_deduplicated_and_recovery_requires_two_passes(tmp_path):
    store = WakefulStore(tmp_path / "wakeful.sqlite3")
    failure = event("functional_failure")

    assert complete(
        store, cursor="1", condition="functional_failure", wake_event=failure
    )
    assert not complete(
        store,
        cursor="2",
        condition="functional_failure",
        wake_event=event("functional_failure"),
    )
    assert not complete(
        store,
        cursor="3",
        condition="healthy",
        wake_event=event("healthy"),
    )
    recovery = event("healthy", at=NOW + timedelta(seconds=2))
    assert complete(
        store,
        cursor="4",
        condition="healthy",
        wake_event=recovery,
    )

    assert store.pending() == (failure, recovery)


def test_outbox_limit_is_bounded(tmp_path):
    store = WakefulStore(tmp_path / "wakeful.sqlite3")

    for limit in (0, 1001):
        try:
            store.pending(limit=limit)
        except ValueError as exc:
            assert "between 1 and 1000" in str(exc)
        else:
            raise AssertionError("invalid outbox limit was accepted")


def test_cycle_lease_survives_restart_expires_and_rejects_stale_owner(tmp_path):
    path = tmp_path / "wakeful.sqlite3"
    first = WakefulStore(path)
    stale = first.acquire_cycle(monitor="edge", now=NOW, lease_seconds=5)
    assert stale is not None

    restarted = WakefulStore(path)
    assert restarted.acquire_cycle(
        monitor="edge", now=NOW + timedelta(seconds=4), lease_seconds=5
    ) is None
    replacement = restarted.acquire_cycle(
        monitor="edge", now=NOW + timedelta(seconds=5), lease_seconds=5
    )
    assert replacement is not None
    assert replacement.cursor is None

    try:
        first.complete_cycle(
            lease=stale,
            cursor="stale-cursor",
            condition="oauth_failure",
            healthy=False,
            event=event("oauth_failure"),
            now=NOW + timedelta(seconds=5),
        )
    except RuntimeError as exc:
        assert "replaced" in str(exc)
    else:
        raise AssertionError("stale lease changed durable monitor state")

    restarted.complete_cycle(
        lease=replacement,
        cursor="current-cursor",
        condition="oauth_failure",
        healthy=False,
        event=event("oauth_failure"),
        now=NOW + timedelta(seconds=6),
    )
    assert first.cursor("edge") == "current-cursor"
    assert len(first.pending()) == 1
