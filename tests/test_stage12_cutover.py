import hashlib
import json
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from switchstand.edge_maintenance import Config, Failed, _offline_boundary
from switchstand.stage12_cutover import (
    ConcreteCommands,
    Evidence,
    Stage12Cutover,
    _binding_digest,
    freeze_evidence,
)

SHA = "a" * 40


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def _private(path: Path, data: bytes) -> None:
    path.write_bytes(data)
    path.chmod(0o600)


def _inputs(tmp_path: Path) -> tuple[Evidence, Path]:
    manifest: dict[str, object] = {
        "schema_version": 1,
        "source_candidate": SHA,
        "rows": [],
        "exceptions": [],
        "counts": {"broad": 0, "bound": 0, "included": 0, "exceptions": 0},
    }
    manifest["sha256"] = hashlib.sha256(_canonical(manifest)).hexdigest()
    encoded = json.dumps(manifest, sort_keys=True).encode() + b"\n"
    first, second = tmp_path / "first.json", tmp_path / "second.json"
    _private(first, encoded)
    _private(second, encoded)
    worksheet = tmp_path / "worksheet.json"
    _private(worksheet, b'{"format_version":1}\n')
    attempt = tmp_path / "attempt"
    attempt.mkdir(mode=0o700)
    evidence = Evidence(
        SHA,
        (first, second),
        str(manifest["sha256"]),
        hashlib.sha256(_canonical([])).hexdigest(),
        worksheet,
        hashlib.sha256(worksheet.read_bytes()).hexdigest(),
        1,
    )
    return evidence, attempt


def test_freezes_exact_evidence_before_effects(tmp_path: Path):
    evidence, attempt = _inputs(tmp_path)

    frozen = freeze_evidence(evidence, attempt)

    assert frozen.corpus_digest == evidence.expected_corpus_digest
    assert frozen.manifests == (attempt / "corpus-a.json", attempt / "corpus-b.json")
    assert frozen.worksheet == attempt / "stage2-worksheet.json"
    assert all(
        path.stat().st_mode & 0o777 == 0o600 for path in (*frozen.manifests, frozen.worksheet)
    )


def test_same_attempt_accepts_only_byte_identical_evidence(tmp_path: Path):
    evidence, attempt = _inputs(tmp_path)
    first = freeze_evidence(evidence, attempt)
    assert freeze_evidence(evidence, attempt) == first

    _private(evidence.manifests[0], evidence.manifests[0].read_bytes() + b" ")
    with pytest.raises(Failed, match="does not match this attempt"):
        freeze_evidence(evidence, attempt)


@pytest.mark.parametrize("boundary", ["PRE_MARKER", "POSTGRES_AUTHORITY", "COMPLETE"])
def test_reconstructed_cutover_restores_durable_backup_for_host_resume(
    tmp_path: Path, boundary: str,
):
    evidence, attempt = _inputs(tmp_path)
    original = Stage12Cutover(attempt, evidence, None)  # type: ignore[arg-type]
    frozen = freeze_evidence(evidence, attempt)
    original.database_backup = "b" * 64
    _private(attempt / "corpus-final.json", evidence.manifests[0].read_bytes())
    proof = {
        "schema_receipt": "c" * 64,
        "stage1_receipt": "d" * 64,
        "stage2_validation": "e" * 64,
        "prepared_import": "f" * 64,
        "final_manifest": evidence.expected_corpus_digest,
        "stage2_receipt": "1" * 64,
    }
    original._write(frozen, boundary, **proof)  # pyright: ignore[reportPrivateUsage]

    resumed = Stage12Cutover(attempt, evidence, None)  # type: ignore[arg-type]
    assert resumed.database_backup == "PENDING"
    assert _offline_boundary(resumed, _config(tmp_path, attempt)) == boundary
    assert resumed.database_backup == "b" * 64


@pytest.mark.parametrize("field", ["candidate", "corpus", "exception", "worksheet"])
def test_reviewed_identity_mismatch_is_rejected(tmp_path: Path, field: str):
    evidence, attempt = _inputs(tmp_path)
    name = {
        "candidate": "candidate_sha",
        "corpus": "expected_corpus_digest",
        "exception": "exception_digest",
        "worksheet": "worksheet_digest",
    }[field]
    value = "b" * 40 if field == "candidate" else "0" * 64

    with pytest.raises(Failed):
        freeze_evidence(replace(evidence, **{name: value}), attempt)  # pyright: ignore[reportCallIssue]


