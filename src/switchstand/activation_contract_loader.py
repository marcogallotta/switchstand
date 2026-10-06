"""Load an explicit private set of immutable activation contracts."""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path
from types import MappingProxyType
from typing import Literal
from uuid import UUID

from pydantic import ConfigDict, Field, model_validator

from .activation_continuity import ActivationContract
from .contracts import ClosedModel
from .secure_file import read_private_bytes

MAX_CONTRACT_FILE_BYTES = 64 * 1024
MAX_CONTRACTS = 100
ACTIVATION_CONTRACTS_PATH_ENV = "SWITCHSTAND_ACTIVATION_CONTRACTS_PATH"


class InstalledActivationContracts(ClosedModel):
    """Versioned closed envelope for one explicitly selected installation."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal[1]
    contracts: tuple[ActivationContract, ...] = Field(min_length=1, max_length=MAX_CONTRACTS)

    @model_validator(mode="after")
    def distinct_obligations(self) -> InstalledActivationContracts:
        identities = [contract.obligation_id for contract in self.contracts]
        if len(identities) != len(set(identities)):
            raise ValueError("activation contract obligation IDs must be distinct")
        return self


def load_activation_contracts(path: Path) -> Mapping[UUID, ActivationContract]:
    """Load a closed, bounded contract envelope from one explicit private path."""

    payload = read_private_bytes(path, max_bytes=MAX_CONTRACT_FILE_BYTES)
    envelope = InstalledActivationContracts.model_validate_json(payload)
    return MappingProxyType({contract.obligation_id: contract for contract in envelope.contracts})


def load_activation_contracts_from_environment() -> Mapping[UUID, ActivationContract] | None:
    """Load the one explicitly configured installation, or preserve default-off."""

    raw_path = os.getenv(ACTIVATION_CONTRACTS_PATH_ENV)
    if raw_path is None:
        return None
    path = Path(raw_path)
    if not path.is_absolute():
        raise ValueError(f"{ACTIVATION_CONTRACTS_PATH_ENV} must be an absolute path")
    return load_activation_contracts(path)
