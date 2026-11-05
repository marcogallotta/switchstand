"""Privacy-safe, request-local timing capture for the MCP edge."""

from __future__ import annotations

import asyncio
import json
import logging
import sys
import time
from collections.abc import Awaitable, Callable, Collection, Mapping
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal, cast
from uuid import UUID, uuid4
from weakref import WeakKeyDictionary

from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
from fastmcp.tools import ToolResult
from mcp.server.context import CallNext as SDKCallNext
from mcp.server.context import ServerRequestContext
from pydantic import BaseModel
from sqlalchemy import event, text
from sqlalchemy.engine import Connection, ExceptionContext
from sqlalchemy.ext.asyncio import AsyncEngine

from .failure_journal import redact_mcp_content as redact

LOG = logging.getLogger("uvicorn.error")
TIMING_SCHEMA = "switchstand.mcp_call_timing.v1"
TIMING_PREFIX = "switchstand_mcp_timing "
TIMING_SCHEMA_GENERATION = "0026_agent_message_ack"
_SQL_STACK = "switchstand_observability_sql_stack"
TEXT_LIMIT = 2000


def capture_content(value: Any) -> dict[str, object]:
    """Copy wire content, with visible partial coverage on traversal exhaustion."""
    nodes, remaining, partial = 256, 16384, False

    def copy(item: Any, depth: int = 0) -> Any:
        nonlocal nodes, remaining, partial
        nodes -= 1
        if nodes < 0 or depth > 32 or remaining < 0:
            partial = True
            return "[CAPTURE_LIMIT]"
        if isinstance(item, BaseModel):
            item = item.model_dump(mode="json", by_alias=True)
        if isinstance(item, str):
            text = redact(item)
            if len(text) > TEXT_LIMIT:
                text = text[:TEXT_LIMIT] + f"[TRUNCATED original_chars={len(item)}]"
            remaining -= len(json.dumps(text, ensure_ascii=False).encode("utf-8"))
            if remaining < 0:
                partial = True
                return "[CAPTURE_LIMIT]"
            return text
        if isinstance(item, Mapping):
            result: dict[str, Any] = {}
            for key, child in cast(Mapping[str, Any], item).items():
                if nodes <= 0 or remaining < 0:
                    partial = True
                    result["[CAPTURE_LIMIT]"] = "remaining fields not captured"
                    break
                name = copy(key, depth + 1)
                # Use the canonical credential syntax owner for secret-key context too.
                secret = redact(f'{key}="credential"') != f'{key}="credential"'
                result[name] = "[redacted]" if secret else copy(child, depth + 1)
            return result
        if isinstance(item, (list, tuple)):
            children: list[Any] = []
            for child in cast(list[Any], item):
                if nodes <= 0 or remaining < 0:
                    partial = True
                    children.append("[CAPTURE_LIMIT]")
                    break
                children.append(copy(child, depth + 1))
            return children
        if item is None or isinstance(item, (bool, int, float)):
            return item
        partial = True
        return "[UNSUPPORTED_CONTENT]"

    try:
        copied = copy(value)
        return {"value": copied, "coverage": "PARTIAL" if partial else "CAPTURED"}
    except Exception:  # noqa: BLE001 -- failed capture must preserve the response
        return {"value": "[CAPTURE_FAILED]", "coverage": "UNKNOWN"}


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


class _RequestDiagnosticFilter(logging.Filter):
    """Keep dependency failure identity without rejected inputs or tracebacks."""

    def filter(self, record: logging.LogRecord) -> bool:
        if _active_call.get() is not None:
            kind = record.exc_info[0] if record.exc_info else None
            record.msg = "MCP request diagnostic: %s"
            record.args = (kind.__name__ if kind else "TOOL_DIAGNOSTIC",)
            record.exc_info = record.exc_text = record.stack_info = None
        return True


def _protect_request_diagnostics() -> None:
    for name in ("fastmcp.server.server", "mcp.server.mcpserver.server"):
        logger = logging.getLogger(name)
        if not any(isinstance(item, _RequestDiagnosticFilter) for item in logger.filters):
            logger.addFilter(_RequestDiagnosticFilter())


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
        _protect_request_diagnostics()
        self.identity = identity
        self.emit = emit or self._log
        self.persist = persist
        self.allowed_tools = frozenset(allowed_tools)

    @staticmethod
    def _log(record: dict[str, object]) -> None:
        LOG.info(
            "%s%s", TIMING_PREFIX,
            json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=False),
        )

    @staticmethod
    def emit_stderr(record: dict[str, object]) -> None:
        # SDK Rich handlers wrap JSON across physical lines; retain one raw stderr record.
        sys.stderr.write(TIMING_PREFIX + json.dumps(record, ensure_ascii=False,
                                                   separators=(",", ":")) + "\n")

    async def _safe_emit(
        self, timing: CallTiming, ended: float, status: Literal["ok", "error"],
        error_class: str | None,
        audit: dict[str, object],
    ) -> None:
        record = timing.record(ended=ended, status=status, identity={}, error_class=error_class)
        try:
            record["identity"] = dict(self.identity())
            record.update(audit)
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
        return await self._capture(
            context.message.name, context.message.arguments,
            lambda: call_next(context),
        )

    async def capture_request(self, context: ServerRequestContext[Any, Any], call_next: SDKCallNext) -> Any:
        if context.method != "tools/call":
            return await call_next(context)
        params = context.params or {}
        return await self._capture(params.get("name", "UNKNOWN"), params.get("arguments"),
                                   lambda: call_next(context))

    async def _capture(self, tool: Any, arguments: Any, invoke: Callable[[], Awaitable[Any]]) -> Any:
        safe_tool = capture_content(tool)["value"]
        timing = CallTiming(
            call_id=str(uuid4()),
            tool=safe_tool if isinstance(safe_tool, str) else "UNKNOWN",
            wall_started_at=datetime.now(UTC),
            monotonic_started_at=time.perf_counter(),
        )
        token = _active_call.set(timing)
        status: Literal["ok", "error"] = "error"
        error_class: str | None = None
        audit: dict[str, object] = {"input": capture_content(arguments), "output": None}
        try:
            result = await invoke()
            failed = bool(cast(Mapping[str, Any], result).get("isError", False)) if isinstance(
                result, Mapping
            ) else bool(getattr(result, "is_error", False))
            status = "error" if failed else "ok"
            error_class = "TOOL_RESULT_ERROR" if failed else None
            audit["output"] = capture_content(result)
            return cast(Any, result)
        except BaseException as error:
            error_class = "RAISED_EXCEPTION"
            audit["exception_type"] = type(error).__name__
            raise
        finally:
            ended = time.perf_counter()
            _active_call.reset(token)
            await self._safe_emit(timing, ended, status, error_class, audit)


def install_call_audit(server: Any, *, stdio: bool = True, **options: Any) -> None:
    """Install once at the SDK wire seam; diagnostics use stderr, never STDIO stdout."""
    options.setdefault("identity", dict)
    if stdio:
        options.setdefault("emit", CallTimingMiddleware.emit_stderr)
    if not any(getattr(item, "__self__", None).__class__ is CallTimingMiddleware
               for item in server.middleware):
        server.middleware.insert(0, CallTimingMiddleware(**options).capture_request)


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
