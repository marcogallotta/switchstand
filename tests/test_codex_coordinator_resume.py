"""Startup verification over a real Unix websocket with an unresolved-send fence."""
import importlib.machinery
import importlib.util
import json
from pathlib import Path
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from websockets.asyncio.server import unix_serve
from websockets.exceptions import ConnectionClosed

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/codex-coordinator-resume"
loader = importlib.machinery.SourceFileLoader("coordinator_resume", str(SCRIPT))
spec = importlib.util.spec_from_loader(loader.name, loader)
module = importlib.util.module_from_spec(spec)
loader.exec_module(module)


@pytest.mark.parametrize("mode", ["ok", "rejected", "missing", "cwd", "danger", "roots", "extra",
                                  "identity", "source", "busy", "became_busy", "hook", "admission",
                                  "title", "labels", "tool", "failed", "lost_acceptance"])
async def test_startup_context_and_unresolved_send_boundary(tmp_path, monkeypatch, mode):
    identity, requests = str(uuid4()), []
    socket, pending = str(tmp_path / "daemon.sock"), tmp_path / "pending"
    def hook():
        if mode == "hook":
            raise RuntimeError("notes unavailable")
        return "\n".join(f"## RESTORED NOTE {i}: {label}" for i, label in enumerate(module.LABELS, 1))
    monkeypatch.setattr(module, "restore_notes", hook)
    monkeypatch.setattr(module, "admit", AsyncMock(
        side_effect=RuntimeError("WAIT") if mode == "admission" else None))
    async def daemon(connection):
        async for raw in connection:
            packet = json.loads(raw)
            requests.append(packet)
            method = packet["method"]
            if method == "initialized":
                continue
            result = {}
            if method == "thread/resume":
                result = {
                    "thread": {"id": identity, "status": {"type": "active" if mode == "busy" else "idle"}},
                    "cwd": str(module.RECOVERY),
                    "instructionSources": [str(module.RECOVERY / "AGENTS.md")],
                    "sandbox": {"type": "workspaceWrite", "writableRoots": sorted(module.ROOTS)}}
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
                elif mode == "source":
                    result["instructionSources"] = ["/home/marco/switchstand/AGENTS.md"]
            elif method == "thread/read":
                result = {"thread": {"status": {"type": "active" if mode == "became_busy" else "idle"}}}
            elif method == "turn/start":
                if mode == "lost_acceptance":
                    await connection.close()
                    return
                result = {"turn": {"id": "verification"}}
            response = {"id": packet["id"], "result": result}
            if mode == "rejected" and method == "thread/resume":
                response = {"id": packet["id"], "error": {"message": "missing thread"}}
            await connection.send(json.dumps(response))
            if method == "turn/start":
                receipt = {"agents_title": "wrong" if mode == "title" else "Recovery Coordinator",
                           "restored_notes": [] if mode == "labels" else module.LABELS}
                item = {"type": "commandExecution"} if mode == "tool" else {
                    "type": "agentMessage", "text": json.dumps(receipt)}
                await connection.send(json.dumps({"method": "item/completed", "params": {
                    "threadId": identity, "turnId": "verification", "item": item}}))
                await connection.send(json.dumps({"method": "turn/completed", "params": {
                    "threadId": identity, "turn": {"id": "verification",
                    "status": "failed" if mode == "failed" else "completed"}}}))
    async with unix_serve(daemon, socket):
        if mode == "ok":
            await module.resume(socket, identity, pending)
        else:
            with pytest.raises((RuntimeError, ConnectionClosed)):
                await module.resume(socket, identity, pending)
        starts = [p for p in requests if p["method"] == "turn/start"]
        submitted = mode in {"ok", "title", "labels", "tool", "failed", "lost_acceptance"}
        assert len(starts) == int(submitted)
        assert pending.exists() == (submitted and mode != "ok")
        if pending.exists():
            before = len(requests)
            with pytest.raises(RuntimeError, match="unresolved"):
                await module.resume(socket, identity, pending)
            assert len(requests) == before
    assert all(p["method"] != "thread/start" for p in requests)
    params = requests[2]["params"]
    assert params["threadId"] == identity and params["excludeTurns"] is True
    assert params["cwd"] == str(module.RECOVERY) and params["sandbox"] == "workspace-write"
    assert set(params["config"]["sandbox_workspace_write"]["writable_roots"]) == module.ROOTS
    if starts:
        text = starts[0]["params"]["input"][0]["text"]
        assert "Recovery Coordinator" not in text  # Must elicit, not supply, the loaded title.


@pytest.mark.parametrize("overbudget", [False, True])
def test_real_hook_output_budget_and_labels(tmp_path, monkeypatch, overbudget):
    monkeypatch.setattr(module, "RECOVERY", tmp_path)
    (tmp_path / "AGENTS.md").write_text("# Recovery Coordinator\n")
    home = tmp_path / "codex-home"
    home.mkdir()
    hook = home / "recovery-notes-hook"
    notes = "\n".join(f"## RESTORED NOTE {i}: {label}" for i, label in enumerate(module.LABELS, 1))
    hook.write_text("#!/usr/bin/python3\nprint(" + repr(notes + ("x" * 15360 if overbudget else "")) + ")\n")
    hook.chmod(0o700)
    if overbudget:
        with pytest.raises(RuntimeError, match="15 KB"):
            module.restore_notes()
    else:
        assert module.restore_notes().strip() == notes
