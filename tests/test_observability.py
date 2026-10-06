from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID

import pytest
from fastmcp.server.middleware import MiddlewareContext
from fastmcp.tools import ToolResult
from mcp_types import CallToolRequestParams
from sqlalchemy import create_engine

from switchstand import observability
from switchstand.observability import (
    CallTiming,
    CallTimingMiddleware,
    annotate_target,
    register_sqlalchemy_timing,
)


def context(tool: str, request_id: str = "request-1") -> MiddlewareContext[Any]:
    return MiddlewareContext(
        message=CallToolRequestParams(name=tool, arguments={"secret": "do-not-log"}),
        fastmcp_context=cast(Any, SimpleNamespace(origin_request_id=request_id)),
        method="tools/call",
    )


def sql_listeners(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    listeners: dict[str, Any] = {}
    monkeypatch.setattr(
        observability.event, "listen",
        lambda _engine, name, callback: listeners.__setitem__(name, callback),
    )
    register_sqlalchemy_timing(SimpleNamespace(sync_engine=object()))  # type: ignore[arg-type]
    return listeners


async def test_success_error_result_and_exception_each_emit_one_redacted_record() -> None:
    records: list[dict[str, object]] = []
    middleware = CallTimingMiddleware(
        lambda: {"subject": "safe-subject"}, records.append,
    )

    async def succeed(context: MiddlewareContext[Any]) -> ToolResult:
        del context
        return ToolResult(structured_content={"ok": True})

    async def error_result(context: MiddlewareContext[Any]) -> ToolResult:
        del context
        return ToolResult(structured_content={"ok": False}, is_error=True)

    async def fail(context: MiddlewareContext[Any]) -> ToolResult:
        del context
        raise ValueError("secret exception")

    await middleware.on_call_tool(context("success", "1"), succeed)
    await middleware.on_call_tool(context("error-result", "2"), error_result)
    with pytest.raises(ValueError, match="secret exception"):
        await middleware.on_call_tool(context("exception", "3"), fail)

    assert [(record["tool"], record["status"]) for record in records] == [
        ("success", "ok"), ("error-result", "error"), ("exception", "error"),
    ]
    assert all(str(UUID(str(record["call_id"]))) == record["call_id"] for record in records)
    assert [record["error_class"] for record in records] == [None, "TOOL_RESULT_ERROR", "RAISED_EXCEPTION"]
    assert all(record["schema"] == observability.TIMING_SCHEMA for record in records)
    assert all(record["identity"] == {"subject": "safe-subject"} for record in records)
    assert set(records[0]) == {
        "schema", "call_id", "tool", "wall_started_at", "duration_ms", "status",
        "error_class",
        "identity", "target_work_id", "db_count", "db_sum_ms", "db_max_ms", "pool_wait_ms",
        "child_union_ms", "server_residual_ms",
    }
    assert "do-not-log" not in json.dumps(records)
    assert "secret exception" not in json.dumps(records)


async def test_emission_failures_do_not_suppress_persistence_or_change_result() -> None:
    persisted: list[dict[str, object]] = []
    async def persist(record: Any) -> None:
        persisted.append(dict(record))
    def broken(*_args: object) -> Any:
        raise RuntimeError("unavailable")

    expected = ToolResult(structured_content={"ok": True})
    async def succeed(context: MiddlewareContext[Any]) -> ToolResult:
        del context
        return expected

    for identity, emit in ((broken, persisted.append), (dict, broken)):
        middleware = CallTimingMiddleware(identity, emit, persist, {"safe"})
        assert await middleware.on_call_tool(context("safe"), succeed) is expected
    assert len(persisted) == 2


def test_default_emitter_uses_stable_prefix_and_compact_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    emitted: list[tuple[str, str, str]] = []

    def capture(template: str, prefix: str, payload: str) -> None:
        emitted.append((template, prefix, payload))

    monkeypatch.setattr(
        observability.LOG, "info", capture,
    )

    CallTimingMiddleware(dict).emit({
        "schema": observability.TIMING_SCHEMA, "status": "ok",
    })

    assert emitted == [(
        "%s%s", observability.TIMING_PREFIX,
        '{"schema":"switchstand.mcp_call_timing.v1","status":"ok"}',
    )]


def test_overlapping_child_intervals_use_union_not_category_sums() -> None:
    timing = CallTiming("call", "tool", datetime.now(UTC), 10.0)
    timing.add_span(11.0, 14.0)
    timing.add_span(12.0, 15.0)
    timing.add_span(16.0, 17.0)

    record = timing.record(ended=20.0, status="ok", identity={})

    assert record["db_count"] == 3
    assert record["db_sum_ms"] == 7000.0
    assert record["db_max_ms"] == 3000.0
    assert record["pool_wait_ms"] is None
    assert record["child_union_ms"] == 5000.0
    assert record["server_residual_ms"] == 5000.0


async def test_concurrent_calls_do_not_cross_contaminate_child_spans(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    records: list[dict[str, object]] = []
    middleware = CallTimingMiddleware(dict, records.append)
    listeners = sql_listeners(monkeypatch)

    async def run(tool: str, target: str) -> None:
        async def measured(context: MiddlewareContext[Any]) -> ToolResult:
            del context
            annotate_target(UUID(target) if target == work_id else None)
            connection = SimpleNamespace(info={})
            listeners["before_cursor_execute"](connection, None, "", None, None, False)
            await asyncio.sleep(0)
            listeners["after_cursor_execute"](connection, None, "", None, None, False)
            return ToolResult(structured_content={"ok": True})

        await middleware.on_call_tool(context(tool, tool), measured)

    work_id = "10000000-0000-4000-8000-000000000001"
    await asyncio.gather(run("first", work_id), run("second", "unrelated"))

    by_tool = {str(record["tool"]): record for record in records}
    assert set(by_tool) == {"first", "second"}
    assert by_tool["first"]["db_count"] == 1
    assert by_tool["second"]["db_count"] == 1
    assert by_tool["first"]["target_work_id"] == work_id
    assert by_tool["second"]["target_work_id"] is None


async def test_sqlalchemy_hooks_aggregate_nested_operations_without_query_data(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    listeners = sql_listeners(monkeypatch)
    assert set(listeners) == {"before_cursor_execute", "after_cursor_execute", "handle_error"}

    records: list[dict[str, object]] = []
    middleware = CallTimingMiddleware(dict, records.append)
    connection = SimpleNamespace(info={})

    async def queries(context: MiddlewareContext[Any]) -> ToolResult:
        del context
        before = listeners["before_cursor_execute"]
        after = listeners["after_cursor_execute"]
        before(connection, None, "SELECT secret", {"token": "secret"}, None, False)
        before(connection, None, "SELECT nested", None, None, False)
        after(connection, None, "SELECT nested", None, None, False)
        after(connection, None, "SELECT secret", {"token": "secret"}, None, False)
        before(connection, None, "SELECT failing", None, None, False)
        listeners["handle_error"](SimpleNamespace(connection=connection))
        return ToolResult(structured_content={"ok": True})

    await middleware.on_call_tool(context("database"), queries)

    assert records[0]["db_count"] == 3
    serialized = json.dumps(records[0])
    assert "SELECT" not in serialized and "token" not in serialized


async def test_sqlalchemy_listener_contract_records_a_real_execution() -> None:
    engine = create_engine("sqlite://")
    register_sqlalchemy_timing(SimpleNamespace(sync_engine=engine))  # type: ignore[arg-type]
    records: list[dict[str, object]] = []
    middleware = CallTimingMiddleware(dict, records.append)

    async def query(context: MiddlewareContext[Any]) -> ToolResult:
        del context
        with engine.connect() as connection:
            connection.exec_driver_sql("SELECT 1")
        return ToolResult(structured_content={"ok": True})

    try:
        await middleware.on_call_tool(context("database"), query)
    finally:
        engine.dispose()

    assert records[0]["db_count"] == 1
