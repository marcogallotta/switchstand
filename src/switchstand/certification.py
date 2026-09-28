"""Supervisor for the isolated ordinary-MCP native-certification profile."""
# pyright: reportPrivateUsage=false

from __future__ import annotations

import asyncio
import fcntl
import json
import os
import secrets
import signal
import socket
import subprocess
import sys
from collections.abc import Awaitable, Generator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, cast

import httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from .chatgpt_edge import CERTIFICATION_RUNTIME_PATH, _https_resource_url
from .database import validate_test_database_url

MARKER = "SWITCHSTAND_CERTIFICATION_OWNER_V1"
CERTIFICATION_PROJECT = "1218535890201656"
CERTIFICATION_RESOURCE = "https://laptop.tail46f0b9.ts.net:8446/mcp"
PAGE_LIMIT, TASK_LIMIT = 100, 500
JSON = dict[str, Any]


def _required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} is required")
    return value


def _owned(notes: object, marker: str) -> bool:
    return isinstance(notes, str) and marker in notes.splitlines()


@contextmanager
def certification_lock(state: Path | None = None) -> Generator[None]:
    """Exclude every certification-project inventory and mutation runner."""
    directory = state or Path.home() / ".local/state/switchstand/certification"
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    with (directory / "supervisor.lock").open("a+") as lock:
        os.chmod(lock.name, 0o600)
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("another native certification run is active") from None
        yield


class FixtureCleaner:
    def __init__(self, client: httpx.AsyncClient, project: str, marker: str) -> None:
        if project != CERTIFICATION_PROJECT:
            raise RuntimeError("certification project does not match the pinned isolated project")
        self.client, self.project, self.marker = client, project, marker

    async def _rows(
        self, path: str, params: dict[str, str | int], *, missing_ok: bool = False,
    ) -> list[JSON]:
        rows: list[JSON] = []
        offset: str | None = None
        seen: set[str] = set()
        while True:
            page_params = params | ({"offset": offset} if offset else {})
            response = await self.client.get(path, params=page_params)
            if response.status_code == 404 and missing_ok:
                return rows
            response.raise_for_status()
            payload = cast(JSON, response.json())
            raw_rows = payload.get("data")
            if not isinstance(raw_rows, list):
                raise TypeError("certification project inventory is invalid")
            page = cast(list[object], raw_rows)
            if len(page) > PAGE_LIMIT:
                raise RuntimeError("certification project inventory is invalid")
            for raw in page:
                if not isinstance(raw, dict):
                    raise TypeError("certification project inventory is invalid")
                rows.append(cast(JSON, raw))
            next_page = payload.get("next_page")
            if next_page is None:
                return rows
            next_offset = (
                cast(JSON, next_page).get("offset") if isinstance(next_page, dict) else None
            )
            if not isinstance(next_offset, str) or not next_offset or next_offset in seen:
                raise RuntimeError("certification project pagination is invalid")
            seen.add(next_offset)
            offset = next_offset

    async def inventory(self, *, missing_ok: bool = False) -> tuple[tuple[str, ...], tuple[str, ...]]:
        roots = await self._rows("/tasks", {
            "project": self.project, "completed_since": "1970-01-01T00:00:00Z",
            "limit": PAGE_LIMIT, "opt_fields": "gid,notes",
        })
        queue = [(row, 0) for row in roots]
        tasks: dict[str, tuple[bool, int]] = {}
        while queue:
            row, depth = queue.pop(0)
            gid = row.get("gid")
            if not isinstance(gid, str) or not gid or depth > 9:
                raise RuntimeError("certification task tree is invalid")
            ownership = _owned(row.get("notes"), self.marker)
            previous = tasks.get(gid)
            if previous:
                if previous[0] != ownership:
                    raise RuntimeError("certification task identity changed during inventory")
                if previous[1] >= depth:
                    continue
            tasks[gid] = (ownership, depth)
            if len(tasks) > TASK_LIMIT:
                raise RuntimeError("certification task tree exceeds cleanup limit")
            children = await self._rows(f"/tasks/{gid}/subtasks", {
                "limit": PAGE_LIMIT, "opt_fields": "gid,notes",
            }, missing_ok=missing_ok)
            queue.extend((child, depth + 1) for child in children)
        owned = tuple(gid for gid, details in sorted(
            tasks.items(), key=lambda item: (-item[1][1], item[0])
        ) if details[0])
        foreign = tuple(sorted(gid for gid, (is_owned, _) in tasks.items() if not is_owned))
        return owned, foreign

    async def clean(self) -> None:
        owned, foreign = await self.inventory()
        if foreign:
            raise RuntimeError("unexpected unowned certification tasks: " + ",".join(foreign))
        for gid in owned:
            response = await self.client.delete(f"/tasks/{gid}")
            if response.status_code != 404:
                response.raise_for_status()
            for attempt in range(20):
                readback = await self.client.get(f"/tasks/{gid}")
                if readback.status_code == 404:
                    break
                readback.raise_for_status()
                if attempt == 19:
                    raise RuntimeError(f"certification cleanup residue: {gid}")
                await asyncio.sleep(0.5)
        residue: tuple[str, ...] = ()
        foreign = ()
        for attempt in range(20):
            residue, foreign = await self.inventory(missing_ok=True)
            if not residue and not foreign:
                return
            if attempt < 19:
                await asyncio.sleep(0.5)
        raise RuntimeError("certification cleanup residue: " + ",".join(residue + foreign))


