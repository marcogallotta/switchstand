import hashlib
import json
from dataclasses import replace
from pathlib import Path

import pytest

from switchstand.edge_maintenance import Failed
from switchstand.stage12_cutover import Evidence, freeze_evidence

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
