from pathlib import Path
from uuid import uuid4

from switchstand import stateful_qualifier as qualifier


def test_qualifier_journals_stable_operation_identity_before_effect(tmp_path: Path) -> None:
    attempt = tmp_path / "attempt"
    attempt.mkdir(mode=0o700)
    prepared = {
        "attempt_id": str(uuid4()),
        "work_id": str(uuid4()),
        "observed_revision": "revision",
        "completed": False,
        "stale_operation_id": str(uuid4()),
        "effect_operation_id": str(uuid4()),
    }
    journal = qualifier._load_or_prepare(attempt, prepared)
    effect = qualifier._update_arguments(journal, stale=False)
    assert (attempt / "prepared.json").is_file()
    assert effect["operation_id"] == prepared["effect_operation_id"]
    assert qualifier._load_or_prepare(attempt, prepared) == journal