async def _reset_database(url: str) -> None:
    validate_test_database_url(url)
    engine = create_async_engine(url, isolation_level="AUTOCOMMIT")
    try:
        async with engine.connect() as connection:
            await connection.execute(text("DROP SCHEMA IF EXISTS public CASCADE"))
            await connection.execute(text("CREATE SCHEMA public"))
    finally:
        await engine.dispose()


def _run(command: list[str], root: Path, environment: dict[str, str] | None = None) -> str:
    return subprocess.run(
        command, cwd=root, env=environment, check=True, text=True, capture_output=True,
    ).stdout.strip()


def _verify_runtime(root: Path, expected: str) -> None:
    if not (root / "AGENTS.md").is_file() or not (root / "src/switchstand").is_dir():
        raise RuntimeError("certification repository is not a Switchstand checkout")
    actual = _run(["git", "rev-parse", "HEAD"], root)
    if actual != expected:
        raise RuntimeError(f"certification runtime mismatch: expected {expected}, got {actual}")
    if _run(["git", "status", "--porcelain"], root):
        raise RuntimeError("certification repository must be clean")
    if root not in Path(__file__).resolve().parents:
        raise RuntimeError("certification module is not loaded from the pinned repository")


def verify_certification_runtime(root: Path, expected: str) -> None:
    """Verify that support code is executing from the clean exact candidate."""
    _verify_runtime(root, expected)


def _verify_serve_mapping(root: Path, resource: str, port: int) -> None:
    try:
        status = json.loads(_run(["tailscale", "serve", "status", "--json"], root))
    except (json.JSONDecodeError, subprocess.CalledProcessError) as exc:
        raise RuntimeError("cannot read certification Tailscale Serve mapping") from exc
    authority = resource.removeprefix("https://").removesuffix("/mcp")
    expected_proxy = f"http://127.0.0.1:{port}"
    try:
        https = status["TCP"][str(int(authority.rsplit(":", 1)[1]))]["HTTPS"]
        proxy = status["Web"][authority]["Handlers"]["/"]["Proxy"]
    except (KeyError, TypeError, ValueError, IndexError) as exc:
        raise RuntimeError("certification Tailscale Serve mapping is missing") from exc
    if https is not True or proxy != expected_proxy:
        raise RuntimeError(
            f"certification Tailscale Serve mapping mismatch: expected {authority} -> "
            f"{expected_proxy}"
        )