def test_symlink_source_and_nonprivate_attempt_are_rejected(tmp_path: Path):
    evidence, attempt = _inputs(tmp_path)
    link = tmp_path / "link.json"
    link.symlink_to(evidence.manifests[0])
    replaced = replace(evidence, manifests=(link, evidence.manifests[1]))
    with pytest.raises(Failed, match="unavailable"):
        freeze_evidence(replaced, attempt)

    attempt.chmod(0o755)
    with pytest.raises(Failed, match="identity or mode"):
        freeze_evidence(evidence, attempt)


def _config(tmp_path: Path, attempt: Path, *, target: str = "disposable") -> Config:
    root = tmp_path / "proof"
    root.mkdir(exist_ok=True)
    env = root / "edge.env"
    _private(env, b"ASANA_TOKEN=secret\nDATABASE_URL=postgresql:///live\n")
    return Config(
        attempt, tmp_path / "current", SHA, tmp_path / "candidate", SHA,
        root / "candidate-launcher", "b" * 64, root / "launcher", "c" * 64,
        root / "fastmcp", env, target, target_root=root if target == "disposable" else None,
        service="switchstand-rehearsal-proof.service" if target == "disposable" else "switchstand-chatgpt-mcp.service",
        local_url="http://127.0.0.1:18801/mcp" if target == "disposable" else "http://127.0.0.1:8790/mcp",
        caddy="http://127.0.0.1:18802" if target == "disposable" else "http://127.0.0.1:2019",
        public_origin="http://127.0.0.1:18803" if target == "disposable" else "https://laptop.tail46f0b9.ts.net",
        lock_path=root / "lock" if target == "disposable" else Path("/home/marco/.local/state/switchstand/edge-maintenance.lock"),
    )


