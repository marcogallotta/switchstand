"""Load one explicit private review policy for the ordinary edge."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Annotated, Literal, Self

from pydantic import ConfigDict, Field, model_validator

from .agent_mailboxes import agent_name_key
from .contracts import ClosedModel
from .reviews import ReviewGuidelines, ReviewKind, ReviewPolicy
from .secure_file import read_private_bytes

MAX_REVIEW_CONFIG_BYTES = 16 * 1024
REVIEW_CONFIG_PATH_ENV = "SWITCHSTAND_REVIEW_CONFIG_PATH"
ReviewerName = Annotated[str, Field(min_length=1, max_length=80)]


@dataclass(frozen=True, slots=True)
class ReviewEdgeConfig:
    """Explicit default-off review policy dependencies for the ordinary edge."""

    policy: ReviewPolicy
    guidelines: ReviewGuidelines


class InstalledReviewConfig(ClosedModel):
    """Closed versioned envelope for one installed review policy."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal[1]
    policy_version: str = Field(min_length=1, max_length=120)
    reviewer_by_kind: dict[ReviewKind, ReviewerName] = Field(max_length=6)
    guidelines_version: str = Field(min_length=1, max_length=120)
    guidelines_digest: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def valid_identities(self) -> Self:
        if self.policy_version != self.policy_version.strip():
            raise ValueError("review policy version must not have surrounding whitespace")
        if self.guidelines_version != self.guidelines_version.strip():
            raise ValueError("review guidelines version must not have surrounding whitespace")
        for reviewer in self.reviewer_by_kind.values():
            agent_name_key(reviewer)
        return self


def load_review_config(path: Path) -> ReviewEdgeConfig:
    """Load one bounded private review policy without retaining mutable input."""

    payload = read_private_bytes(path, max_bytes=MAX_REVIEW_CONFIG_BYTES)
    envelope = InstalledReviewConfig.model_validate_json(payload)
    policy = ReviewPolicy(
        version=envelope.policy_version,
        reviewer_by_kind=MappingProxyType(dict(envelope.reviewer_by_kind)),
    )
    guidelines = ReviewGuidelines(
        version=envelope.guidelines_version,
        digest=envelope.guidelines_digest,
    )
    return ReviewEdgeConfig(policy=policy, guidelines=guidelines)


def load_review_config_from_environment() -> ReviewEdgeConfig | None:
    """Load explicitly configured review policy, or preserve default-off."""

    raw_path = os.getenv(REVIEW_CONFIG_PATH_ENV)
    if raw_path is None:
        return None
    path = Path(raw_path)
    if not path.is_absolute():
        raise ValueError(f"{REVIEW_CONFIG_PATH_ENV} must be an absolute path")
    return load_review_config(path)
