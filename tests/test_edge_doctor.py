import json
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from switchstand.edge_doctor import REQUIRED_KEYS, run

RESOURCE = "https://public.example/mcp"

class EdgeHandler(BaseHTTPRequestHandler):
    resource = RESOURCE

    def do_POST(self):
        self.send_response(401)
        metadata = self.resource.removesuffix("/mcp") + "/.well-known/oauth-protected-resource/mcp"
        self.send_header("WWW-Authenticate", f'Bearer resource_metadata="{metadata}"')
        self.end_headers()

    def do_GET(self):
        body = json.dumps({"resource": self.resource}).encode()
        self.send_response(200)
        self.end_headers()
        self.wfile.write(body)

@pytest.fixture
def edge():
    server = ThreadingHTTPServer(("127.0.0.1", 0), EdgeHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/mcp", server.server_port
    finally:
        server.shutdown()
        server.server_close()

def env_file(tmp_path, port, *, missing=()):
    path = tmp_path / "edge.env"
    values = {key: "configured" for key in REQUIRED_KEYS}
    values.update({"SWITCHSTAND_MCP_RESOURCE_URL": RESOURCE,
                   "SWITCHSTAND_MCP_BIND_HOST": "127.0.0.1",
                   "SWITCHSTAND_MCP_BIND_PORT": str(port)})
    path.write_text("\n".join(f"{key}={value}" for key, value in values.items() if key not in missing))
    path.chmod(0o600)
    return path

def test_all_checks_pass_against_real_local_http_server(edge, tmp_path, capsys):
    url, port = edge
    env = env_file(tmp_path, port)
    sha = subprocess.run(["git", "rev-parse", "HEAD"], check=True, capture_output=True,
                         text=True).stdout.strip()
    assert run(["--env-file", str(env), "--public-url", url, "--expected-sha", sha]) == 0
    output = capsys.readouterr().out
    assert "PASS env_file" in output and "PASS env_keys" in output
    assert "PASS local_http" in output and "PASS public_http" in output
    assert f"PASS runtime_sha: {sha}" in output and "configured" not in output

def test_failures_and_omitted_checks_are_truthful(edge, tmp_path, capsys):
    _, port = edge
    env = env_file(tmp_path, port, missing={"ASANA_TOKEN"})
    env.chmod(0o644)
    EdgeHandler.resource = "https://wrong.example/mcp"
    try:
        assert run(["--env-file", str(env), "--expected-sha", "0" * 40]) == 1
    finally:
        EdgeHandler.resource = RESOURCE
    output = capsys.readouterr().out
    assert "FAIL env_file: mode 0644" in output and "FAIL env_keys: missing ASANA_TOKEN" in output
    assert "FAIL local_http: unexpected challenge or resource metadata" in output
    assert "NOT_RUN public_http: no public URL supplied" in output
    assert "FAIL runtime_sha:" in output
