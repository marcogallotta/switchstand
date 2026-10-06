import json
from pathlib import Path
from uuid import uuid4

import pytest

from switchstand import human_review_propose
from switchstand.human_review_propose import (
    MAX_CONSEQUENCE_BYTES,
    propose_file,
    read_consequence,
)
from switchstand.human_reviews import HumanReviewConsequence, HumanReviewResult


class Proposer:
    def __init__(self, result: HumanReviewResult):
        self.result = result
        self.seen: HumanReviewConsequence | None = None

    async def propose(self, value: HumanReviewConsequence) -> HumanReviewResult:
        self.seen = value
        return self.result


def consequence() -> HumanReviewConsequence:
    return HumanReviewConsequence(
        package_work_id=uuid4(),
        package_revision="pg_exact",
        implementation_scope=("build the reviewed package",),
        implementation_target="git:exact-head",
        excluded_effects=("deployment", "automatic dispatch"),
    )


def private_json(path: Path, value: HumanReviewConsequence) -> None:
    path.write_text(value.model_dump_json())
    path.chmod(0o600)


async def test_propose_file_reads_private_consequence_and_only_proposes(tmp_path: Path) -> None:
    path = tmp_path / "consequence.json"
    expected = consequence()
    private_json(path, expected)
    state = Proposer(HumanReviewResult(status="DENIED", reason="package_not_found"))

    result = await propose_file(path, state)

    assert result.status == "DENIED"
    assert state.seen == expected


def test_read_consequence_requires_exact_private_regular_file(tmp_path: Path) -> None:
    path = tmp_path / "consequence.json"
    private_json(path, consequence())
    path.chmod(0o640)

    with pytest.raises(ValueError, match="mode-0600 regular file"):
        read_consequence(path)

    path.unlink()
    target = tmp_path / "target.json"
    private_json(target, consequence())
    path.symlink_to(target)
    with pytest.raises(OSError):
        read_consequence(path)


def test_read_consequence_rejects_oversized_or_extra_input(tmp_path: Path) -> None:
    path = tmp_path / "consequence.json"
    path.write_bytes(b" " * (MAX_CONSEQUENCE_BYTES + 1))
    path.chmod(0o600)
    with pytest.raises(ValueError, match="1..65536 bytes"):
        read_consequence(path)

    payload = json.loads(consequence().model_dump_json())
    payload["decision"] = "APPROVED"
    path.write_text(json.dumps(payload))
    with pytest.raises(ValueError, match="Extra inputs are not permitted"):
        read_consequence(path)


def test_cli_reports_nonprepared_result_and_exits_nonzero(monkeypatch, capsys) -> None:
    async def denied(_path: Path) -> HumanReviewResult:
        return HumanReviewResult(status="STALE", reason="package_revision_changed")

    monkeypatch.setattr(human_review_propose, "_propose", denied)

    with pytest.raises(SystemExit) as stopped:
        human_review_propose.run(["consequence.json"])

    assert stopped.value.code == 2
    assert json.loads(capsys.readouterr().out) == {
        "reason": "package_revision_changed",
        "status": "STALE",
    }
