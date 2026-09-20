import argparse
import asyncio
from uuid import uuid4

import httpx
import pytest

import switchstand.create_canary as canary


def arguments(**changes):
    values = {
        "parent_task": "100", "test_project": "200", "correlation_field": "300",
        "negative_task": "400",
        "database_url": "postgresql+psycopg://switchstand@localhost/switchstand_test",
        "auth_endpoint": "https://switchstand.example/mcp", "auth_token": "token",
        "timeout": 240, "keep_task": False,
    }
    return argparse.Namespace(**(values | changes))


def test_inputs_require_disposable_database_numeric_ids_and_real_auth():
    actual = canary._inputs(arguments())
    assert actual.test_project == "200" and actual.timeout == 240
    for changes, message in (
        ({"parent_task": "production"}, "numeric"),
        ({"database_url": "postgresql+psycopg://localhost/production"}, "switchstand_test"),
        ({"auth_token": ""}, "authenticated edge"),
    ):
        with pytest.raises(canary.Blocked, match=message):
            canary._inputs(arguments(**changes))


async def test_edge_reports_child_startup_failure(monkeypatch):
    class Process:
        returncode = 1
        stderr = asyncio.StreamReader()

        def terminate(self): pass
        def kill(self): pass
        async def wait(self): return 1

    process = Process()
    process.stderr.feed_data(b"causal startup failure")
    process.stderr.feed_eof()
    async def start(*_args, **_kwargs): return process
    monkeypatch.setattr(asyncio, "create_subprocess_exec", start)
    with pytest.raises(canary.Blocked, match="causal startup failure"):
        async with canary._edge(canary._inputs(arguments()), lose=True):
            pass


async def test_failure_evidence_cleans_only_exact_owned_task(monkeypatch):
    operation_id, deleted = uuid4(), []
    monkeypatch.setenv("ASANA_TOKEN", "test")
    async def search(*_args): return ["500"]
    async def task(*_args, **_kwargs):
        return {"parent": {"gid": "100"}, "memberships": [{"project": {"gid": "200"}}],
                "custom_fields": [{"gid": "300", "text_value": str(operation_id)}]}
    class Client:
        async def __aenter__(self): return self
        async def __aexit__(self, *_args): pass
        async def delete(self, path):
            deleted.append(path)
            return httpx.Response(200, request=httpx.Request("DELETE", "https://test" + path))
    monkeypatch.setattr(canary, "_search", search)
    monkeypatch.setattr(canary, "_asana_get", task)
    monkeypatch.setattr(httpx, "AsyncClient", lambda **_kwargs: Client())
    report = {"operation_id": str(operation_id), "possible_provider_effect": True}
    await canary._evidence_cleanup(canary._inputs(arguments()), report)
    assert report["correlated_task_count"] == 1
    assert report["observed_post_count"] is None
    assert report["cleanup"] == "deleted" and deleted == ["/tasks/500"]