async def _ready(
    child: asyncio.subprocess.Process, port: int, resource: str, runtime_sha: str, run_id: str,
) -> None:
    metadata = "/.well-known/oauth-protected-resource/mcp"
    bases = (f"http://127.0.0.1:{port}", resource.removesuffix("/mcp"))
    expected_runtime = {"runtime_sha": runtime_sha, "run_id": run_id}
    deadline = asyncio.get_running_loop().time() + 10
    async with httpx.AsyncClient(trust_env=False, timeout=0.2) as client:
        while asyncio.get_running_loop().time() < deadline:
            if child.returncode is not None:
                raise RuntimeError(
                    f"certification edge exited before readiness: {child.returncode}"
                )
            try:
                for base in bases:
                    metadata_response = await client.get(base + metadata)
                    metadata_payload = metadata_response.json()
                    if (metadata_response.status_code != 200
                            or not isinstance(metadata_payload, dict)
                            or cast(JSON, metadata_payload).get("resource") != resource):
                        break
                    runtime_response = await client.get(base + CERTIFICATION_RUNTIME_PATH)
                    runtime_payload = runtime_response.json()
                    if (runtime_response.status_code == 200
                            and isinstance(runtime_payload, dict)
                            and runtime_payload != expected_runtime):
                        raise RuntimeError(
                            "certification route reached a different runtime binding"
                        )
                    if runtime_response.status_code != 200 or runtime_payload != expected_runtime:
                        break
                else:
                    return
            except RuntimeError:
                raise
            except (OSError, httpx.HTTPError, ValueError):
                pass
            await asyncio.sleep(0.1)
    raise RuntimeError("certification edge readiness timed out")


async def _stop_child(child: asyncio.subprocess.Process) -> None:
    if child.returncode is not None:
        await child.wait()
        return
    child.terminate()
    try:
        await asyncio.wait_for(child.wait(), 5)
    except TimeoutError:
        child.kill()
        await child.wait()


async def _supervise_edge(
    root: Path, environment: dict[str, str], port: int, resource: str,
) -> None:
    loop = asyncio.get_running_loop()
    stop, restart = asyncio.Event(), asyncio.Event()
    loop.add_signal_handler(signal.SIGHUP, restart.set)
    for signum in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(signum, stop.set)
    try:
        while not stop.is_set():
            child = await asyncio.create_subprocess_exec(
                sys.executable, "-m", "switchstand.chatgpt_edge", cwd=root, env=environment,
            )
            try:
                await _ready(
                    child, port, resource,
                    environment["SWITCHSTAND_CERTIFICATION_RUNTIME_SHA"],
                    environment["SWITCHSTAND_CERTIFICATION_RUN_ID"],
                )
                print(f"CERTIFICATION_EDGE_READY CHILD_PID={child.pid}", flush=True)
                waits = (asyncio.create_task(child.wait()), asyncio.create_task(stop.wait()),
                         asyncio.create_task(restart.wait()))
                done, pending = await asyncio.wait(waits, return_when=asyncio.FIRST_COMPLETED)
                for task in pending:
                    task.cancel()
                await asyncio.gather(
                    *(cast(Awaitable[object], task) for task in pending),
                    return_exceptions=True,
                )
                if waits[0] in done:
                    raise RuntimeError(
                        f"certification edge exited unexpectedly: {child.returncode}"
                    )
                restart.clear()
            finally:
                await _stop_child(child)
    finally:
        for signum in (signal.SIGHUP, signal.SIGINT, signal.SIGTERM):
            loop.remove_signal_handler(signum)


def _preflight_port(port: int) -> None:
    with socket.socket() as listener:
        try:
            listener.bind(("127.0.0.1", port))
        except OSError as exc:
            raise RuntimeError(f"certification port {port} is already in use") from exc


