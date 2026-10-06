from __future__ import annotations

import argparse
from pathlib import Path
from uuid import uuid4

import pytest

from switchstand import stateful_qualifier as qualifier
from switchstand.grants import PrincipalContext
from switchstand.product_currentness import ProductCurrentness
from switchstand.product_currentness_stateful import StatefulQualificationBasis


def _current(current: str, blockers: tuple[str, ...]) -> ProductCurrentness:
    return ProductCurrentness(
        status="ok" if current == "TRUE" else "unknown",
        product_work_id=qualifier.STATEFUL_PRODUCT_WORK_ID,
        current=current,
        contract_revision="stateful-technical-currentness-v1",
        reconciliation_id=uuid4(),
        basis_id="basis",
        conditions=(),
        blockers=blockers,
        reason=None if current == "TRUE" else "evidence_or_coherence_unproved",
    )


async def test_qualifier_journals_before_effect_and_emits_rechecked_receipt(
    monkeypatch, tmp_path: Path,
) -> None:
    work_id, denied_work_id, attempt_id = uuid4(), uuid4(), uuid4()
    token = tmp_path / "token"
    token.write_text("secret")
    token.chmod(0o600)
    key = tmp_path / "key"
    key.write_bytes(b"k" * 32)
    key.chmod(0o600)
    receipt = tmp_path / "qualification.json"
    attempt_dir = tmp_path / "attempt"
    tools = [
        {"name": "work_get", "inputSchema": {}},
        {"name": "work_update", "inputSchema": {}},
        {"name": "outcome_state_update", "inputSchema": {}},
    ]
    tools_digest = qualifier._digest(tools)
    runtime_sha = "d" * 40
    calls: list[tuple[str, dict[str, object]]] = []

    class Client:
        def __init__(self, endpoint, observed_token):
            assert observed_token == "secret"

        def initialize(self):
            pass

        def close(self):
            pass

        def tools(self):
            return tools

        def runtime(self):
            return {"runtime_sha": runtime_sha, "run_id": "run-1"}

        def call(self, name, arguments):
            calls.append((name, arguments))
            if name == "work_get" and arguments["work_id"] == str(work_id):
                if sum(call[0] == "work_update" for call in calls) >= 3:
                    return {"status": "ok", "item": {
                        "id": str(work_id), "revision": "r2", "completed": False,
                    }}
                return {"status": "ok", "item": {
                    "id": str(work_id), "revision": "r1", "completed": False,
                }}
            if name == "work_get":
                return {"status": "denied"}
            assert (attempt_dir / "prepared.json").is_file()
            if arguments["observed_revision"] == "stateful-qualifier-deliberately-stale":
                return {"status": "stale", "effect": "not_sent"}
            return {
                "status": "ok",
                "effect": "applied",
                "receipt": {
                    "resulting_revision": "r2",
                    "principal": {
                        "issuer": "https://issuer.test",
                        "subject": "owner",
                        "client_id": "client",
                        "assurance": "authenticated",
                    },
                },
            }

    class Engine:
        async def dispose(self):
            pass

    class Process:
        returncode = 0

        async def communicate(self):
            return f"{runtime_sha}\n".encode(), b""

    async def subprocess_exec(*_args, **_kwargs):
        return Process()

    class Reader:
        def __init__(self, *_args, qualification_receipt=None, **_kwargs):
            self.qualified = qualification_receipt is not None

        async def qualification_basis(self):
            return StatefulQualificationBasis(
                persistence_token="a" * 64,
                basis_token="b" * 64,
                contract_token="c" * 64,
            )

    async def evaluate(_work_id, reader):
        return _current("TRUE", ()) if reader.qualified else _current(
            "UNKNOWN", ("functional_proof",)
        )

    monkeypatch.setattr(qualifier, "MCPClient", Client)
    monkeypatch.setattr(qualifier.asyncio, "create_subprocess_exec", subprocess_exec)
    monkeypatch.setattr(qualifier, "create_async_engine", lambda _url: Engine())
    monkeypatch.setattr(qualifier, "LiveStatefulEvidenceReader", Reader)
    monkeypatch.setattr(qualifier, "evaluate_stateful_currentness", evaluate)
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://ignored")
    arguments = argparse.Namespace(
        endpoint="https://example.test/mcp",
        token_file=token,
        issuer="https://issuer.test",
        subject="owner",
        client_id="client",
        work_id=work_id,
        denied_work_id=denied_work_id,
        attempt_id=attempt_id,
        attempt_dir=attempt_dir,
        expected_runtime_sha=runtime_sha,
        expected_run_id="run-1",
        expected_tools_sha256=tools_digest,
        expected_migration="0023_activation_continuity",
        key=key,
        receipt=receipt,
        repo=Path.cwd(),
    )

    result = await qualifier._qualify(arguments)
    replayed = await qualifier._qualify(arguments)

    assert result["result"] == "PASS" and replayed == result
    assert receipt.is_file() and (attempt_dir / "result.json").is_file()
    assert (receipt.stat().st_mode & 0o777) == 0o600
    expected_calls = [
        "work_get", "work_get", "work_update", "work_update", "work_update", "work_get",
    ]
    assert [name for name, _ in calls] == expected_calls * 2
    effect_calls = [arguments for name, arguments in calls if name == "work_update"][1:]
    assert effect_calls[0] == effect_calls[1] == effect_calls[3] == effect_calls[4]
    assert effect_calls[0]["operation_id"] == str(
        qualifier.uuid5(qualifier.OPERATION_NAMESPACE, f"{attempt_id}:effect")
    )


