"""Privacy-safe, request-local timing capture for the MCP edge."""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable, Mapping
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal
from uuid import UUID, uuid4

from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
from fastmcp.tools import ToolResult
from sqlalchemy import event
from sqlalchemy.engine import Connection, ExceptionContext
from sqlalchemy.ext.asyncio import AsyncEngine

LOG = logging.getLogger("uvicorn.error")
TIMING_SCHEMA = "switchstand.mcp_call_timing.v1"
TIMING_PREFIX = "switchstand_mcp_timing "
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
        identity: Mapping[str, str | None],
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
    ) -> None:
        self.identity = identity
        self.emit = emit or self._log

    @staticmethod
    def _log(record: dict[str, object]) -> None:
        LOG.info(
            "%s%s", TIMING_PREFIX,
            json.dumps(record, sort_keys=True, separators=(",", ":")),
        )

    def _safe_emit(
        self, timing: CallTiming, ended: float, status: Literal["ok", "error"],
    ) -> None:
        try:
            self.emit(timing.record(
                ended=ended, status=status, identity=self.identity(),
            ))
        except Exception:  # noqa: BLE001 -- observability must never change tool outcome
            return

    async def on_call_tool(
        self, context: MiddlewareContext[Any], call_next: CallNext[Any, ToolResult],
    ) -> ToolResult:
        fastmcp_context = context.fastmcp_context
        request_id = None if fastmcp_context is None else fastmcp_context.origin_request_id
        timing = CallTiming(
            call_id=str(request_id if request_id is not None else uuid4()),
            tool=str(context.message.name),
            wall_started_at=datetime.now(UTC),
            monotonic_started_at=time.perf_counter(),
        )
        token = _active_call.set(timing)
        status: Literal["ok", "error"] = "error"
        try:
            result = await call_next(context)
            status = "error" if result.is_error else "ok"
            return result
        finally:
            ended = time.perf_counter()
            _active_call.reset(token)
            self._safe_emit(timing, ended, status)


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
