import asyncio
import json
import os
import socket
import subprocess
import time
from pathlib import Path

import pytest
import uvicorn
from chatgpt_fixture import grant, service
from key_value.aio.stores.memory import MemoryStore
from mcp.server.auth.provider import AccessToken

from switchstand.chatgpt_edge import (
    REQUIRED_SCOPE,
    MCPAuthConfig,
    SwitchstandGitHubProvider,
    create_app,
)
from switchstand.grants import PrincipalContext

RESOURCE = "https://switchstand.example.com/mcp"
ISSUER = "https://switchstand.example.com/"
GITHUB_ID = "192548"
CONFIG = MCPAuthConfig("client", "secret", GITHUB_ID, RESOURCE)
SERVER_NAME = "switchstand_proof"


class AppServerClient:
    def __init__(self, binary: Path, workspace: Path, env: dict[str, str]) -> None:
        self.next_id = 1
        self.process = subprocess.Popen(
            [str(binary), "app-server"],
            cwd=workspace,
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )

    def close(self) -> None:
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=3)

    def send(self, message: dict[str, object]) -> None:
        assert self.process.stdin is not None
        self.process.stdin.write(json.dumps(message, separators=(",", ":")) + "\n")
        self.process.stdin.flush()

    def notify(self, method: str) -> None:
        self.send({"method": method})

    def request(self, method: str, params: dict[str, object] | None) -> dict[str, object]:
        request_id = self.next_id
        self.next_id += 1
        request: dict[str, object] = {"id": request_id, "method": method}
        if params is not None:
            request["params"] = params
        self.send(request)
        assert self.process.stdout is not None
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            line = self.process.stdout.readline()
            if not line:
                break
            message = json.loads(line)
            if message.get("id") == request_id and "method" not in message:
                return message
            if "id" in message and isinstance(message.get("method"), str):
                self.send({
                    "id": message["id"],
                    "error": {"code": -32601, "message": "proof client does not handle server requests"},
                })
        stderr = ""
        if self.process.stderr is not None and self.process.poll() is not None:
            stderr = self.process.stderr.read()
        raise AssertionError(f"Codex app-server did not answer {method}: {stderr[-4000:]}")


def response_result(response: dict[str, object]) -> dict[str, object]:
    error = response.get("error")
    assert error is None, error
    result = response.get("result")
    assert isinstance(result, dict), response
    return result


def codex_smoke(binary: Path, workspace: Path, env: dict[str, str]) -> None:
    version = subprocess.run(
        [str(binary), "--version"], text=True, capture_output=True, check=True
    ).stdout.strip()
    print(f"CODEX_PROOF_VERSION={version}", flush=True)

    client = AppServerClient(binary, workspace, env)
    try:
        response_result(client.request(
            "initialize",
            {
                "clientInfo": {
                    "name": "switchstand-stateful-proof",
                    "title": "Switchstand stateful MCP proof",
                    "version": "1",
                },
                "capabilities": {"experimentalApi": True},
            },
        ))
        client.notify("initialized")

        inventory = response_result(client.request("mcpServerStatus/list", {"detail": "full"}))
        entries = inventory.get("data")
        assert isinstance(entries, list)
        entry = next(
            item for item in entries
            if isinstance(item, dict) and item.get("name") == SERVER_NAME
        )
        tools = entry.get("tools")
        assert isinstance(tools, dict) and "grant_get" in tools, entry

        thread = response_result(client.request(
            "thread/start", {"cwd": str(workspace), "ephemeral": True}
        ))
        thread_data = thread.get("thread")
        assert isinstance(thread_data, dict)
        thread_id = thread_data.get("id")
        assert isinstance(thread_id, str)

        for _ in range(2):
            called = response_result(client.request(
                "mcpServer/tool/call",
                {
                    "threadId": thread_id,
                    "server": SERVER_NAME,
                    "tool": "grant_get",
                    "arguments": {"api_version": "1"},
                },
            ))
            structured = called.get("structuredContent")
            assert isinstance(structured, dict), called
            assert structured["status"] == "ok"
            principal = structured["principal"]
            assert isinstance(principal, dict)
            assert principal["issuer"] == ISSUER
            assert principal["subject"] == GITHUB_ID
            assert principal["client_id"] == "codex-proof"
    finally:
        client.close()


@pytest.mark.asyncio
async def test_current_codex_app_server_calls_grant_get_over_stateful_http(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    raw_binary = os.getenv("CODEX_EXEC_PATH")
    if not raw_binary:
        pytest.skip("real Codex binary is supplied only by the Stage-5 proof job")
    binary = Path(raw_binary)
    assert binary.is_file() and os.access(binary, os.X_OK)

    async def verified(_self, token: str) -> AccessToken | None:
        if token != "proof-token":
            return None
        return AccessToken(
            token=token,
            client_id="codex-proof",
            scopes=[REQUIRED_SCOPE],
            subject=GITHUB_ID,
            claims={"iss": ISSUER},
            resource=RESOURCE,
            expires_at=int(time.time()) + 300,
        )

    monkeypatch.setattr(SwitchstandGitHubProvider, "verify_token", verified)
    subject = service()
    subject.grants.grant = grant(
        principal=PrincipalContext(
            issuer=ISSUER,
            subject=GITHUB_ID,
            client_id="codex-proof",
            assurance="authenticated",
        ),
        scope="workspace",
        operations=frozenset({"work_get"}),
    )
    app = create_app(subject, CONFIG, client_storage=MemoryStore())

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(128)
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level="warning", lifespan="on"))
    server_task = asyncio.create_task(server.serve(sockets=[sock]))
    try:
        for _ in range(100):
            if server.started:
                break
            await asyncio.sleep(0.05)
        assert server.started

        codex_home = tmp_path / "codex-home"
        workspace = tmp_path / "workspace"
        codex_home.mkdir()
        workspace.mkdir()
        (codex_home / "config.toml").write_text(
            'approval_policy = "never"\n'
            f'[mcp_servers.{SERVER_NAME}]\n'
            f'url = "http://127.0.0.1:{port}/mcp"\n'
            'bearer_token_env_var = "SWITCHSTAND_PROOF_TOKEN"\n'
            'required = true\n'
            'default_tools_approval_mode = "approve"\n'
            'enabled_tools = ["grant_get"]\n'
        )
        env = dict(os.environ)
        env["CODEX_HOME"] = str(codex_home)
        env["SWITCHSTAND_PROOF_TOKEN"] = "proof-token"
        await asyncio.to_thread(codex_smoke, binary, workspace, env)
    finally:
        server.should_exit = True
        await server_task
