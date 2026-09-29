import json
import os
import shutil
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from switchstand.edge_doctor import REQUIRED_KEYS, run

ROOT = Path(__file__).parents[1]

RESOURCE = "https://public.example/switchstand/mcp"
METADATA_PATH = "/.well-known/oauth-protected-resource/switchstand/mcp"

class EdgeHandler(BaseHTTPRequestHandler):
    resource = RESOURCE
    document = None

    def do_POST(self):
        self.send_response(401)
        metadata = "https://public.example" + METADATA_PATH
        self.send_header("WWW-Authenticate", f'Bearer resource_metadata="{metadata}"')
        self.end_headers()

    def do_GET(self):
        if self.path != METADATA_PATH:
            self.send_response(404)
            self.end_headers()
            return
        body = json.dumps(self.document if self.document is not None
                          else {"resource": self.resource}).encode()
        self.send_response(200)
        self.end_headers()
        self.wfile.write(body)

@pytest.fixture
def edge():
    server = ThreadingHTTPServer(("127.0.0.1", 0), EdgeHandler)
    serving = threading.Thread(
        target=server.serve_forever,
        kwargs={"poll_interval": 0.01},
        daemon=True,
    )
    serving.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/mcp", server.server_port
    finally:
        server.shutdown()
        server.server_close()
        serving.join(timeout=1)
        assert not serving.is_alive(), "edge test server did not terminate"

def env_file(tmp_path, port, *, missing=()):
    path = tmp_path / "edge.env"
    values = {key: "configured" for key in REQUIRED_KEYS}
    values.update({"SWITCHSTAND_MCP_RESOURCE_URL": RESOURCE,
                   "SWITCHSTAND_MCP_BIND_HOST": "127.0.0.1",
                   "SWITCHSTAND_MCP_BIND_PORT": str(port)})
    path.write_text("\n".join(f"{key}={value}" for key, value in values.items() if key not in missing))
    path.chmod(0o600)
    return path


def oauth_env_file(tmp_path, *, missing=()):
    path = tmp_path / "oauth.env"
    values = {key: "configured" for key in REQUIRED_KEYS}
    path.write_text("\n".join(f"{key}={value}" for key, value in values.items()
                              if key not in missing))
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


def test_split_config_passes_without_provider_or_database_secrets(edge, tmp_path, capsys):
    url, _ = edge
    assert run([
        "--env-file", str(oauth_env_file(tmp_path)),
        "--resource-url", RESOURCE,
        "--local-url", url,
        "--public-url", url,
    ]) == 0
    output = capsys.readouterr().out
    assert "PASS env_file" in output and "PASS env_keys" in output
    assert "PASS local_http" in output and "PASS public_http" in output
    assert "NOT_RUN checkout_sha" in output and "configured" not in output


@pytest.mark.parametrize("local_url", [
    "https://127.0.0.1:8790/mcp",
    "http://example.com/mcp",
    "http://127.0.0.1:8790/wrong",
    "http://user:secret@127.0.0.1:8790/mcp",
])
def test_explicit_local_url_is_restricted_to_loopback_http_mcp(
        local_url, tmp_path, capsys):
    assert run([
        "--env-file", str(oauth_env_file(tmp_path)),
        "--resource-url", RESOURCE,
        "--local-url", local_url,
    ]) == 1
    output = capsys.readouterr().out
    assert "FAIL local_http: invalid edge configuration" in output
    assert "secret" not in output


@pytest.mark.parametrize("option", ["--resource-url", "--local-url", "--public-url"])
def test_explicit_empty_url_does_not_fall_back_or_become_omitted(
        option, edge, tmp_path, capsys):
    _, port = edge
    assert run(["--env-file", str(env_file(tmp_path, port)), option, ""]) == 1
    output = capsys.readouterr().out
    assert "FAIL" in output
    if option == "--public-url":
        assert "NOT_RUN public_http" not in output

def test_failures_and_omitted_checks_are_truthful(edge, tmp_path, capsys):
    _, port = edge
    env = env_file(tmp_path, port, missing={"SWITCHSTAND_MCP_GITHUB_CLIENT_SECRET"})
    env.chmod(0o644)
    EdgeHandler.resource = "https://wrong.example/mcp"
    try:
        assert run(["--env-file", str(env), "--expected-sha", "0" * 40]) == 1
    finally:
        EdgeHandler.resource = RESOURCE
    output = capsys.readouterr().out
    assert "FAIL env_file: mode 0644" in output
    assert "FAIL env_keys: missing SWITCHSTAND_MCP_GITHUB_CLIENT_SECRET" in output
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


def test_repository_entrypoint_uses_linked_worktree_source(tmp_path: Path) -> None:
    primary = tmp_path / "primary"
    primary.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=primary, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=primary, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.invalid"], cwd=primary, check=True
    )
    (primary / "tracked").write_text("base\n")
    subprocess.run(["git", "add", "tracked"], cwd=primary, check=True)
    subprocess.run(["git", "commit", "-m", "base"], cwd=primary, check=True, capture_output=True)
    venv_bin = primary / ".venv" / "bin"
    venv_bin.mkdir(parents=True)
    python = venv_bin / "python"
    python.write_text(
        "#!/bin/sh\n"
        "printf '%s\\n' \"$PYTHONPATH\" > \"$ENTRYPOINT_RESULT\"\n"
        "printf '%s\\n' \"$@\" >> \"$ENTRYPOINT_RESULT\"\n"
    )
    python.chmod(0o755)
    writer = tmp_path / "writer"
    subprocess.run(
        ["git", "worktree", "add", "-b", "candidate", str(writer)],
        cwd=primary,
        check=True,
        capture_output=True,
    )
    script = writer / "scripts" / "switchstand-edge-doctor"
    script.parent.mkdir()
    shutil.copy2(ROOT / "scripts" / "switchstand-edge-doctor", script)
    result_file = tmp_path / "entrypoint-result"

    result = subprocess.run(
        [script, "--help"],
        env=os.environ | {"ENTRYPOINT_RESULT": str(result_file)},
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    lines = result_file.read_text().splitlines()
    assert lines[0].split(os.pathsep)[0] == str(writer / "src")
    assert lines[1:] == ["-m", "switchstand.edge_doctor", "--help"]


def test_module_entrypoint_executes_doctor() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "switchstand.edge_doctor", "--help"],
        cwd=ROOT,
        env=os.environ | {"PYTHONPATH": str(ROOT / "src")},
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "usage:" in result.stdout
    assert "--env-file" in result.stdout
