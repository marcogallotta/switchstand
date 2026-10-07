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
EvidenceDimension = Literal[
    "LAYER_CAUSAL_QUALITY",
    "INERTNESS_NON_RELIANCE",
    "CUMULATIVE_TOP_QUALITY",
    "BROAD_QUALITY",
    "RUNTIME_LIFECYCLE",
    "WAKEFUL_REAL_HOST",
]
_SHA = re.compile(r"[0-9a-f]{40}\Z")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
EVIDENCE_DIMENSIONS: frozenset[str] = frozenset({
    "LAYER_CAUSAL_QUALITY", "INERTNESS_NON_RELIANCE", "CUMULATIVE_TOP_QUALITY",
    "BROAD_QUALITY", "RUNTIME_LIFECYCLE", "WAKEFUL_REAL_HOST",
})


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
class ProportionalQualification:
    """Typed evidence for one layer without creating another landing decision."""

    subject_sha: str
    required: frozenset[EvidenceDimension]
    passed: frozenset[EvidenceDimension]
    unknown: frozenset[EvidenceDimension] = frozenset()
    composition_sha: str | None = None


def proportional_qualification_is_sufficient(evidence: ProportionalQualification) -> bool:
    """Accept only exact, typed proof; missing or UNKNOWN dimensions fail closed."""

    return (
        _SHA.fullmatch(evidence.subject_sha) is not None
        and evidence.required <= EVIDENCE_DIMENSIONS
        and evidence.passed <= EVIDENCE_DIMENSIONS
        and evidence.unknown <= EVIDENCE_DIMENSIONS
        and (
            "CUMULATIVE_TOP_QUALITY" not in evidence.required
            or _SHA.fullmatch(evidence.composition_sha or "") is not None
        )
        and not evidence.unknown
        and evidence.required <= evidence.passed
    )


@dataclass(frozen=True)
class StackLandingEvidence:
    """Evidence required before any contiguous part of a V1 stack may land."""

    exact_top_sha: str
    exact_composition_sha: str
    cumulative_review_sha: str | None
    cumulative_quality_sha: str | None
    focused_reviews_current: bool
    required_dimensions: frozenset[EvidenceDimension]
    layer_qualifications: tuple[ProportionalQualification, ...]


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

    passed = frozenset(
        dimension
        for item in evidence.layer_qualifications
        if item.subject_sha == evidence.exact_top_sha
        and (
            "CUMULATIVE_TOP_QUALITY" not in item.passed
            or item.composition_sha == evidence.exact_composition_sha
        )
        for dimension in item.passed
        if proportional_qualification_is_sufficient(item)
    )
    return (
        _SHA.fullmatch(evidence.exact_top_sha) is not None
        and _SHA.fullmatch(evidence.exact_composition_sha) is not None
        and _SHA.fullmatch(evidence.cumulative_review_sha or "") is not None
        and _SHA.fullmatch(evidence.cumulative_quality_sha or "") is not None
        and evidence.focused_reviews_current
        and bool(evidence.layer_qualifications)
        and bool(evidence.required_dimensions)
        and evidence.required_dimensions <= EVIDENCE_DIMENSIONS
        and evidence.required_dimensions <= passed
        and all(
            proportional_qualification_is_sufficient(item)
            for item in evidence.layer_qualifications
        )
        and all(
            "CUMULATIVE_TOP_QUALITY" not in item.required
            or item.composition_sha == evidence.exact_composition_sha
            for item in evidence.layer_qualifications
        )
        and evidence.cumulative_review_sha == evidence.exact_top_sha
        and evidence.cumulative_quality_sha == evidence.exact_top_sha
    )
