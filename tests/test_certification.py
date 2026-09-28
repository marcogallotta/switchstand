import os
import signal
import subprocess
import tomllib
from asyncio import Event
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from switchstand.certification import (
    CERTIFICATION_PROJECT,
    CERTIFICATION_RESOURCE,
    FixtureCleaner,
    _owned,
    _ready,
    _reset_database,
    _supervise_edge,
    certification_lock,
    _verify_serve_mapping,
    run,
)

PROJECT = CERTIFICATION_PROJECT
MARKER = "SWITCHSTAND_CERTIFICATION_OWNER_V1:" + "a" * 64


def test_certification_marker_is_exact():
    assert _owned(f"notes\n{MARKER}", MARKER)
    assert not _owned(MARKER + "x", MARKER)
    assert not _owned(MARKER, MARKER + "x")


def test_certification_lock_excludes_competing_project_runner(tmp_path: Path):
    with (
        certification_lock(tmp_path),
        pytest.raises(RuntimeError, match="another native certification run is active"),
        certification_lock(tmp_path),
    ):
        pytest.fail("competing certification lock was admitted")


async def test_cleanup_is_paginated_owned_only_and_read_back():
    deleted: list[str] = []
    project_queries: list[dict[str, str]] = []
    present = {"root", "child", "grandchild"}
    stale = True

    def api(request: httpx.Request) -> httpx.Response:
        nonlocal stale
        if request.method == "DELETE":
            gid = request.url.path.rsplit("/", 1)[-1]
            deleted.append(gid)
            present.remove(gid)
            return httpx.Response(200, json={"data": {}})
        if request.url.path.startswith("/api/1.0/tasks/") and not request.url.path.endswith("subtasks"):
            return httpx.Response(404, json={})
        if request.url.path == "/api/1.0/tasks/root/subtasks":
            return httpx.Response(200, json={
                "data": ([{"gid": "child", "notes": MARKER}] if "child" in present else []),
                "next_page": None,
            })
        if request.url.path == "/api/1.0/tasks/child/subtasks":
            rows = [{"gid": "grandchild", "notes": MARKER}] if "grandchild" in present else []
            return httpx.Response(200, json={"data": rows, "next_page": None})
        if request.url.path == "/api/1.0/tasks/grandchild/subtasks":
            return httpx.Response(200, json={"data": [], "next_page": None})
        project_queries.append(dict(request.url.params))
        if not present:
            rows = ([{"gid": "root", "notes": MARKER}] if stale else [])
            stale = False
            return httpx.Response(200, json={"data": rows, "next_page": None})
        offset = request.url.params.get("offset")
        return httpx.Response(200, json={
            "data": [{"gid": "child" if offset else "root", "notes": MARKER}],
            "next_page": None if offset else {"offset": "page-2"},
        })

    async with httpx.AsyncClient(
        base_url="https://app.asana.com/api/1.0", transport=httpx.MockTransport(api),
    ) as client:
        await FixtureCleaner(client, PROJECT, MARKER).clean()
    assert deleted == ["grandchild", "child", "root"]
    assert all(query["completed_since"] == "1970-01-01T00:00:00Z"
               for query in project_queries)


