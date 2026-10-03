import json
import subprocess
from pathlib import Path
from uuid import uuid4

import httpx
import pytest

import switchstand.edge_semantic_probe as probe


class FakeMCP:
    def __init__(self, endpoint, token):
        self.endpoint, self.token, self.requests = endpoint, token, []
        self.client = object()

    def close(self):
        pass

    def initialize(self):
        if self.token != "right":
            raise AssertionError("wrong token must use raw initialization")

    def _post(self, body, *, session=True):
        del body, session
        return httpx.Response(401)

    def unauthorized_initialize(self):
        return 401

    def call(self, method, params, request_id):
        self.requests.append({"method": method, "params": params, "id": request_id})
        if method == "tools/list":
            return {"tools": [{"name": "work_get", "inputSchema": {"type": "object"}}]}
        name, arguments = params["name"], params["arguments"]
        if name == "work_get":
            if arguments["work_id"] == IDS["foreign"]:
                return {"status": "denied", "item": None}
            title = "Dependency" if arguments["work_id"] == IDS["dependency"] else "Representative"
            return {"status": "ok", "item": {"id": arguments["work_id"],
                "title": title, "revision": "r1", "completed": False,
                "routing": {}, "context": {}}}
        if name == "work_search":
            work_id = IDS["dependency"] if arguments["text"] == "Dependency" else IDS["work"]
            return {"status": "ok", "items": [{"id": work_id, "title": arguments["text"],
                "revision": "r1", "completed": False, "routing": {}, "context": {}}]}
        if name == "work_history":
            return {"status": "stale", "work_id": IDS["work"], "revision": "r1"}
        raise AssertionError(name)


IDS = {name: str(uuid4()) for name in ("work", "foreign", "dependency")}
TOOLS = [{"name": "work_get", "inputSchema": {"type": "object"}}]


def private(path: Path, value: str) -> Path:
    path.write_text(value)
    path.chmod(0o600)
    return path


@pytest.fixture
def grounded(monkeypatch):
    monkeypatch.setattr(probe, "MCP", FakeMCP)
    monkeypatch.setattr(probe, "_runtime", lambda _client, _endpoint, sha:
                        {"runtime_sha": sha, "run_id": "run-1"})
    monkeypatch.setattr(probe.subprocess, "run", lambda *args, **kwargs:
                        subprocess.CompletedProcess(args[0], 0, stdout="a" * 40 + "\n"))


def arguments(tmp_path: Path, endpoint="http://127.0.0.1:8790/mcp") -> list[str]:
    return ["--endpoint", endpoint,
        "--token-file", str(private(tmp_path / "token", "right")),
        "--wrong-token-file", str(private(tmp_path / "wrong", "wrong")),
        "--work-id", IDS["work"], "--foreign-work-id", IDS["foreign"],
        "--dependency-work-id", IDS["dependency"], "--expected-sha", "a" * 40,
        "--expected-tools-sha256", probe._digest(TOOLS),
        "--expected-principal", "issuer|subject|client", "--repo", str(tmp_path)]


def test_read_only_probe_records_exact_schema_and_denials(tmp_path, grounded):
    receipt = tmp_path / "read.json"
    assert probe.run(arguments(tmp_path) + ["--receipt", str(receipt)]) == 0

    value = json.loads(receipt.read_text())
    assert value["result"] == "PASS" and value["tools"] == TOOLS
    assert value["production_mutation"] == "NOT_RUN"
    assert value["principal_proof"] == "NOT_RUN"
    assert value["results_sha256"] == probe._digest(value["results"])
    assert value["results"]["get"]["item"]["id"] == IDS["work"]
    assert value["results"]["dependency"]["item"]["id"] == IDS["dependency"]
    assert receipt.stat().st_mode & 0o777 == 0o600


def test_effect_options_are_not_part_of_read_only_probe(tmp_path, grounded):
    with pytest.raises(SystemExit):
        probe.run(arguments(tmp_path) + ["--receipt", str(tmp_path / "never.json"),
            "--allow-disposable-mutation"])
    assert not (tmp_path / "never.json").exists()


def test_rejects_non_private_token_before_network(tmp_path, grounded):
    args = arguments(tmp_path)
    Path(args[3]).chmod(0o644)
    with pytest.raises(probe.ProbeFailure, match="mode-0600"):
        probe.run(args + ["--receipt", str(tmp_path / "never.json")])
