import json
from pathlib import Path
from types import MappingProxyType

import pytest
from pydantic import ValidationError

from switchstand.review_config_loader import (
    MAX_REVIEW_CONFIG_BYTES,
    REVIEW_CONFIG_PATH_ENV,
    load_review_config,
    load_review_config_from_environment,
)
from switchstand.secure_file import PrivateFileOpenError


def payload(**changes: object) -> dict[str, object]:
    value: dict[str, object] = {
        "schema_version": 1,
        "policy_version": "policy-v1",
        "reviewer_by_kind": {"CODE": "Reviewer"},
        "guidelines_version": "guidelines-v1",
        "guidelines_digest": "a" * 64,
    }
    value.update(changes)
    return value


def write(path: Path, value: object) -> None:
    path.write_text(json.dumps(value), encoding="utf-8")
    path.chmod(0o600)


def test_loads_exact_immutable_partial_policy(tmp_path: Path) -> None:
    path = tmp_path / "reviews.json"
    write(path, payload())

    loaded = load_review_config(path)

    assert loaded.policy.version == "policy-v1"
    assert isinstance(loaded.policy.reviewer_by_kind, MappingProxyType)
    assert loaded.policy.reviewer_by_kind == {"CODE": "Reviewer"}
    assert loaded.guidelines.version == "guidelines-v1"
    assert loaded.guidelines.digest == "a" * 64
    with pytest.raises(TypeError):
        loaded.policy.reviewer_by_kind["CODE"] = "Changed"  # type: ignore[index]


def test_empty_mapping_preserves_reviewer_acquisition(tmp_path: Path) -> None:
    path = tmp_path / "reviews.json"
    write(path, payload(reviewer_by_kind={}))

    assert load_review_config(path).policy.reviewer_name("CODE") is None


def test_environment_path_is_literal_absolute_and_default_off(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv(REVIEW_CONFIG_PATH_ENV, raising=False)
    assert load_review_config_from_environment() is None

    for value in ("reviews.json", "   "):
        monkeypatch.setenv(REVIEW_CONFIG_PATH_ENV, value)
        with pytest.raises(ValueError, match="absolute path"):
            load_review_config_from_environment()

    path = tmp_path / "reviews.json"
    write(path, payload())
    monkeypatch.setenv(REVIEW_CONFIG_PATH_ENV, str(path))
    assert load_review_config_from_environment() == load_review_config(path)

    monkeypatch.setenv(REVIEW_CONFIG_PATH_ENV, f"{path} ")
    with pytest.raises(PrivateFileOpenError):
        load_review_config_from_environment()


@pytest.mark.parametrize(
    "value",
    [
        {},
        payload(schema_version=2),
        payload(unexpected=True),
        payload(policy_version=""),
        payload(policy_version=" policy-v1"),
        payload(reviewer_by_kind={"UNKNOWN": "Reviewer"}),
        payload(reviewer_by_kind={"CODE": ""}),
        payload(reviewer_by_kind={"CODE": "   "}),
        payload(guidelines_version=""),
        payload(guidelines_version="guidelines-v1 "),
        payload(guidelines_digest="A" * 64),
        payload(guidelines_digest="a" * 63),
    ],
)
def test_rejects_malformed_or_open_ended_policy(
    tmp_path: Path, value: object
) -> None:
    path = tmp_path / "reviews.json"
    write(path, value)

    with pytest.raises(ValidationError):
        load_review_config(path)


def test_private_file_boundary_rejects_mode_symlink_and_oversize(tmp_path: Path) -> None:
    path = tmp_path / "reviews.json"
    write(path, payload())
    path.chmod(0o644)
    with pytest.raises(ValueError, match="mode-0600"):
        load_review_config(path)

    target = tmp_path / "target.json"
    write(target, payload())
    link = tmp_path / "link.json"
    link.symlink_to(target)
    with pytest.raises(PrivateFileOpenError):
        load_review_config(link)

    oversized = tmp_path / "oversized.json"
    oversized.write_bytes(b" " * (MAX_REVIEW_CONFIG_BYTES + 1))
    oversized.chmod(0o600)
    with pytest.raises(ValueError, match="exceeds maximum size"):
        load_review_config(oversized)
