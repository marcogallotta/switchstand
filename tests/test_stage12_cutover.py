import hashlib
import json
from pathlib import Path

import pytest

from switchstand.edge_maintenance import Failed, Unknown
from switchstand.stage12_cutover import Evidence, Stage12Cutover

SHA = "a" * 40


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def _private(path: Path, data: bytes) -> None:
    path.write_bytes(data)
    path.chmod(0o600)


def _manifest(path: Path, *, candidate: str = SHA, title: str = "one") -> dict[str, object]:
    document: dict[str, object] = {
        "schema_version": 1,
        "source_candidate": candidate,
        "rows": [
            {
                "provider_work_id": "1",
                "work_id": None,
                "title": title,
                "completed": False,
                "revision": "r1",
                "routing": {},
                "context": {},
                "dependencies": [],
            }
        ],
        "exceptions": [],
        "counts": {"broad": 1, "bound": 0, "included": 1, "exceptions": 0},
    }
    document["sha256"] = hashlib.sha256(_canonical(document)).hexdigest()
    _private(path, json.dumps(document, sort_keys=True).encode() + b"\n")
    return document


def _subject(tmp_path: Path) -> tuple[Stage12Cutover, Commands]:
    attempt = tmp_path / "attempt"
    attempt.mkdir(mode=0o700)
    first, second = tmp_path / "first.json", tmp_path / "second.json"
    manifest = _manifest(first)
    _private(second, first.read_bytes())
    worksheet = tmp_path / "worksheet.json"
    _private(worksheet, b'{"format_version":1}\n')
    exceptions = hashlib.sha256(_canonical([])).hexdigest()
    commands = Commands(attempt, first)
    evidence = Evidence(
        SHA,
        (first, second),
        exceptions,
        worksheet,
        hashlib.sha256(worksheet.read_bytes()).hexdigest(),
        1,
    )
    subject = Stage12Cutover(attempt, evidence, commands)
    assert manifest["sha256"]
    return subject, commands


class Commands:
    def __init__(self, attempt: Path, manifest: Path):
        self.attempt = attempt
        self.manifest = manifest
        self.calls: list[str] = []
        self.fail: str | None = None

    def _call(self, name: str) -> None:
        self.calls.append(name)
        if self.fail == name:
            raise Failed(name)

    def upgrade_schema(self, receipt: Path) -> Path:
        self._call("upgrade")
        _private(receipt, b'{"outcome":"APPLIED"}\n')
        backup = self.attempt / "database.dump"
        _private(backup, b"database")
        return backup

    def prepare_stage1(self) -> str:
        self._call("prepare")
        return "prepared"

    def capture_final_corpus(self, destination: Path) -> None:
        self._call("capture")
        _private(destination, self.manifest.read_bytes())

    def activate_stage1(self, prepared_digest: str, receipt: Path) -> None:
        assert prepared_digest == "prepared"
        self._call("stage1")
        _private(receipt, b'{"generation":1}\n')

    def validate_stage2(self) -> None:
        self._call("validate-stage2")

    def activate_stage2(self, receipt: Path) -> None:
        self._call("stage2")
        _private(receipt, b'{"generation":1}\n')

    def abort_schema(self, receipt: Path) -> None:
        assert receipt.is_file()
        self._call("abort")


def _boundary(subject: Stage12Cutover) -> str:
    return json.loads(subject.receipt_path.read_text())["terminal_boundary"]


def test_cutover_advances_only_after_durable_subordinate_receipts(tmp_path: Path):
    subject, commands = _subject(tmp_path)
    boundaries: list[str] = []

    subject.run(boundaries.append)

    assert boundaries == ["PRE_MARKER", "POSTGRES_AUTHORITY", "COMPLETE"]
    assert commands.calls == [
        "upgrade",
        "prepare",
        "capture",
        "stage1",
        "validate-stage2",
        "stage2",
    ]
    assert _boundary(subject) == "COMPLETE"
    receipt = json.loads(subject.receipt_path.read_text())
    assert receipt["candidate_sha"] == SHA
    assert receipt["database_backup"] == hashlib.sha256(b"database").hexdigest()
    assert subject.receipt_path.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize(
    ("failure", "expected_boundary"),
    [
        ("prepare", "PRE_MARKER"),
        ("stage1", "PRE_MARKER"),
        ("validate-stage2", "POSTGRES_AUTHORITY"),
        ("stage2", "POSTGRES_AUTHORITY"),
    ],
)
def test_failure_preserves_last_proven_boundary(
    tmp_path: Path, failure: str, expected_boundary: str
):
    subject, commands = _subject(tmp_path)
    commands.fail = failure

    with pytest.raises(Failed):
        subject.run(lambda _boundary: None)

    assert _boundary(subject) == expected_boundary


def test_evidence_mismatch_fails_before_any_command(tmp_path: Path):
    subject, commands = _subject(tmp_path)
    _manifest(subject._evidence.manifests[1], title="changed")  # pyright: ignore[reportPrivateUsage]

    with pytest.raises(Failed, match="do not match"):
        subject.run(lambda _boundary: None)

    assert commands.calls == []


def test_abort_failure_becomes_unknown(tmp_path: Path):
    subject, commands = _subject(tmp_path)
    subject.run(lambda _boundary: None)
    commands.fail = "abort"

    with pytest.raises(Unknown, match="was not proven"):
        subject.abort_pre_authority()
