import json
import subprocess
from pathlib import Path
from typing import ClassVar
from uuid import uuid4

import httpx
import pytest

import switchstand.edge_semantic_probe as probe


class FakeMCP:
    related: ClassVar[list[object]] = []
    relation_outcome: ClassVar[str] = "ok"
    relation_calls: ClassVar[int] = 0

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
        if name == "work_structure":
            return {"status": "stale", "work_id": IDS["work"], "revision": "r1"}
        if name == "work_relate":
            type(self).relation_calls += 1
            if self.relation_outcome == "raise":
                raise httpx.ReadTimeout("ambiguous")
            if self.relation_outcome == "denied":
                return {"status": "denied", "effect": "not_sent"}
            receipt = {"operation_id": arguments["operation_id"],
                "qualification": "test:disposable-c2d2",
                "principal": {"issuer": "issuer", "subject": "subject", "client_id": "client"}}
            result = {"status": "ok", "effect": "applied", "receipt": receipt}
            self.related.append(result)
            return result
        raise AssertionError(name)


IDS = {name: str(uuid4()) for name in ("work", "foreign", "dependency", "operation")}
TOOLS = [{"name": "work_get", "inputSchema": {"type": "object"}}]


def private(path: Path, value: str) -> Path:
    path.write_text(value)
    path.chmod(0o600)
    return path


@pytest.fixture
def grounded(monkeypatch):
    FakeMCP.related, FakeMCP.relation_outcome, FakeMCP.relation_calls = [], "ok", 0
    run_ids = iter(("run-1", "run-2", "run-3"))
    monkeypatch.setattr(probe, "MCP", FakeMCP)
    monkeypatch.setattr(probe, "_runtime", lambda _client, _endpoint, sha:
                        {"runtime_sha": sha, "run_id": next(run_ids)})
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
    assert value["mutation"] is None and value["principal_proof"] == "NOT_RUN"
    assert value["results_sha256"] == probe._digest(value["results"])
    assert value["results"]["get"]["item"]["id"] == IDS["work"]
    assert value["results"]["dependency"]["item"]["id"] == IDS["dependency"]
    assert receipt.stat().st_mode & 0o777 == 0o600
    assert not FakeMCP.related


def test_disposable_replay_and_cold_restart_are_exact(tmp_path, grounded):
    first, second = tmp_path / "first.json", tmp_path / "second.json"
    mutation = ["--allow-disposable-mutation", "--operation-id", IDS["operation"],
        "--expected-qualification", "test:disposable-c2d2"]
    assert probe.run(arguments(tmp_path) + ["--receipt", str(first)] + mutation) == 0
    assert probe.run(arguments(tmp_path) + ["--receipt", str(second),
        "--restart-of", str(first)] + mutation) == 0

    assert len(FakeMCP.related) == 4
    assert all(item == FakeMCP.related[0] for item in FakeMCP.related)
    value = json.loads(second.read_text())
    assert value["restart_of"] == probe._digest(json.loads(first.read_text()))
    assert value["runtime"]["run_id"] == "run-2"
    assert value["mutation"]["arguments"]["patch"] == {
        "kind": "dependency", "action": "add", "target_work_id": IDS["dependency"]}


def test_production_mutation_is_hard_disabled(tmp_path, grounded):
    with pytest.raises(probe.ProbeFailure, match="requires loopback"):
        probe.run(arguments(tmp_path, "https://public.example/mcp") + [
            "--receipt", str(tmp_path / "never.json"), "--allow-disposable-mutation",
            "--operation-id", IDS["operation"],
            "--expected-qualification", "test:disposable-c2d2"])
    assert not (tmp_path / "never.json").exists()


@pytest.mark.parametrize(("outcome", "status"), [("raise", "UNKNOWN"), ("denied", "FAIL")])
def test_failed_first_effect_is_recorded_without_replay(tmp_path, grounded, outcome, status):
    FakeMCP.relation_outcome = outcome
    receipt = tmp_path / "failed.json"
    with pytest.raises(probe.ProbeFailure):
        probe.run(arguments(tmp_path) + ["--receipt", str(receipt),
            "--allow-disposable-mutation", "--operation-id", IDS["operation"],
            "--expected-qualification", "test:disposable-c2d2"])
    value = json.loads(receipt.read_text())
    assert value["result"] == status and value["operation_id"] == IDS["operation"]
    assert FakeMCP.relation_calls == 1 and value["transcript"]


def test_rejects_non_private_token_before_network(tmp_path, grounded):
    args = arguments(tmp_path)
    Path(args[3]).chmod(0o644)
    with pytest.raises(probe.ProbeFailure, match="mode-0600"):
        probe.run(args + ["--receipt", str(tmp_path / "never.json")])
