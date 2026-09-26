"""Disposable MCP prototype for agent-addressed messaging.

This deliberately ignores production WorkGrant/message machinery. It exists only to
test whether ordinary agents feel better when identity is an agent name instead of a
task. State is one local JSON file so separate MCP sessions can interact.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import secrets
from pathlib import Path
from uuid import uuid4

import httpx
from fastmcp import FastMCP
from fastmcp.server.dependencies import get_context
from pydantic import JsonValue

STATE_PATH = Path(
    os.getenv(
        "SWITCHSTAND_PROTO_STATE",
        "~/.local/state/switchstand/agent-messaging-prototype.json",
    )
).expanduser()
PORT = int(os.getenv("SWITCHSTAND_PROTO_PORT", "8791"))
NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,31}$")
LOCK = asyncio.Lock()
MCP = FastMCP("Switchstand product prototype", version="0")

ASANA_API = "https://app.asana.com/api/1.0"
PRODUCT_ROUTES = {
    "stateful": {
        "owner": "1218348601889574",
        "map": "1218659756993596",
        "implementation": "1218664323392679",
        "correction": "1218890890941815",
    },
    "lifecycle": {"owner": "1218348601889574"},
}


def _asana_token() -> str:
    token = os.getenv("ASANA_TOKEN")
    if token:
        return token
    env_path = Path("~/.config/switchstand/.env").expanduser()
    if env_path.exists():
        for raw in env_path.read_text().splitlines():
            if raw.startswith("ASANA_TOKEN="):
                return raw.split("=", 1)[1].strip().strip("'\"")
    raise ValueError("ASANA_TOKEN unavailable")


async def _asana_task(gid: str) -> dict:
    fields = "gid,name,notes,completed,modified_at,parent.gid,permalink_url"
    async with httpx.AsyncClient(
        base_url=ASANA_API,
        headers={"Authorization": f"Bearer {_asana_token()}"},
        timeout=10,
        trust_env=False,
    ) as client:
        response = await client.get(f"/tasks/{gid}", params={"opt_fields": fields})
        response.raise_for_status()
        return response.json()["data"]


def _task_summary(task: dict, role: str) -> dict:
    notes = task.get("notes") or ""
    return {
        "role": role,
        "gid": task["gid"],
        "name": task["name"],
        "completed": task.get("completed", False),
        "modified_at": task.get("modified_at"),
        "parent_gid": (task.get("parent") or {}).get("gid"),
        "permalink_url": task.get("permalink_url"),
        "headline": notes.splitlines()[0] if notes else None,
    }


async def work_resolve(query: str) -> dict:
    """Resolve Marco's natural work reference to canonical current Switchstand work."""
    q = query.strip().lower()
    gid_match = re.search(r"(?<!\d)(\d{13,})(?!\d)", q)
    if gid_match:
        task = await _asana_task(gid_match.group(1))
        return {"status": "ok", "confidence": 1.0, "resolved": _task_summary(task, "exact")}

    route = None
    for alias, candidate in PRODUCT_ROUTES.items():
        if alias in q:
            route = candidate
            break
    if route is None:
        return {
            "status": "unknown",
            "confidence": 0.0,
            "reason": "no_canonical_route_known",
            "next": "capture this product-usage gap instead of guessing from search",
        }

    target_role = "owner"
    if "implement" in q or "spec" in q:
        target_role = "implementation"
    elif "map" in q or "rollout" in q or "capabilit" in q:
        target_role = "map"
    elif any(word in q for word in ("correct", "prune", "audit", "follow-up", "blocker")):
        target_role = "correction"

    target_gid = route.get(target_role) or route["owner"]
    target = await _asana_task(target_gid)
    related = []
    for role, gid in route.items():
        if gid != target_gid:
            related.append(_task_summary(await _asana_task(gid), role))

    return {
        "status": "ok",
        "confidence": 0.97,
        "resolved": _task_summary(target, target_role),
        "canonical_owner_gid": route["owner"],
        "related_current_work": related,
        "reason": (
            "resolved through the known semantic owner route; text search is not authority"
        ),
    }


def _empty() -> dict:
    return {"agents": {}, "sessions": {}, "messages": []}


def _load() -> dict:
    return json.loads(STATE_PATH.read_text()) if STATE_PATH.exists() else _empty()


def _save(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, indent=2, sort_keys=True))