async def test_qualifier_rejects_wrong_checkout_before_mcp_or_effects(
    monkeypatch, tmp_path: Path,
) -> None:
    token = tmp_path / "token"
    token.write_text("secret")
    token.chmod(0o600)
    repo = tmp_path / "checkout"
    repo.mkdir()
    commands: list[tuple[object, ...]] = []

    class Process:
        returncode = 0

        async def communicate(self):
            return (b"b" * 40) + b"\n", b""

    async def subprocess_exec(*args, **kwargs):
        commands.append(args)
        assert kwargs == {
            "stdout": qualifier.asyncio.subprocess.PIPE,
            "stderr": qualifier.asyncio.subprocess.PIPE,
        }
        return Process()

    class UnexpectedClient:
        def __init__(self, *_args, **_kwargs):
            pytest.fail("MCP must not be initialized for a mismatched checkout")

    monkeypatch.setattr(qualifier.asyncio, "create_subprocess_exec", subprocess_exec)
    monkeypatch.setattr(qualifier, "MCPClient", UnexpectedClient)
    arguments = argparse.Namespace(
        endpoint="https://example.test/mcp",
        token_file=token,
        repo=repo,
        expected_runtime_sha="a" * 40,
    )

    with pytest.raises(qualifier.QualificationFailure, match="checkout does not match"):
        await qualifier._qualify(arguments)

    assert commands == [("git", "-C", str(repo), "rev-parse", "HEAD")]


def test_attempt_journal_replays_only_exact_prepared_identity(tmp_path: Path) -> None:
    attempt = tmp_path / "attempt"
    attempt.mkdir(mode=0o700)
    expected = {
        "schema": 1,
        "attempt_id": str(uuid4()),
        "observed_revision": "revision",
        "completed": False,
    }

    assert qualifier._load_or_prepare(attempt, expected) == expected
    assert qualifier._load_or_prepare(attempt, expected) == expected
    with pytest.raises(qualifier.QualificationFailure, match="exact arguments"):
        qualifier._load_or_prepare(attempt, expected | {"endpoint": "changed"})


def test_endpoint_and_private_token_fail_closed(tmp_path: Path) -> None:
    assert (
        qualifier._endpoint("https://public.example/switchstand/mcp")
        == "https://public.example/switchstand/mcp"
    )
    with pytest.raises(qualifier.QualificationFailure, match="credential-free"):
        qualifier._endpoint("http://example.test/mcp")
    with pytest.raises(qualifier.QualificationFailure, match="credential-free"):
        qualifier._endpoint("http://127.0.0.1/switchstand/mcp")
    token = tmp_path / "token"
    token.write_text("secret")
    token.chmod(0o640)
    with pytest.raises(ValueError, match="mode-0600"):
        qualifier._private_token(token)


def test_tool_errors_and_effect_principal_mismatches_fail_closed() -> None:
    client = object.__new__(qualifier.MCPClient)
    client._post = lambda *_args, **_kwargs: {  # type: ignore[method-assign]
        "isError": True,
        "structuredContent": {"status": "ok"},
    }
    with pytest.raises(qualifier.QualificationFailure, match="tool error"):
        client.call("work_get", {})

    principal = PrincipalContext(
        issuer="https://issuer.test",
        subject="owner",
        client_id="client",
        assurance="authenticated",
    )
    receipt = {
        "principal": {
            "issuer": principal.issuer,
            "subject": "different-caller",
            "client_id": principal.client_id,
            "assurance": principal.assurance,
        }
    }
    assert not qualifier._receipt_matches_principal(receipt, principal)
