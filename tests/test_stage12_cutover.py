import hashlib
import json
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from switchstand.edge_maintenance import Config, Failed
from switchstand.stage12_cutover import ConcreteCommands, Evidence, freeze_evidence

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

    def run(command: list[str], _environment=None, *, check=True):
        calls.append(command)
        if command[:2] == ["docker", "inspect"]:
            value = [{"Config": {"Labels": {
                "com.docker.compose.project": "switchstand-rehearsal-proof",
                "com.docker.compose.service": "postgres",
            }}, "NetworkSettings": {"Networks": {
                "switchstand-rehearsal-proof_default": {"IPAddress": "172.30.0.9"}
            }}}]
            return subprocess.CompletedProcess(command, 0, json.dumps(value), "")
        return subprocess.CompletedProcess(command, 0, "/git/common\n" if command[0] == "git" else "cid\n", "")

    monkeypatch.setattr(subject, "_run", run)
    environment = subject._env()  # pyright: ignore[reportPrivateUsage]
    assert environment["DATABASE_URL"].endswith("@172.30.0.9/switchstand")
    assert any("switchstand-rehearsal-proof" in command for command in calls)


def test_disposable_database_rejects_live_project_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    evidence, attempt = _inputs(tmp_path)
    subject = ConcreteCommands(_config(tmp_path, attempt), evidence)
    value = [{"Config": {"Labels": {"com.docker.compose.project": "switchstand",
        "com.docker.compose.service": "postgres"}}, "NetworkSettings": {"Networks": {
        "switchstand-rehearsal-proof_default": {"IPAddress": "172.30.0.9"}}}}]
    monkeypatch.setattr(subject, "_run", lambda command, *args, **kwargs:
        subprocess.CompletedProcess(command, 0, json.dumps(value) if command[:2] == ["docker", "inspect"] else "cid\n", ""))
    with pytest.raises(Failed, match="database target"):
        subject._env()  # pyright: ignore[reportPrivateUsage]


@pytest.mark.parametrize(("status", "state"), [(0, "APPLIED"), (1, "ABSENT"), (2, "UNKNOWN")])
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