def _hash(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


def _key(name: str) -> str:
    if not NAME_RE.fullmatch(name):
        raise ValueError("name must be 1-32 letters/digits/_/- and start with a letter")
    return name.casefold()


def _session() -> str:
    session_id = get_context().session_id
    if not session_id:
        raise RuntimeError("MCP session unavailable")
    return session_id


def _actor(state: dict) -> tuple[str, dict]:
    binding = state["sessions"].get(_session())
    if binding is None:
        raise ValueError("register or resume an agent first")
    agent = state["agents"].get(binding["key"])
    if agent is None or agent["generation"] != binding["generation"]:
        raise ValueError("stale agent binding")
    return binding["key"], agent


def _owned_message(state: dict, message_id: str) -> tuple[dict, dict]:
    key, agent = _actor(state)
    for item in state["messages"]:
        if item["id"] == message_id and item["recipient_key"] == key:
            return item, agent
    raise ValueError("message not found for current agent")


async def agent_register(name: str) -> dict:
    """Choose an unused visible name and bind this MCP session to it."""
    async with LOCK:
        state = _load()
        key = _key(name)
        if key in state["agents"]:
            return {"status": "conflict", "reason": "name_taken"}
        credential = secrets.token_urlsafe(24)
        state["agents"][key] = {
            "name": name,
            "generation": 1,
            "credential_hash": _hash(credential),
        }
        state["sessions"][_session()] = {"key": key, "generation": 1}
        _save(state)
        return {
            "status": "ok",
            "agent_name": name,
            "generation": 1,
            "resume_credential": credential,
        }


async def agent_resume(name: str, resume_credential: str) -> dict:
    """Bind a new MCP session to the same agent name after reconnect."""
    async with LOCK:
        state = _load()
        key = _key(name)
        agent = state["agents"].get(key)
        if agent is None or agent["credential_hash"] != _hash(resume_credential):
            return {"status": "denied"}
        state["sessions"][_session()] = {"key": key, "generation": agent["generation"]}
        _save(state)
        return {
            "status": "ok",
            "agent_name": agent["name"],
            "generation": agent["generation"],
        }


async def agent_takeover(name: str, takeover_code: str) -> dict:
    """Replace an old agent only when Marco hands over the prototype takeover code."""
    expected = os.getenv("SWITCHSTAND_PROTO_TAKEOVER_CODE", "")
    if not expected or not secrets.compare_digest(takeover_code, expected):
        return {"status": "denied"}
    async with LOCK:
        state = _load()
        key = _key(name)
        agent = state["agents"].get(key)
        if agent is None:
            return {"status": "not_found"}
        credential = secrets.token_urlsafe(24)
        agent["generation"] += 1
        agent["credential_hash"] = _hash(credential)
        state["sessions"][_session()] = {
            "key": key,
            "generation": agent["generation"],
        }
        _save(state)
        return {
            "status": "ok",
            "agent_name": agent["name"],
            "generation": agent["generation"],
            "resume_credential": credential,
        }


async def agent_status() -> dict:
    """Show the logical identity bound to this MCP session."""
    async with LOCK:
        state = _load()
        _key_, agent = _actor(state)
        return {
            "status": "ok",
            "agent_name": agent["name"],
            "generation": agent["generation"],
        }


async def work_focus(work_id: str) -> dict:
    """Switch current work context without changing agent identity."""
    async with LOCK:
        state = _load()
        _key_, agent = _actor(state)
        agent["work_id"] = work_id
        _save(state)
        return {"status": "ok", "agent_name": agent["name"], "work_id": work_id}


async def message_send(
    recipient: str,
    payload: JsonValue,
    context: dict[str, str] | None = None,
) -> dict:
    """Send to an agent name; task/work context is optional metadata."""
    async with LOCK:
        state = _load()
        _sender_key, sender = _actor(state)
        recipient_key = _key(recipient)
        target = state["agents"].get(recipient_key)
        if target is None:
            return {"status": "not_found", "reason": "recipient_unknown"}
        message_id = str(uuid4())
        state["messages"].append(
            {
                "id": message_id,
                "sender": sender["name"],
                "recipient": target["name"],
                "recipient_key": recipient_key,
                "payload": payload,
                "context": context,
                "state": "AVAILABLE",
                "received_generation": None,
                "in_reply_to": None,
            }
        )
        _save(state)
        return {"status": "ok", "message_id": message_id}


async def message_pending() -> dict:
    """List this agent's available or received messages."""
    async with LOCK:
        state = _load()
        key, _agent = _actor(state)
        messages = [
            item
            for item in state["messages"]
            if item["recipient_key"] == key and item["state"] != "DISPOSITIONED"
        ]
        return {"status": "ok", "messages": messages}


async def message_receive(message_id: str) -> dict:
    """Claim one exact message for the current agent generation."""
    async with LOCK:
        state = _load()
        item, agent = _owned_message(state, message_id)
        if item["state"] == "DISPOSITIONED":
            return {"status": "conflict", "reason": "already_dispositioned"}
        item["state"] = "RECEIVED"
        item["received_generation"] = agent["generation"]
        _save(state)
        return {"status": "ok", "message": item}


async def message_reply(
    message_id: str,
    payload: JsonValue,
    context: dict[str, str] | None = None,
) -> dict:
    """Reply to the logical sender; task/work context remains optional."""
    async with LOCK:
        state = _load()
        item, agent = _owned_message(state, message_id)
        if item["state"] != "RECEIVED":
            return {"status": "conflict", "reason": "receive_first"}
        if item["received_generation"] != agent["generation"]:
            return {"status": "stale"}
        reply_id = str(uuid4())
        state["messages"].append(
            {
                "id": reply_id,
                "sender": agent["name"],
                "recipient": item["sender"],
                "recipient_key": _key(item["sender"]),
                "payload": payload,
                "context": context,
                "state": "AVAILABLE",
                "received_generation": None,
                "in_reply_to": item["id"],
            }
        )
        item["state"] = "DISPOSITIONED"
        _save(state)
        return {"status": "ok", "message_id": reply_id}


async def message_done(message_id: str) -> dict:
    """Mark one received message handled without replying."""
    async with LOCK:
        state = _load()
        item, agent = _owned_message(state, message_id)
        if item["state"] != "RECEIVED":
            return {"status": "conflict", "reason": "receive_first"}
        if item["received_generation"] != agent["generation"]:
            return {"status": "stale"}
        item["state"] = "DISPOSITIONED"
        _save(state)
        return {"status": "ok"}


for tool in (
    agent_register, agent_resume, agent_takeover, agent_status, work_focus, work_resolve,
    message_send, message_pending, message_receive, message_reply, message_done,
):
    MCP.tool(tool)


async def _serve() -> None:
    app = MCP.http_app(path="/mcp", json_response=True, stateless_http=False)
    await app.state.fastmcp_server.run_http_async(
        host="127.0.0.1",
        port=PORT,
        path="/mcp",
        json_response=True,
        stateless_http=False,
        show_banner=False,
    )


if __name__ == "__main__":
    asyncio.run(_serve())
