import os
import subprocess
from dataclasses import replace
from pathlib import Path

from switchstand.stacked_delivery import (
    EvidenceDimension,
    FocusedLayerReview,
    FocusedReviewCheck,
    LayerQualification,
    LayerReviewIdentity,
    StackLandingEvidence,
    focused_review_is_current,
    layer_qualification_is_sufficient,
    stack_is_ready_to_land,
)

IDENTITY = LayerReviewIdentity("a" * 64, "b" * 64)
ROOT = Path(__file__).parents[1]


def _git(repo: Path, *args: str) -> str:
    env = {**os.environ, "GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "test@example.com",
           "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "test@example.com"}
    return subprocess.run(
        ["git", *args], cwd=repo, env=env, check=True, text=True, capture_output=True
    ).stdout.strip()


def _commit(repo: Path, name: str, parent: str | None = None, other: str | None = None) -> str:
    tree = _git(repo, "write-tree")
    args = ["commit-tree", tree, "-m", name]
    if parent:
        args += ["-p", parent]
    if other:
        args += ["-p", other]
    return _git(repo, *args)


def test_quality_composition_verifier_accepts_native_stack_chain(tmp_path: Path) -> None:
    _git(tmp_path, "init")
    stack_base = _commit(tmp_path, "stack base")
    layer_a = _commit(tmp_path, "layer a", stack_base)
    prefix = _commit(tmp_path, "prefix", stack_base, layer_a)
    layer_b = _commit(tmp_path, "layer b", layer_a)
    composition = _commit(tmp_path, "composition", prefix, layer_b)
    _git(tmp_path, "checkout", "--detach", composition)

    subprocess.run(
        [ROOT / "scripts/verify-quality-composition", composition, layer_b, layer_a,
         stack_base, "2"],
        cwd=tmp_path,
        check=True,
    )

    rejected = subprocess.run(
        [ROOT / "scripts/verify-quality-composition", composition, layer_b, layer_a,
         "f" * 40, "2"],
        cwd=tmp_path,
        check=False,
    )
    assert rejected.returncode != 0


def test_focused_review_reuse_requires_unchanged_delta_and_dependency_contract() -> None:
    passed = FocusedLayerReview(IDENTITY, "PASS")

    assert focused_review_is_current(passed, IDENTITY)
    assert not focused_review_is_current(
        passed, LayerReviewIdentity("c" * 64, "b" * 64)
    )
    assert not focused_review_is_current(
        passed, LayerReviewIdentity("a" * 64, "d" * 64)
    )
    assert not focused_review_is_current(passed, IDENTITY, conflict_resolution_occurred=True)
    assert not focused_review_is_current(FocusedLayerReview(IDENTITY, "UNKNOWN"), IDENTITY)
    assert not focused_review_is_current(
        FocusedLayerReview(LayerReviewIdentity("", ""), "PASS"),
        LayerReviewIdentity("", ""),
    )


def test_typed_proportional_dimensions_fail_closed() -> None:
    passed: frozenset[EvidenceDimension] = frozenset({
        "LAYER_CAUSAL_QUALITY", "INERTNESS_NON_RELIANCE",
    })
    assert layer_qualification_is_sufficient(LayerQualification("a" * 40, passed), passed)
    invalid = (
        LayerQualification("a" * 40, frozenset()),
        LayerQualification("a" * 40, passed, known=False),
        LayerQualification("not-a-sha", passed),
        LayerQualification("a" * 40, frozenset({"invented"})),  # type: ignore[arg-type]
    )
    assert not any(layer_qualification_is_sufficient(item) for item in invalid)


def test_no_layer_lands_before_exact_cumulative_top_proof() -> None:
    required: frozenset[EvidenceDimension] = frozenset({
        "CUMULATIVE_TOP_QUALITY", "BROAD_QUALITY", "RUNTIME_LIFECYCLE",
        "WAKEFUL_REAL_HOST",
    })
    catalogue = LayerQualification(
        "a" * 40, required - {"WAKEFUL_REAL_HOST"}, composition_sha="c" * 40,
    )
    host = LayerQualification("a" * 40, frozenset({"WAKEFUL_REAL_HOST"}))
    reviews = tuple(
        FocusedReviewCheck(
            FocusedLayerReview(
                identity, "PASS", layer,
                frozenset({"LAYER_CAUSAL_QUALITY", "INERTNESS_NON_RELIANCE"}),
            ), identity,
        )
        for layer, identity in zip(
            ("A", "B", "C", "D"),
            (IDENTITY, LayerReviewIdentity("c" * 64, "d" * 64),
             LayerReviewIdentity("e" * 64, "f" * 64), LayerReviewIdentity("1" * 64, "2" * 64)),
            strict=True,
        )
    )
    ready = StackLandingEvidence(
        "a" * 40, "c" * 40, "a" * 40, "a" * 40, reviews, (catalogue, host),
    )
    assert stack_is_ready_to_land(ready)
    rejected = (
        replace(ready, cumulative_quality_sha=None),
        replace(ready, cumulative_review_sha="b" * 40),
        replace(ready, focused_reviews=reviews[:-1]),
        replace(ready, focused_reviews=(*reviews[:-1], reviews[0])),
        replace(ready, focused_reviews=(replace(
            reviews[0], review=replace(
                reviews[0].review, reviewed_dimensions=frozenset({"LAYER_CAUSAL_QUALITY"}),
            ),
        ), *reviews[1:])),
        replace(ready, focused_reviews=(replace(
            reviews[0], current=LayerReviewIdentity("3" * 64, "b" * 64),
        ), *reviews[1:])),
        replace(ready, focused_reviews=(replace(
            reviews[0], current=LayerReviewIdentity("a" * 64, "3" * 64),
        ), *reviews[1:])),
        replace(ready, focused_reviews=(replace(
            reviews[0], conflict_resolution_occurred=True,
        ), *reviews[1:])),
        replace(ready, qualifications=(catalogue, replace(host, subject_sha="b" * 40))),
        replace(ready, qualifications=(replace(catalogue, composition_sha="d" * 40), host)),
    )
    assert not any(stack_is_ready_to_land(item) for item in rejected)

    for dimension in required:
        reduced = replace(
            catalogue,
            passed=catalogue.passed - {dimension},
        ) if dimension != "WAKEFUL_REAL_HOST" else catalogue
        receipts = (reduced, host) if dimension != "WAKEFUL_REAL_HOST" else (catalogue,)
        assert not stack_is_ready_to_land(replace(ready, qualifications=receipts))
