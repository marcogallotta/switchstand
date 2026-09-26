"""Disposable Switchstand MCP prototype. Not production code."""

import asyncio, hashlib, json, os, re, secrets
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
from fastmcp import FastMCP
from fastmcp.server.dependencies import get_context

STATE = Path("~/.local/state/switchstand/proto.json").expanduser()
PORT = int(os.getenv("SWITCHSTAND_PROTO_PORT", "8791"))
MCP = FastMCP("Switchstand prototype", version="0")
LOCK = asyncio.Lock()
NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,31}$")

# Temporary compatibility fixture until Asana installs alias->owner + typed child fields.
ROUTES = {
    "stateful": {
        "owner": "1218348601889574",
        "map": ["1218659756993596"],
        "execution": ["1218664323392679"],
        "correction": ["1218890890941815"],
    },
    "lifecycle": {"owner": "1218348601889574"},
}


def load() -> dict:
    return json.loads(STATE.read_text()) if STATE.exists() else {
        "agents": {}, "sessions": {}, "messages": []
    }


def save(s: dict) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(s, indent=2))


def digest(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


def key(name: str) -> str:
    if not NAME.fullmatch(name):
        raise ValueError("bad agent name")
    return name.casefold()


def session() -> str:
    sid = get_context().session_id
    if not sid:
        raise ValueError("missing MCP session")
    return sid


def actor(s: dict) -> tuple[str, dict]:
    b = s["sessions"].get(session())
    if not b:
        raise ValueError("register/resume first")
    a = s["agents"].get(b["key"])
    if not a or a["generation"] != b["generation"]:
        raise ValueError("stale binding")
    return b["key"], a


def token() -> str:
    if os.getenv("ASANA_TOKEN"):
        return os.environ["ASANA_TOKEN"]
    p = Path("~/.config/switchstand/.env").expanduser()
    for line in p.read_text().splitlines():
        if line.startswith("ASANA_TOKEN="):
            return line.split("=", 1)[1].strip().strip("'\"")
    raise ValueError("ASANA_TOKEN unavailable")


async def task(gid: str) -> dict:
    fields = "gid,name,notes,completed,modified_at,parent.gid,permalink_url"
    async with httpx.AsyncClient(
        headers={"Authorization": f"Bearer {token()}"}, trust_env=False, timeout=10
    ) as c:
        r = await c.get(
            f"https://app.asana.com/api/1.0/tasks/{gid}",
            params={"opt_fields": fields},
        )
        r.raise_for_status()
        return r.json()["data"]


def summary(t: dict, role: str) -> dict:
    return {
        "role": role, "gid": t["gid"], "name": t["name"],
        "completed": t.get("completed", False), "modified_at": t.get("modified_at"),
        "parent_gid": (t.get("parent") or {}).get("gid"),
        "url": t.get("permalink_url"),
    }


async def work_resolve(query: str) -> dict:
    """Resolve exact IDs or alias->owner, then return owner-local typed current sets."""
    q = query.lower()
    m = re.search(r"(?<!\d)(\d{13,})(?!\d)", q)
    if m:
        return {"status": "EXACT", "owner": summary(await task(m.group(1)), "exact")}

    matches = [(a, r) for a, r in ROUTES.items() if a in q]
    if not matches:
        return {"status": "UNKNOWN", "reason": "no alias->owner route"}
    owners = {r["owner"] for _, r in matches}
    if len(owners) != 1:
        return {"status": "AMBIGUOUS", "aliases": [a for a, _ in matches]}

    route = matches[0][1]
    out = {"status": "UNAMBIGUOUS", "owner": summary(await task(route["owner"]), "owner")}
    related: dict[str, list[dict]] = {}
    for role, gids in route.items():
        if role == "owner":
            continue
        related[role] = [summary(await task(gid), role) for gid in gids]
    out["current"] = related
    return out


async def work_read(gid: str) -> dict:
    """Read one exact Asana task by GID."""
    return {"status": "ok", "work": await task(gid)}


async def work_create(name: str, parent_gid: str, notes: str = "") -> dict:
    """Create one real child task under an exact canonical owner/work item."""
    data = {"name": name, "parent": parent_gid}
    if notes:
        data["notes"] = notes
    async with httpx.AsyncClient(
        headers={"Authorization": f"Bearer {token()}"}, trust_env=False, timeout=10
    ) as client:
        r = await client.post("https://app.asana.com/api/1.0/tasks", json={"data": data})
        r.raise_for_status()
        made = r.json()["data"]
    return {"status": "ok", "work": summary(await task(made["gid"]), "created")}


async def work_update(
    gid: str, name: str | None = None, notes: str | None = None,
    completed: bool | None = None,
) -> dict:
    """Update title/notes/completion on one exact Asana task."""
    data = {k: v for k, v in {
        "name": name, "notes": notes, "completed": completed
    }.items() if v is not None}
    if not data:
        return {"status": "no_change", "work": summary(await task(gid), "exact")}
    async with httpx.AsyncClient(
        headers={"Authorization": f"Bearer {token()}"}, trust_env=False, timeout=10
    ) as client:
        r = await client.put(
            f"https://app.asana.com/api/1.0/tasks/{gid}", json={"data": data}
        )
        r.raise_for_status()
    return {"status": "ok", "work": summary(await task(gid), "updated")}


async def agent_register(name: str) -> dict:
    async with LOCK:
        s, k = load(), key(name)
        if k in s["agents"]:
            return {"status": "conflict", "reason": "name_taken"}
        cred = secrets.token_urlsafe(18)
        s["agents"][k] = {"name": name, "generation": 1, "cred": digest(cred)}
        s["sessions"][session()] = {"key": k, "generation": 1}
        save(s)
        return {"status": "ok", "name": name, "resume_credential": cred}


async def agent_resume(name: str, resume_credential: str) -> dict:
    async with LOCK:
        s, k = load(), key(name)
        a = s["agents"].get(k)
        if not a or a["cred"] != digest(resume_credential):
            return {"status": "denied"}
        s["sessions"][session()] = {"key": k, "generation": a["generation"]}
        save(s)
        return {"status": "ok", "name": a["name"], "generation": a["generation"]}


async def agent_takeover(name: str, takeover_code: str) -> dict:
    if not secrets.compare_digest(
        takeover_code, os.getenv("SWITCHSTAND_PROTO_TAKEOVER_CODE", "")
    ):
        return {"status": "denied"}
    async with LOCK:
        s, k = load(), key(name)
        a = s["agents"].get(k)
        if not a:
            return {"status": "not_found"}
        cred = secrets.token_urlsafe(18)
        a["generation"] += 1
        a["cred"] = digest(cred)
        s["sessions"][session()] = {"key": k, "generation": a["generation"]}
        save(s)
        return {"status": "ok", "name": a["name"], "resume_credential": cred}


async def agent_status() -> dict:
    s = load()
    _, a = actor(s)
    return {"status": "ok", "name": a["name"], "generation": a["generation"], "work": a.get("work")}


async def work_focus(work_id: str) -> dict:
    async with LOCK:
        s = load()
        _, a = actor(s)
        a["work"] = work_id
        save(s)
        return {"status": "ok", "agent": a["name"], "work": work_id}


async def message_send(recipient: str, payload: Any, context: dict | None = None) -> dict:
    async with LOCK:
        s, rk = load(), key(recipient)
        _, sender = actor(s)
        if rk not in s["agents"]:
            return {"status": "not_found"}
        mid = str(uuid4())
        s["messages"].append({
            "id": mid, "sender": sender["name"], "recipient_key": rk,
            "payload": payload, "context": context, "state": "AVAILABLE",
            "received_generation": None,
        })
        save(s)
        return {"status": "ok", "message_id": mid}


async def message_pending() -> dict:
    s = load()
    k, _ = actor(s)
    return {"status": "ok", "messages": [
        m for m in s["messages"] if m["recipient_key"] == k and m["state"] != "DONE"
    ]}


async def message_receive(message_id: str) -> dict:
    async with LOCK:
        s = load()
        k, a = actor(s)
        for m in s["messages"]:
            if m["id"] == message_id and m["recipient_key"] == k:
                m["state"], m["received_generation"] = "RECEIVED", a["generation"]
                save(s)
                return {"status": "ok", "message": m}
        return {"status": "not_found"}


async def message_reply(message_id: str, payload: Any) -> dict:
    async with LOCK:
        s = load()
        k, a = actor(s)
        for m in s["messages"]:
            if m["id"] != message_id or m["recipient_key"] != k:
                continue
            if m["state"] != "RECEIVED" or m["received_generation"] != a["generation"]:
                return {"status": "stale"}
            recipient = key(m["sender"])
            rid = str(uuid4())
            s["messages"].append({
                "id": rid, "sender": a["name"], "recipient_key": recipient,
                "payload": payload, "context": {"in_reply_to": message_id},
                "state": "AVAILABLE", "received_generation": None,
            })
            m["state"] = "DONE"
            save(s)
            return {"status": "ok", "message_id": rid}
        return {"status": "not_found"}


for fn in (
    work_resolve, work_read, work_create, work_update,
    agent_register, agent_resume, agent_takeover, agent_status,
    work_focus, message_send, message_pending, message_receive, message_reply,
):
    MCP.tool(fn)


async def serve() -> None:
    app = MCP.http_app(path="/mcp", json_response=True, stateless_http=False)
    await app.state.fastmcp_server.run_http_async(
        host="127.0.0.1", port=PORT, path="/mcp",
        json_response=True, stateless_http=False, show_banner=False,
    )


if __name__ == "__main__":
    asyncio.run(serve())
