"""Privacy-safe, request-local timing capture for the MCP edge."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Awaitable, Callable, Collection, Mapping
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal
from uuid import UUID, uuid4
from weakref import WeakKeyDictionary

from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
from fastmcp.tools import ToolResult
from sqlalchemy import event, text
from sqlalchemy.engine import Connection, ExceptionContext
from sqlalchemy.ext.asyncio import AsyncEngine

LOG = logging.getLogger("uvicorn.error")
TIMING_SCHEMA = "switchstand.mcp_call_timing.v1"
TIMING_PREFIX = "switchstand_mcp_timing "
TIMING_SCHEMA_GENERATION = "0024_mcp_operation_timings"
_SQL_STACK = "switchstand_observability_sql_stack"


@dataclass(slots=True)
class CallTiming:
    call_id: str
    tool: str
    wall_started_at: datetime
    monotonic_started_at: float
    target_work_id: str | None = None
    spans: list[tuple[float, float]] = field(default_factory=list[tuple[float, float]])

    def add_span(self, started: float, ended: float) -> None:
        self.spans.append((started, max(started, ended)))

    @staticmethod
    def _milliseconds(seconds: float) -> float:
        return round(max(0.0, seconds) * 1000, 3)

    def record(
        self, *, ended: float, status: Literal["ok", "error"],
        identity: Mapping[str, str | None], error_class: str | None = None,
    ) -> dict[str, object]:
        intervals = sorted(self.spans)
        union = 0.0
        if intervals:
            current_start, current_end = intervals[0]
            for started, span_end in intervals[1:]:
                if started <= current_end:
                    current_end = max(current_end, span_end)
                else:
                    union += current_end - current_start
                    current_start, current_end = started, span_end
            union += current_end - current_start

        database = [ended - started for started, ended in self.spans]
        duration = max(0.0, ended - self.monotonic_started_at)
        return {
            "schema": TIMING_SCHEMA,
            "call_id": self.call_id,
            "tool": self.tool,
            "wall_started_at": self.wall_started_at.isoformat(),
            "duration_ms": self._milliseconds(duration),
            "status": status,
            "error_class": error_class,
            "identity": dict(identity),
            "target_work_id": self.target_work_id,
            "db_count": len(database),
            "db_sum_ms": self._milliseconds(sum(database)),
            "db_max_ms": self._milliseconds(max(database, default=0.0)),
            "pool_wait_ms": None,
            "child_union_ms": self._milliseconds(union),
            "server_residual_ms": self._milliseconds(max(0.0, duration - union)),
        }


_active_call: ContextVar[CallTiming | None] = ContextVar(
    "switchstand_active_call_timing", default=None
)


class TimingPersistence:
    """One bounded, non-retrying append path independent of the business result."""

    def __init__(self, engine: AsyncEngine, runtime_generation: str | None) -> None:
        self.engine = engine
        self.runtime_generation = (
            runtime_generation if runtime_generation is not None
            and len(runtime_generation) == 40
            and all(character in "0123456789abcdef" for character in runtime_generation)
            else None
        )
        self.write_failed = False

    async def persist(self, record: Mapping[str, object]) -> None:
        try:
            call_id, tool = str(record["call_id"]), str(record["tool"])
            error_class = record.get("error_class")
            if len(call_id) > 255 or len(tool) > 255 or (
                error_class is not None and len(str(error_class)) > 255
            ):
                raise ValueError("timing identity exceeds retention bound")
            target = record.get("target_work_id")
            values = {
                "call_id": call_id, "tool": tool,
                "target_work_id": None if target is None else UUID(str(target)),
                "started_at": datetime.fromisoformat(str(record["wall_started_at"])),
                "duration_ms": float(str(record["duration_ms"])),
                "status": str(record["status"]),
                "error_class": None if error_class is None else str(error_class),
                "db_count": int(str(record["db_count"])),
                "db_total_ms": float(str(record["db_sum_ms"])),
                "db_max_ms": float(str(record["db_max_ms"])),
                "child_union_ms": float(str(record["child_union_ms"])),
                "server_residual_ms": float(str(record["server_residual_ms"])),
                "runtime_generation": self.runtime_generation,
                "schema_generation": TIMING_SCHEMA_GENERATION,
            }
            async with asyncio.timeout(1):
                async with self.engine.begin() as connection:
                    await connection.execute(text(
                        "INSERT INTO mcp_operation_timings "
                        "(call_id,tool,target_work_id,started_at,duration_ms,status,error_class,"
                        "db_count,db_total_ms,db_max_ms,child_union_ms,server_residual_ms,"
                        "runtime_generation,schema_generation) VALUES "
                        "(:call_id,:tool,:target_work_id,:started_at,:duration_ms,:status,"
                        ":error_class,:db_count,:db_total_ms,:db_max_ms,:child_union_ms,"
                        ":server_residual_ms,:runtime_generation,:schema_generation)"
                    ), values)
        except Exception:  # noqa: BLE001 -- timing must never change the tool result
            self.write_failed = True


_stores: WeakKeyDictionary[AsyncEngine, TimingPersistence] = WeakKeyDictionary()


def register_timing_persistence(
    engine: AsyncEngine, runtime_generation: str | None,
) -> TimingPersistence:
    store = TimingPersistence(engine, runtime_generation)
    _stores[engine] = store
    return store


def timing_persistence(engine: AsyncEngine) -> TimingPersistence | None:
    return _stores.get(engine)


def annotate_target(value: UUID | None) -> None:
    """Attach one exact typed WorkId identified by the ordinary-tool seam."""
    timing = _active_call.get()
    if timing is None:
        return
    timing.target_work_id = None if value is None else str(value)


class CallTimingMiddleware(Middleware):
    """Emit exactly one bounded terminal record for each tool call."""

    def __init__(
        self, identity: Callable[[], Mapping[str, str | None]],
        emit: Callable[[dict[str, object]], None] | None = None,
        persist: Callable[[Mapping[str, object]], Awaitable[None]] | None = None,
        allowed_tools: Collection[str] = (),
    ) -> None:
        self.identity = identity
        self.emit = emit or self._log
        self.persist = persist
        self.allowed_tools = frozenset(allowed_tools)

    @staticmethod
    def _log(record: dict[str, object]) -> None:
        LOG.info(
            "%s%s", TIMING_PREFIX,
            json.dumps(record, sort_keys=True, separators=(",", ":")),
        )

    async def _safe_emit(
        self, timing: CallTiming, ended: float, status: Literal["ok", "error"],
        error_class: str | None,
    ) -> None:
        record = timing.record(ended=ended, status=status, identity={}, error_class=error_class)
        try:
            record["identity"] = dict(self.identity())
            self.emit(record)
        except Exception:  # noqa: BLE001 -- observability must never change tool outcome
            LOG.debug("timing log emission failed")
        if self.persist is not None and str(record["tool"]) in self.allowed_tools:
            try:
                await self.persist(record)
            except Exception:  # noqa: BLE001 -- observability must never change tool outcome
                LOG.debug("timing persistence callback failed")

    async def on_call_tool(
        self, context: MiddlewareContext[Any], call_next: CallNext[Any, ToolResult],
    ) -> ToolResult:
        timing = CallTiming(
            call_id=str(uuid4()),
            tool=str(context.message.name),
            wall_started_at=datetime.now(UTC),
            monotonic_started_at=time.perf_counter(),
        )
        token = _active_call.set(timing)
        status: Literal["ok", "error"] = "error"
        error_class: str | None = None
        try:
            result = await call_next(context)
            status = "error" if result.is_error else "ok"
            error_class = "TOOL_RESULT_ERROR" if result.is_error else None
            return result
        except BaseException:
            error_class = "RAISED_EXCEPTION"
            raise
        finally:
            ended = time.perf_counter()
            _active_call.reset(token)
            await self._safe_emit(timing, ended, status, error_class)


def register_sqlalchemy_timing(engine: AsyncEngine) -> None:
    """Attach active-request-only SQL timing to one async engine."""
    sync_engine = engine.sync_engine

    def before_cursor_execute(
        connection: Connection, _cursor: Any, _statement: str,
        _parameters: Any, _context: Any, _executemany: bool,
    ) -> None:
        timing = _active_call.get()
        if timing is not None:
            connection.info.setdefault(_SQL_STACK, []).append(
                (timing, time.perf_counter())
            )

    def finish(connection: Connection | None) -> None:
        if connection is None:
            return
        stack = connection.info.get(_SQL_STACK)
        if stack:
            timing, started = stack.pop()
            timing.add_span(started, time.perf_counter())

    def after_cursor_execute(
        connection: Connection, _cursor: Any, _statement: str,
        _parameters: Any, _context: Any, _executemany: bool,
    ) -> None:
        finish(connection)

    def handle_error(context: ExceptionContext) -> None:
        finish(context.connection)

    event.listen(sync_engine, "before_cursor_execute", before_cursor_execute)
    event.listen(sync_engine, "after_cursor_execute", after_cursor_execute)
    event.listen(sync_engine, "handle_error", handle_error)
