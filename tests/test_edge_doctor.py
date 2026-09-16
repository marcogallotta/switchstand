import json
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from switchstand.edge_doctor import REQUIRED_KEYS, run

RESOURCE = "https://public.example/mcp"

class EdgeHandler(BaseHTTPRequestHandler):
    resource = RESOURCE
    document = None

    def do_POST(self):
        self.send_response(401)
        metadata = self.resource.removesuffix("/mcp") + "/.well-known/oauth-protected-resource/mcp"
        self.send_header("WWW-Authenticate", f'Bearer resource_metadata="{metadata}"')
        self.end_headers()

    def do_GET(self):
        body = json.dumps(self.document if self.document is not None
                          else {"resource": self.resource}).encode()
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

def test_all_checks_pass_against_real_local_http_server(edge, tmp_path, capsys, monkeypatch):
    url, port = edge
    env = env_file(tmp_path, port)
    sha = "a" * 40
    # Quality mounts source without Git metadata; only HTTP is real in this test.
    monkeypatch.setattr(
        "switchstand.edge_doctor.subprocess.run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 0, stdout=f"{sha}\n"),
    )
    assert run(["--env-file", str(env), "--public-url", url, "--expected-sha", sha]) == 0
    output = capsys.readouterr().out
    assert "PASS env_file" in output and "PASS env_keys" in output
    assert "PASS local_http" in output and "PASS public_http" in output
    assert f"PASS checkout_sha: {sha}" in output and "configured" not in output

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
    assert "FAIL checkout_sha:" in output


@pytest.mark.parametrize("document", [[], "unexpected", 42])
def test_non_object_metadata_fails_without_crashing(edge, tmp_path, capsys, monkeypatch, document):
    _, port = edge
    monkeypatch.setattr(EdgeHandler, "document", document)
    assert run(["--env-file", str(env_file(tmp_path, port))]) == 1
    assert "FAIL local_http: unexpected challenge or resource metadata" in capsys.readouterr().out


def test_invalid_configuration_does_not_echo_input(edge, tmp_path, capsys):
    _, port = edge
    env = env_file(tmp_path, port)
    env.write_text(env.read_text().replace(RESOURCE, "https://user:secret@example.com:bad/mcp"))
    assert run(["--env-file", str(env)]) == 1
    output = capsys.readouterr().out
    assert "FAIL local_http: invalid edge configuration" in output
    assert "secret" not in output


def test_public_url_credentials_are_rejected(edge, tmp_path, capsys):
    url, port = edge
    credential_url = url.replace("http://", "http://user:secret@")
    assert run(["--env-file", str(env_file(tmp_path, port)),
                "--public-url", credential_url]) == 1
    output = capsys.readouterr().out
    assert "FAIL public_http: probe URL must not contain credentials" in output
    assert "secret" not in output
