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


@pytest.mark.parametrize("reject", [False, True])
async def test_exact_resume_without_turn_or_new_thread(tmp_path, reject):
    identity, requests = str(uuid4()), []
    socket = str(tmp_path / "daemon.sock")
    async def daemon(connection):
        async for raw in connection:
            packet = json.loads(raw)
            requests.append(packet)
            if packet["method"] == "initialized":
                continue
            result = {} if packet["method"] == "initialize" else {
                "thread": {"id": identity, "status": {"type": "idle"}}}
            response = {"id": packet["id"], "result": result}
            if reject and packet["method"] == "thread/resume":
                response = {"id": packet["id"], "error": {"message": "missing thread"}}
            await connection.send(json.dumps(response))
    async with unix_serve(daemon, socket):
        if reject:
            with pytest.raises(RuntimeError, match="rejected"):
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
