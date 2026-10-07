"""Fail-closed policy primitives for native stacked delivery.

This module decides only reusable review identity and evidence sufficiency. It
does not discover stacks, run CI, merge pull requests, or manufacture evidence.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

ReviewVerdict = Literal["PASS", "FAIL", "UNKNOWN"]
StackLayer = Literal["A", "B", "C", "D"]
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
EVIDENCE_DIMENSIONS: frozenset[EvidenceDimension] = frozenset({
    "LAYER_CAUSAL_QUALITY", "INERTNESS_NON_RELIANCE", "CUMULATIVE_TOP_QUALITY",
    "BROAD_QUALITY", "RUNTIME_LIFECYCLE", "WAKEFUL_REAL_HOST",
})
REQUIRED_LANDING_DIMENSIONS = EVIDENCE_DIMENSIONS
REQUIRED_REVIEW_DIMENSIONS = frozenset({"LAYER_CAUSAL_QUALITY", "INERTNESS_NON_RELIANCE"})
REQUIRED_STACK_LAYERS: frozenset[StackLayer] = frozenset({"A", "B", "C", "D"})


@dataclass(frozen=True)
class LayerReviewIdentity:
    """The two identities a focused layer review actually covers."""

    layer_diff_digest: str
    dependency_contract_digest: str


@dataclass(frozen=True)
class FocusedLayerReview:
    identity: LayerReviewIdentity
    verdict: ReviewVerdict
    layer: StackLayer | None = None
    reviewed_dimensions: frozenset[EvidenceDimension] = frozenset()


@dataclass(frozen=True)
class FocusedReviewCheck:
    review: FocusedLayerReview
    current: LayerReviewIdentity
    conflict_resolution_occurred: bool = False


@dataclass(frozen=True)
class LayerQualification:
    """Known typed evidence for one exact candidate subject."""

    subject_sha: str
    passed: frozenset[EvidenceDimension]
    known: bool = True
    composition_sha: str | None = None


def layer_qualification_is_sufficient(
    evidence: LayerQualification,
    required: frozenset[EvidenceDimension] = frozenset(),
) -> bool:
    """Reject malformed or UNKNOWN evidence without creating a second authority."""

    return (
        _SHA.fullmatch(evidence.subject_sha) is not None
        and evidence.known
        and bool(evidence.passed)
        and evidence.passed <= EVIDENCE_DIMENSIONS
        and required <= evidence.passed
        and (
            "CUMULATIVE_TOP_QUALITY" not in evidence.passed
            or _SHA.fullmatch(evidence.composition_sha or "") is not None
        )
    )


def combine_layer_qualifications(
    subject_sha: str, composition_sha: str, receipts: tuple[LayerQualification, ...],
) -> LayerQualification:
    """Compose independently sourced receipts only when their exact bindings agree."""

    known = bool(receipts) and all(
        layer_qualification_is_sufficient(item)
        and item.subject_sha == subject_sha
        and item.composition_sha in {None, composition_sha}
        for item in receipts
    )
    return LayerQualification(
        subject_sha, frozenset(dimension for item in receipts for dimension in item.passed),
        known, composition_sha,
    )


@dataclass(frozen=True)
class StackLandingEvidence:
    """Evidence required before any contiguous part of a V1 stack may land."""

    exact_top_sha: str
    exact_composition_sha: str
    cumulative_review_sha: str | None
    cumulative_quality_sha: str | None
    focused_reviews: tuple[FocusedReviewCheck, ...]
    qualifications: tuple[LayerQualification, ...]


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


def focused_review_evidence(
    subject_sha: str, checks: tuple[FocusedReviewCheck, ...],
) -> LayerQualification:
    """Emit review dimensions only from non-empty, exact-current review bindings."""

    layers = {check.review.layer for check in checks}
    identities = {check.review.identity for check in checks}
    known = (
        frozenset(layers) == REQUIRED_STACK_LAYERS
        and len(checks) == len(layers)
        and len(checks) == len(identities)
        and all(
            REQUIRED_REVIEW_DIMENSIONS <= check.review.reviewed_dimensions
            and focused_review_is_current(
                check.review, check.current,
                conflict_resolution_occurred=check.conflict_resolution_occurred,
            )
            for check in checks
        )
    )
    passed: frozenset[EvidenceDimension] = (
        frozenset({"LAYER_CAUSAL_QUALITY", "INERTNESS_NON_RELIANCE"})
        if known else frozenset()
    )
    return LayerQualification(subject_sha, passed, known=known)


def stack_is_ready_to_land(evidence: StackLandingEvidence) -> bool:
    """Enforce the V1 no-early-partial-landing boundary."""

    qualification = combine_layer_qualifications(
        evidence.exact_top_sha,
        evidence.exact_composition_sha,
        (*evidence.qualifications, focused_review_evidence(
            evidence.exact_top_sha, evidence.focused_reviews,
        )),
    )
    return (
        _SHA.fullmatch(evidence.exact_top_sha) is not None
        and _SHA.fullmatch(evidence.exact_composition_sha) is not None
        and _SHA.fullmatch(evidence.cumulative_review_sha or "") is not None
        and _SHA.fullmatch(evidence.cumulative_quality_sha or "") is not None
        and layer_qualification_is_sufficient(qualification, REQUIRED_LANDING_DIMENSIONS)
        and evidence.cumulative_review_sha == evidence.exact_top_sha
        and evidence.cumulative_quality_sha == evidence.exact_top_sha
    )
