import json
from pathlib import Path
from types import MappingProxyType
from uuid import UUID

import pytest
from pydantic import ValidationError

from switchstand.activation_continuity import ActivationContract
from switchstand.activation_contract_loader import (
    MAX_CONTRACT_FILE_BYTES,
    load_activation_contracts,
)

PRODUCT = UUID("30000000-0000-4000-8000-000000000001")
OWNER = UUID("30000000-0000-4000-8000-000000000002")
VERIFIER = UUID("30000000-0000-4000-8000-000000000003")


def contract(**changes: object) -> ActivationContract:
    values: dict[str, object] = {
        "product_work_id": PRODUCT,
        "outcome_key": "release",
        "target_revision": "git:abc",
        "target_phase": "ACTIVATED",
        "return_owner_work_id": OWNER,
        "acceptance_contract_id": "acceptance",
        "contract_revision": "v1",
        "acceptance_verifier_work_id": VERIFIER,
        "adoption_requirement": "NOT_REQUIRED",
        "adoption_actor_work_id": None,
        "lifecycle_authority_work_id": PRODUCT,
    }
    values.update(changes)
    return ActivationContract.model_validate(values)


def write(path: Path, value: object) -> None:
    path.write_text(json.dumps(value), encoding="utf-8")
    path.chmod(0o600)


def envelope(*contracts: ActivationContract, **extra: object) -> dict[str, object]:
    return {
        "schema_version": 1,
        "contracts": [value.model_dump(mode="json") for value in contracts],
        **extra,
    }


def test_loads_immutable_registry_keyed_by_derived_obligation(tmp_path: Path) -> None:
    path = tmp_path / "contracts.json"
    bound = contract()
    write(path, envelope(bound))

    loaded = load_activation_contracts(path)

    assert isinstance(loaded, MappingProxyType)
    assert loaded == {bound.obligation_id: bound}
    with pytest.raises(TypeError):
        loaded[bound.obligation_id] = bound  # type: ignore[index]


@pytest.mark.parametrize(
    "value",
    [
        {},
        {"schema_version": 1, "contracts": []},
        {"schema_version": 2, "contracts": []},
        {"schema_version": 1, "contracts": [], "unexpected": True},
        {"schema_version": 1, "contracts": [{}]},
    ],
)
def test_rejects_empty_wrong_version_extra_and_malformed_envelopes(
    tmp_path: Path, value: object
) -> None:
    path = tmp_path / "contracts.json"
    write(path, value)

    with pytest.raises(ValidationError):
        load_activation_contracts(path)


@pytest.mark.parametrize("payload", [b"", b"not-json"])
def test_rejects_empty_and_malformed_json(tmp_path: Path, payload: bytes) -> None:
    path = tmp_path / "contracts.json"
    path.write_bytes(payload)
    path.chmod(0o600)

    with pytest.raises(ValidationError):
        load_activation_contracts(path)


def test_rejects_duplicate_derived_obligation_ids(tmp_path: Path) -> None:
    path = tmp_path / "contracts.json"
    first = contract()
    duplicate = contract(contract_revision="v2")
    assert first.obligation_id == duplicate.obligation_id
    write(path, envelope(first, duplicate))

    with pytest.raises(ValidationError, match="obligation IDs must be distinct"):
        load_activation_contracts(path)


def test_rejects_oversized_private_file_before_parsing(tmp_path: Path) -> None:
    path = tmp_path / "contracts.json"
    path.write_bytes(b" " * (MAX_CONTRACT_FILE_BYTES + 1))
    path.chmod(0o600)

    with pytest.raises(ValueError, match="exceeds maximum size"):
        load_activation_contracts(path)


def test_preserves_private_file_guards(tmp_path: Path) -> None:
    target = tmp_path / "target.json"
    write(target, envelope(contract()))
    target.chmod(0o640)
    with pytest.raises(ValueError, match="mode-0600 regular file"):
        load_activation_contracts(target)

    target.chmod(0o600)
    link = tmp_path / "contracts.json"
    link.symlink_to(target)
    with pytest.raises(OSError):
        load_activation_contracts(link)