async def run() -> None:
    project = _required("SWITCHSTAND_TEST_PROJECT_GID")
    marker = _required("SWITCHSTAND_CERTIFICATION_OWNERSHIP_MARKER")
    if not marker.startswith(MARKER + ":") or len(marker) < 64:
        raise RuntimeError("certification ownership marker is invalid")
    expected = _required("SWITCHSTAND_CERTIFICATION_RUNTIME_SHA")
    root = Path(_required("SWITCHSTAND_CERTIFICATION_REPO")).resolve()
    fastmcp_home = Path(_required("FASTMCP_HOME")).resolve()
    state = Path.home() / ".local/state/switchstand/certification"
    if fastmcp_home != (state / "fastmcp").resolve() or root in fastmcp_home.parents:
        raise RuntimeError("certification FASTMCP_HOME must be a dedicated certification path")
    url = _required("DATABASE_URL")
    validate_test_database_url(url)
    resource = _https_resource_url(_required("SWITCHSTAND_CERTIFICATION_RESOURCE_URL"))
    if resource != CERTIFICATION_RESOURCE:
        raise RuntimeError("certification resource does not match the pinned isolated origin")
    port = _required("SWITCHSTAND_CERTIFICATION_BIND_PORT")
    if port != "8798":
        raise RuntimeError("certification bind port must be the dedicated port 8798")
    _preflight_port(int(port))
    _verify_runtime(root, expected)
    environment = dict(os.environ) | {
        "PYTHONPATH": str(root / "src"),
        "SWITCHSTAND_MCP_RESOURCE_URL": resource,
        "SWITCHSTAND_MCP_GITHUB_CLIENT_ID": _required(
            "SWITCHSTAND_CERTIFICATION_GITHUB_CLIENT_ID"
        ),
        "SWITCHSTAND_MCP_GITHUB_CLIENT_SECRET": _required(
            "SWITCHSTAND_CERTIFICATION_GITHUB_CLIENT_SECRET"
        ),
        "SWITCHSTAND_MCP_GITHUB_USER_ID": _required(
            "SWITCHSTAND_CERTIFICATION_GITHUB_USER_ID"
        ),
        "SWITCHSTAND_MCP_BIND_HOST": "127.0.0.1",
        "SWITCHSTAND_MCP_BIND_PORT": port,
        "SWITCHSTAND_CERTIFICATION_FIXTURE_MARKER": marker,
        "SWITCHSTAND_CERTIFICATION_RUNTIME_SHA": expected,
        "SWITCHSTAND_CERTIFICATION_RUN_ID": secrets.token_hex(32),
    }
    state.mkdir(mode=0o700, parents=True, exist_ok=True)
    with certification_lock(state):
        token = _required("ASANA_TOKEN")
        client = httpx.AsyncClient(
            base_url="https://app.asana.com/api/1.0", trust_env=False,
            headers={"Authorization": f"Bearer {token}"},
        )
        cleaner = FixtureCleaner(client, project, marker)
        failures: list[str] = []
        try:
            _verify_serve_mapping(root, resource, int(port))
            await cleaner.clean()
        except Exception:
            await client.aclose()
            raise
        try:
            await _reset_database(url)
            _run([sys.executable, "-m", "alembic", "upgrade", "head"], root,
                 environment | {"DATABASE_URL": url})
            print(f"CERTIFICATION_RUNTIME_SHA={expected} SUPERVISOR_PID={os.getpid()}", flush=True)
            await _supervise_edge(root, environment, int(port), resource)
        finally:
            try:
                await cleaner.clean()
            except Exception as exc:  # noqa: BLE001 - both cleanup domains must run
                failures.append(f"Asana: {exc}")
            try:
                await _reset_database(url)
            except Exception as exc:  # noqa: BLE001 - preserve the other cleanup result
                failures.append(f"database: {exc}")
            await client.aclose()
            if failures:
                raise RuntimeError("CLEANUP INCOMPLETE — " + "; ".join(failures))
            print(
                f"CERTIFICATION_CLEANUP_COMPLETE project={project} database=switchstand_test",
                flush=True,
            )


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
