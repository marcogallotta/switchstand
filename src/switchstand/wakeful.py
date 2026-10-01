"""Neutral durable event contract for the inert Wakeful prototype."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from uuid import uuid4


class EventSeverity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


@dataclass(frozen=True)
class WakeEvent:
    """Sanitized, agent-system-neutral event stored for a future dispatcher."""

    schema_version: int
    event_id: str
    observed_at: str
    source: str
    subject: str
    kind: str
    severity: str
    summary: str

    @classmethod
    def create(
        cls,
        *,
        source: str,
        subject: str,
        kind: str,
        severity: EventSeverity,
        summary: str,
        observed_at: datetime,
    ) -> WakeEvent:
        fields = (source, subject, kind, summary)
        if any(not value or len(value) > 200 or "\n" in value for value in fields):
            raise ValueError("event text fields must be single-line and at most 200 characters")
        return cls(
            schema_version=1,
            event_id=str(uuid4()),
            observed_at=observed_at.astimezone(UTC).isoformat(),
            source=source,
            subject=subject,
            kind=kind,
            severity=severity.value,
            summary=summary,
        )


@dataclass(frozen=True)
class CycleLease:
    monitor: str
    owner: str
    cursor: str | None
    expires_at: str


class WakefulStore:
    """SQLite cursor, transition state, and durable delivery outbox."""

    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS monitor_cursor (
                    monitor TEXT PRIMARY KEY,
                    cursor TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS monitor_state (
                    monitor TEXT PRIMARY KEY,
                    condition TEXT NOT NULL,
                    healthy_streak INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS monitor_lease (
                    monitor TEXT PRIMARY KEY,
                    owner TEXT NOT NULL,
                    expires_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS wake_outbox (
                    event_id TEXT PRIMARY KEY,
                    created_at TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    delivered_at TEXT
                );
                """
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def cursor(self, monitor: str) -> str | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT cursor FROM monitor_cursor WHERE monitor = ?", (monitor,)
            ).fetchone()
        return None if row is None else str(row[0])

    @staticmethod
    def _timestamp(value: datetime) -> str:
        return value.astimezone(UTC).isoformat()

    def acquire_cycle(
        self,
        *,
        monitor: str,
        now: datetime,
        lease_seconds: int = 30,
    ) -> CycleLease | None:
        """Acquire the single-writer lease and return its cursor snapshot."""
        if not 1 <= lease_seconds <= 300:
            raise ValueError("lease_seconds must be between 1 and 300")
        acquired = self._timestamp(now)
        expires = self._timestamp(now + timedelta(seconds=lease_seconds))
        owner = str(uuid4())
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            lease_row = connection.execute(
                "SELECT expires_at FROM monitor_lease WHERE monitor = ?", (monitor,)
            ).fetchone()
            if lease_row is not None and str(lease_row[0]) > acquired:
                connection.rollback()
                return None
            connection.execute(
                "INSERT INTO monitor_lease(monitor, owner, expires_at) VALUES (?, ?, ?) "
                "ON CONFLICT(monitor) DO UPDATE SET "
                "owner = excluded.owner, expires_at = excluded.expires_at",
                (monitor, owner, expires),
            )
            cursor_row = connection.execute(
                "SELECT cursor FROM monitor_cursor WHERE monitor = ?", (monitor,)
            ).fetchone()
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()
        cursor = None if cursor_row is None else str(cursor_row[0])
        return CycleLease(monitor=monitor, owner=owner, cursor=cursor, expires_at=expires)

    def complete_cycle(
        self,
        *,
        lease: CycleLease,
        cursor: str,
        condition: str,
        healthy: bool,
        event: WakeEvent | None,
        now: datetime,
    ) -> bool:
        """Advance the cursor and enqueue only a durable condition transition."""
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            lease_row = connection.execute(
                "SELECT owner, expires_at FROM monitor_lease WHERE monitor = ?",
                (lease.monitor,),
            ).fetchone()
            current = self._timestamp(now)
            if (
                lease_row is None
                or str(lease_row[0]) != lease.owner
                or str(lease_row[1]) <= current
            ):
                connection.rollback()
                raise RuntimeError("monitor cycle lease is absent, replaced, or expired")
            row = connection.execute(
                "SELECT condition, healthy_streak FROM monitor_state WHERE monitor = ?",
                (lease.monitor,),
            ).fetchone()
            previous = None if row is None else str(row[0])
            healthy_streak = 0 if row is None else int(row[1])
            should_emit = False

            if healthy and previous not in (None, condition):
                healthy_streak += 1
                if healthy_streak >= 2:
                    previous = condition
                    healthy_streak = 0
                    should_emit = True
            elif healthy:
                previous = condition
                healthy_streak = 0
            else:
                should_emit = condition != previous
                previous = condition
                healthy_streak = 0

            connection.execute(
                "INSERT INTO monitor_cursor(monitor, cursor) VALUES (?, ?) "
                "ON CONFLICT(monitor) DO UPDATE SET cursor = excluded.cursor",
                (lease.monitor, cursor),
            )
            connection.execute(
                "INSERT INTO monitor_state(monitor, condition, healthy_streak) VALUES (?, ?, ?) "
                "ON CONFLICT(monitor) DO UPDATE SET "
                "condition = excluded.condition, healthy_streak = excluded.healthy_streak",
                (lease.monitor, previous, healthy_streak),
            )
            if should_emit:
                if event is None:
                    raise ValueError("condition transition requires an event")
                payload = json.dumps(asdict(event), sort_keys=True, separators=(",", ":"))
                connection.execute(
                    "INSERT INTO wake_outbox(event_id, created_at, payload) VALUES (?, ?, ?)",
                    (event.event_id, event.observed_at, payload),
                )
            connection.execute(
                "DELETE FROM monitor_lease WHERE monitor = ? AND owner = ?",
                (lease.monitor, lease.owner),
            )
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()
        return should_emit

    def abandon_cycle(self, lease: CycleLease) -> None:
        """Release this owner's lease without disturbing a replacement owner."""
        with self._connect() as connection:
            connection.execute(
                "DELETE FROM monitor_lease WHERE monitor = ? AND owner = ?",
                (lease.monitor, lease.owner),
            )

    def pending(self, *, limit: int = 100) -> tuple[WakeEvent, ...]:
        if not 1 <= limit <= 1000:
            raise ValueError("limit must be between 1 and 1000")
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT payload FROM wake_outbox WHERE delivered_at IS NULL "
                "ORDER BY created_at, event_id LIMIT ?",
                (limit,),
            ).fetchall()
        return tuple(WakeEvent(**json.loads(str(row[0]))) for row in rows)

    def mark_delivered(self, event_id: str, *, delivered_at: datetime) -> bool:
        with self._connect() as connection:
            result = connection.execute(
                "UPDATE wake_outbox SET delivered_at = ? "
                "WHERE event_id = ? AND delivered_at IS NULL",
                (delivered_at.astimezone(UTC).isoformat(), event_id),
            )
        return result.rowcount == 1
