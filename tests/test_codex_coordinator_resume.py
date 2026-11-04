"""Exercise the startup helper over a real Unix websocket protocol boundary."""
import importlib.machinery
import importlib.util
import json
from pathlib import Path
from uuid import uuid4

import pytest
from websockets.asyncio.server import unix_serve

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/codex-coordinator-resume"
loader = importlib.machinery.SourceFileLoader("coordinator_resume", str(SCRIPT))
spec = importlib.util.spec_from_loader(loader.name, loader)
module = importlib.util.module_from_spec(spec)
loader.exec_module(module)


@pytest.mark.parametrize("mode", ["ok", "rejected", "missing", "cwd", "danger", "roots", "extra", "identity"])
async def test_exact_resume_without_turn_or_new_thread(tmp_path, mode):
    identity, requests = str(uuid4()), []
    socket = str(tmp_path / "daemon.sock")
    async def daemon(connection):
        async for raw in connection:
            packet = json.loads(raw)
            requests.append(packet)
            if packet["method"] == "initialized":
                continue
            result = {} if packet["method"] == "initialize" else {
                "thread": {"id": identity, "status": {"type": "idle"}},
                "cwd": "/home/marco/switchstand-recovery/coordinator",
                "sandbox": {"type": "workspaceWrite", "writableRoots": [
                    "/home/marco/switchstand/.git",
                    "/home/marco/.local/state/switchstand/worktrees"]}}
            if packet["method"] == "thread/resume":
                if mode == "missing":
                    result.pop("sandbox")
                elif mode == "cwd":
                    result["cwd"] = "/home/marco/switchstand"
                elif mode == "danger":
                    result["sandbox"]["type"] = "dangerFullAccess"
                elif mode == "roots":
                    result["sandbox"]["writableRoots"].pop()
                elif mode == "extra":
                    result["sandbox"]["writableRoots"].append("/home/marco")
                elif mode == "identity":
                    result["thread"]["id"] = str(uuid4())
            response = {"id": packet["id"], "result": result}
            if mode == "rejected" and packet["method"] == "thread/resume":
                response = {"id": packet["id"], "error": {"message": "missing thread"}}
            await connection.send(json.dumps(response))
    async with unix_serve(daemon, socket):
        if mode != "ok":
            with pytest.raises(RuntimeError):
                await module.resume(socket, identity)
        else:
            await module.resume(socket, identity)
    assert [p["method"] for p in requests] == ["initialize", "initialized", "thread/resume"]
    params = requests[-1]["params"]
    assert params["threadId"] == identity and params["excludeTurns"] is True
    assert params["cwd"] == "/home/marco/switchstand-recovery/coordinator"
    assert params["sandbox"] == "workspace-write"
    assert params["config"]["sandbox_workspace_write"]["writable_roots"] == [
        "/home/marco/switchstand/.git", "/home/marco/.local/state/switchstand/worktrees"]