async def test_cleanup_rejects_unowned_residue_after_owned_cleanup():
    def api(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/subtasks"):
            return httpx.Response(200, json={"data": [], "next_page": None})
        return httpx.Response(200, json={
            "data": [{"gid": "unexpected", "notes": "not ours"}], "next_page": None,
        })

    async with httpx.AsyncClient(
        base_url="https://app.asana.com/api/1.0", transport=httpx.MockTransport(api),
    ) as client:
        with pytest.raises(RuntimeError, match="unexpected unowned.*unexpected"):
            await FixtureCleaner(client, PROJECT, MARKER).clean()


async def test_preclean_fails_closed_on_missing_subtask_inventory():
    def api(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/subtasks"):
            return httpx.Response(404)
        return httpx.Response(200, json={
            "data": [{"gid": "owned", "notes": MARKER}], "next_page": None,
        })

    async with httpx.AsyncClient(
        base_url="https://app.asana.com/api/1.0", transport=httpx.MockTransport(api),
    ) as client:
        with pytest.raises(httpx.HTTPStatusError):
            await FixtureCleaner(client, PROJECT, MARKER).clean()


@pytest.mark.parametrize("url", [
    "postgresql+psycopg:///switchstand_test?dbname=production",
    "sqlite+aiosqlite:///switchstand_test",
])
async def test_database_reset_rejects_alias_or_wrong_driver_before_connect(url):
    with pytest.raises(ValueError, match="switchstand_test"):
        await _reset_database(url)


def test_certification_alias_matches_ordinary_tool_policy():
    config = tomllib.loads((Path(__file__).parents[1] / ".codex/config.toml").read_text())
    servers = config["mcp_servers"]
    ordinary, certification = servers["switchstand"], servers["switchstand_certification"]
    assert certification["enabled"] is False
    assert certification["enabled_tools"] == ordinary["enabled_tools"]
    assert certification["default_tools_approval_mode"] == "approve"


def test_certification_client_uses_only_alias_and_strips_secrets(tmp_path):
    root = Path(__file__).parents[1]
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake = fake_bin / "codex"
    fake.write_text("#!/bin/sh\nenv\nprintf 'ARG=%s\\n' \"$@\"\n")
    fake.chmod(0o755)
    fake_git = fake_bin / "git"
    fake_git.write_text(f"#!/bin/sh\nprintf '%s\\n' '{root}'\n")
    fake_git.chmod(0o755)
    result = subprocess.run(
        [str(root / "scripts/switchstand-native-certification-client"), "resume"],
        cwd=root, check=True, capture_output=True, text=True,
        env=os.environ | {
            "PATH": f"{fake_bin}:/usr/bin:/bin",
            "SWITCHSTAND_CERTIFICATION_RESOURCE_URL": (
                "https://laptop.tail46f0b9.ts.net:8446/mcp"
            ),
            "ASANA_TOKEN": "must-not-leak", "DATABASE_URL": "must-not-leak",
        },
    ).stdout
    assert "must-not-leak" not in result
    assert "mcp_servers.switchstand.enabled=false" in result
    assert "mcp_servers.switchstand_managed.enabled=false" in result
    assert "mcp_servers.switchstand_development.enabled=false" in result
    assert "mcp_servers.switchstand_certification.enabled=true" in result
    assert "resume" in result


async def test_supervisor_restarts_child_then_reaps_on_stop(monkeypatch, tmp_path: Path):
    callbacks: dict[signal.Signals, object] = {}
    loop = __import__("asyncio").get_running_loop()
    monkeypatch.setattr(loop, "add_signal_handler", lambda sig, fn: callbacks.setdefault(sig, fn))
    monkeypatch.setattr(loop, "remove_signal_handler", lambda sig: callbacks.pop(sig, None))

    class Child:
        def __init__(self, pid: int) -> None:
            self.pid, self.returncode, self.done = pid, None, Event()

        async def wait(self):
            await self.done.wait()
            return self.returncode

        def terminate(self):
            self.returncode = 0
            self.done.set()

        kill = terminate

    children: list[Child] = []

    async def spawn(*_args, **_kwargs):
        child = Child(len(children) + 1)
        children.append(child)
        return child

    async def ready(_child, _port, _resource, _runtime_sha, _run_id):
        callback = callbacks[signal.SIGHUP if len(children) == 1 else signal.SIGTERM]
        callback()  # type: ignore[operator]

    monkeypatch.setattr("switchstand.certification.asyncio.create_subprocess_exec", spawn)
    monkeypatch.setattr("switchstand.certification._ready", ready)
    environment = {
        "SWITCHSTAND_CERTIFICATION_RUNTIME_SHA": "a" * 40,
        "SWITCHSTAND_CERTIFICATION_RUN_ID": "run-1",
    }
    await _supervise_edge(tmp_path, environment, 8797, CERTIFICATION_RESOURCE)
    assert len(children) == 2
    assert all(child.returncode == 0 for child in children)

    async def not_ready(*_args):
        raise RuntimeError("readiness failed")

    monkeypatch.setattr("switchstand.certification._ready", not_ready)
    with pytest.raises(RuntimeError, match="readiness failed"):
        await _supervise_edge(tmp_path, environment, 8797, CERTIFICATION_RESOURCE)
    assert children[-1].returncode == 0


async def test_readiness_requires_local_and_external_exact_metadata(monkeypatch):
    seen: list[str] = []
    runtime = {"runtime_sha": "a" * 40, "run_id": "run-1"}

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            pass

        async def get(self, url):
            seen.append(url)
            payload = runtime if url.endswith("switchstand-certification-runtime") else {
                "resource": CERTIFICATION_RESOURCE
            }
            return SimpleNamespace(status_code=200, json=lambda: payload)

    monkeypatch.setattr("switchstand.certification.httpx.AsyncClient", lambda **_: Client())
    await _ready(
        SimpleNamespace(returncode=None), 8798, CERTIFICATION_RESOURCE, "a" * 40, "run-1"
    )  # type: ignore[arg-type]
    assert seen == [
        "http://127.0.0.1:8798/.well-known/oauth-protected-resource/mcp",
        "http://127.0.0.1:8798/.well-known/switchstand-certification-runtime",
        "https://laptop.tail46f0b9.ts.net:8446/.well-known/oauth-protected-resource/mcp",
        "https://laptop.tail46f0b9.ts.net:8446/.well-known/switchstand-certification-runtime",
    ]


async def test_readiness_rejects_stale_external_runtime(monkeypatch):
    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            pass

        async def get(self, url):
            if url.endswith("oauth-protected-resource/mcp"):
                payload = {"resource": CERTIFICATION_RESOURCE}
            else:
                payload = {"runtime_sha": "a" * 40, "run_id": "run-1"}
                if url.startswith("https://"):
                    payload["run_id"] = "stale-run"
            return SimpleNamespace(status_code=200, json=lambda: payload)

    monkeypatch.setattr("switchstand.certification.httpx.AsyncClient", lambda **_: Client())
    with pytest.raises(RuntimeError, match="different runtime binding"):
        await _ready(
            SimpleNamespace(returncode=None), 8798, CERTIFICATION_RESOURCE,
            "a" * 40, "run-1",
        )  # type: ignore[arg-type]


def test_serve_mapping_requires_exact_external_route(monkeypatch, tmp_path):
    status = {
        "TCP": {"8446": {"HTTPS": True}},
        "Web": {
            "laptop.tail46f0b9.ts.net:8446": {
                "Handlers": {"/": {"Proxy": "http://127.0.0.1:8798"}}
            }
        },
    }
    monkeypatch.setattr(
        "switchstand.certification._run", lambda *_args, **_kwargs: __import__("json").dumps(status)
    )
    _verify_serve_mapping(tmp_path, CERTIFICATION_RESOURCE, 8798)
    status["Web"]["laptop.tail46f0b9.ts.net:8446"]["Handlers"]["/"]["Proxy"] = (
        "http://127.0.0.1:8799"
    )
    with pytest.raises(RuntimeError, match="mapping mismatch"):
        _verify_serve_mapping(tmp_path, CERTIFICATION_RESOURCE, 8798)


async def test_run_orders_preclean_before_database_and_final_cleanup(monkeypatch, tmp_path: Path):
    events: list[str] = []
    values = {
        "SWITCHSTAND_TEST_PROJECT_GID": PROJECT,
        "SWITCHSTAND_CERTIFICATION_OWNERSHIP_MARKER": MARKER,
        "SWITCHSTAND_CERTIFICATION_RUNTIME_SHA": "a" * 40,
        "SWITCHSTAND_CERTIFICATION_REPO": str(tmp_path),
        "FASTMCP_HOME": str(Path.home() / ".local/state/switchstand/certification/fastmcp"),
        "DATABASE_URL": "postgresql+psycopg:///switchstand_test",
        "SWITCHSTAND_CERTIFICATION_RESOURCE_URL": CERTIFICATION_RESOURCE,
        "SWITCHSTAND_CERTIFICATION_BIND_PORT": "8798",
        "SWITCHSTAND_CERTIFICATION_GITHUB_CLIENT_ID": "client",
        "SWITCHSTAND_CERTIFICATION_GITHUB_CLIENT_SECRET": "secret",
        "SWITCHSTAND_CERTIFICATION_GITHUB_USER_ID": "1234",
        "ASANA_TOKEN": "token",
    }
    monkeypatch.setattr("switchstand.certification._required", values.__getitem__)
    monkeypatch.setattr("switchstand.certification._verify_runtime", lambda *_: None)
    monkeypatch.setattr("switchstand.certification._verify_serve_mapping", lambda *_: None)
    monkeypatch.setattr("switchstand.certification._preflight_port", lambda *_: None)
    monkeypatch.setattr("switchstand.certification.validate_test_database_url", lambda url: url)

    class Client:
        async def aclose(self):
            events.append("close")

    class Cleaner:
        mode, calls = "", 0

        def __init__(self, *_args):
            pass

        async def clean(self):
            Cleaner.calls += 1
            events.append("clean")
            if Cleaner.mode == "pre" or (Cleaner.mode == "post" and Cleaner.calls == 2):
                raise RuntimeError("preclean failed")

    async def reset(_url):
        events.append("reset")

    async def supervise(*_args):
        events.append("supervise")

    monkeypatch.setattr("switchstand.certification.httpx.AsyncClient", lambda **_: Client())
    monkeypatch.setattr("switchstand.certification.FixtureCleaner", Cleaner)
    monkeypatch.setattr("switchstand.certification._reset_database", reset)
    monkeypatch.setattr("switchstand.certification._run", lambda *_args, **_kwargs: events.append("migrate") or "")
    monkeypatch.setattr("switchstand.certification._supervise_edge", supervise)
    await run()
    assert events == ["clean", "reset", "migrate", "supervise", "clean", "reset", "close"]
    events.clear()
    Cleaner.mode, Cleaner.calls = "pre", 0
    with pytest.raises(RuntimeError, match="preclean failed"):
        await run()
    assert events == ["clean", "close"]
    events.clear()
    Cleaner.mode, Cleaner.calls = "post", 0
    with pytest.raises(RuntimeError, match="CLEANUP INCOMPLETE"):
        await run()
    assert events == ["clean", "reset", "migrate", "supervise", "clean", "reset", "close"]
    events.clear()
    monkeypatch.setattr(
        "switchstand.certification.fcntl.flock",
        lambda *_: (_ for _ in ()).throw(BlockingIOError()),
    )
    with pytest.raises(RuntimeError, match="another native certification run"):
        await run()
    assert not events
