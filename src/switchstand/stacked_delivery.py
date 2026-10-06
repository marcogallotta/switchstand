"""Fail-closed policy primitives for native stacked delivery.

This module decides only reusable review identity and evidence sufficiency. It
does not discover stacks, run CI, merge pull requests, or manufacture evidence.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

ReviewVerdict = Literal["PASS", "FAIL", "UNKNOWN"]
Inertness = Literal["INERT_PROVEN", "RELIANCE_CHANGING", "UNKNOWN"]
_SHA = re.compile(r"[0-9a-f]{40}\Z")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")


@dataclass(frozen=True)
class LayerReviewIdentity:
    """The two identities a focused layer review actually covers."""

    layer_diff_digest: str
    dependency_contract_digest: str


@dataclass(frozen=True)
class FocusedLayerReview:
    identity: LayerReviewIdentity
    verdict: ReviewVerdict


@dataclass(frozen=True)
class LayerQualification:
    """Typed proof for one exact layer subject."""

    subject_sha: str
    causal_quality_passed: bool
    inertness: Inertness
    runtime_boundary_implicated: bool
    runtime_boundary_passed: bool = False


@dataclass(frozen=True)
class StackLandingEvidence:
    """Evidence required before any contiguous part of a V1 stack may land."""

    exact_top_sha: str
    cumulative_review_sha: str | None
    cumulative_quality_sha: str | None
    focused_reviews_current: bool
    layer_qualifications_passed: bool


def focused_review_is_current(
    review: FocusedLayerReview,
    current: LayerReviewIdentity,
    *,
    conflict_resolution_occurred: bool = False,
) -> bool:
    """Return whether a focused verdict survives a restack."""

    identities_valid = all(
        _DIGEST.fullmatch(value) is not None
        for value in (
            review.identity.layer_diff_digest,
            review.identity.dependency_contract_digest,
            current.layer_diff_digest,
            current.dependency_contract_digest,
        )
    )
    return (
        identities_valid
        and review.verdict == "PASS"
        and not conflict_resolution_occurred
        and review.identity == current
    )


def layer_qualification_is_sufficient(evidence: LayerQualification) -> bool:
    """Apply proportional inert qualification without treating UNKNOWN as proof."""

    if _SHA.fullmatch(evidence.subject_sha) is None or not evidence.causal_quality_passed:
        return False
    if evidence.inertness != "INERT_PROVEN":
        return False
    return not evidence.runtime_boundary_implicated or evidence.runtime_boundary_passed


def stack_is_ready_to_land(evidence: StackLandingEvidence) -> bool:
    """Enforce the V1 no-early-partial-landing boundary."""

    return (
        _SHA.fullmatch(evidence.exact_top_sha) is not None
        and _SHA.fullmatch(evidence.cumulative_review_sha or "") is not None
        and _SHA.fullmatch(evidence.cumulative_quality_sha or "") is not None
        and evidence.focused_reviews_current
        and evidence.layer_qualifications_passed
        and evidence.cumulative_review_sha == evidence.exact_top_sha
        and evidence.cumulative_quality_sha == evidence.exact_top_sha
    )