def test_disposable_database_is_derived_from_exact_namespaced_container(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    evidence, attempt = _inputs(tmp_path)
    subject = ConcreteCommands(_config(tmp_path, attempt), evidence)
    calls: list[list[str]] = []
    working: list[Path | None] = []
    for key in ("PYTHONPATH", "DATABASE_URL", "SWITCHSTAND_BACKUP_DIR"):
        monkeypatch.setenv(key, "/attacker")
    monkeypatch.chdir(tmp_path)
    def run(command: list[str], _environment=None, *, check=True, cwd=None):
        calls.append(command)
        working.append(cwd)
        if command[:2] == ["docker", "inspect"]:
            value = [{"Config": {"Labels": {
                "com.docker.compose.project": "switchstand-rehearsal-proof",
                "com.docker.compose.service": "postgres",
            }}, "NetworkSettings": {"Networks": {
                "switchstand-rehearsal-proof_default": {"IPAddress": "172.30.0.9"}
            }}}]
            return subprocess.CompletedProcess(command, 0, json.dumps(value), "")
        output = f"{SHA}\n/git/common\n" if command[0] == "git" else "cid\n"
        return subprocess.CompletedProcess(command, 0, output, "")

    monkeypatch.setattr(subject, "_run", run)
    environment = subject._env()  # pyright: ignore[reportPrivateUsage]
    assert environment["DATABASE_URL"].endswith("@172.30.0.9/switchstand")
    assert environment["PYTHONPATH"] == str(subject.c.candidate_runtime / "src")
    assert "SWITCHSTAND_BACKUP_DIR" not in environment
    subject._module("switchstand.work_corpus", "compare")  # pyright: ignore[reportPrivateUsage]
    assert calls[-1][:3] == ["/git/.venv/bin/python", "-m", "switchstand.work_corpus"]
    assert working[-1] == subject.c.candidate_runtime
    assert any("switchstand-rehearsal-proof" in command for command in calls)


def test_disposable_database_rejects_live_project_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    evidence, attempt = _inputs(tmp_path)
    assert ConcreteCommands(_config(tmp_path, attempt, target="production"), evidence).target == "production"
    subject = ConcreteCommands(_config(tmp_path, attempt), evidence)
    value = [{"Config": {"Labels": {"com.docker.compose.project": "switchstand",
        "com.docker.compose.service": "postgres"}}, "NetworkSettings": {"Networks": {
        "switchstand-rehearsal-proof_default": {"IPAddress": "172.30.0.9"}}}}]
    monkeypatch.setattr(subject, "_run", lambda command, *args, **kwargs:
        subprocess.CompletedProcess(command, 0, json.dumps(value) if command[:2] == ["docker", "inspect"] else "cid\n", ""))
    with pytest.raises(Failed, match="database target"):
        subject._env()  # pyright: ignore[reportPrivateUsage]


def test_adapter_uses_exact_prepared_evidence_and_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    evidence, attempt = _inputs(tmp_path)
    subject = ConcreteCommands(_config(tmp_path, attempt), evidence)
    _private(attempt / "stage1-prepare.json", b"{}")
    calls: list[tuple[object, ...]] = []

    def module(*arguments, **_kwargs):
        calls.append(arguments)
        stdout = (
            f"prepared_import_sha256={'d' * 64} inserted_count=1\n"
            if arguments[1] == "prepare" else "validated\n"
        )
        return subprocess.CompletedProcess([], 0, stdout, "")

    monkeypatch.setattr(subject, "_module", module)
    frozen = subject._frozen()  # pyright: ignore[reportPrivateUsage]
    subject.validate_stage2_pre_authority(frozen)
    assert subject.prepare_stage1(frozen) == "d" * 64
    subject.cleanup_stage1(frozen)
    rendered = [tuple(str(value) for value in call) for call in calls]
    assert rendered[0][1] == "validate-prepared"
    assert "--expected-worksheet-digest" in rendered[0]
    assert str(attempt / "stage1-prepare.json") in rendered[0]
    assert rendered[1][1] == "prepare" and "--receipt" in rendered[1]
    assert rendered[2][1] == "prepare-cleanup"


@pytest.mark.parametrize(("status", "state"), [(0, "APPLIED"), (3, "ABSENT"), (1, "UNKNOWN"), (2, "UNKNOWN")])
def test_stage1_resume_classifies_exact_reconcile_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, status: int, state: str
):
    evidence, attempt = _inputs(tmp_path)
    subject = ConcreteCommands(_config(tmp_path, attempt), evidence)
    receipt = attempt / "stage1-activation.json"
    _private(receipt, b"{}")
    monkeypatch.setattr(subject, "_module", lambda *args, **kwargs:
        subprocess.CompletedProcess([], status, "", ""))
    assert subject.stage1_state(receipt).state == state


def test_final_corpus_allows_only_null_to_exact_binding(tmp_path: Path):
    def manifest(path: Path, work_id=None, title="same"):
        value = {"schema_version": 1, "rows": [{"work_id": work_id, "title": title}],
                 "counts": {"bound": int(work_id is not None)}}
        value["sha256"] = hashlib.sha256(_canonical(value)).hexdigest()
        _private(path, json.dumps(value).encode())

    before, after = tmp_path / "before", tmp_path / "after"
    manifest(before)
    manifest(after, "10000000-0000-4000-8000-000000000001")
    assert _binding_digest(before, after) == json.loads(after.read_text())["sha256"]
    manifest(after, "10000000-0000-4000-8000-000000000001", "changed")
    with pytest.raises(Failed, match="outside Stage 1"):
        _binding_digest(before, after)


@pytest.mark.parametrize("payload", [b"", b"{", b"[]"])
def test_malformed_schema_receipt_is_unknown(tmp_path: Path, monkeypatch, payload: bytes):
    evidence, attempt = _inputs(tmp_path)
    subject = ConcreteCommands(_config(tmp_path, attempt), evidence)
    receipt = attempt / "schema.json"
    _private(receipt, payload)
    monkeypatch.setattr(subject, "_revision", lambda: "0012_outcome_state")
    monkeypatch.setattr(subject, "_schema", lambda *_args: None)
    assert subject.schema_state(receipt).state == "UNKNOWN"
