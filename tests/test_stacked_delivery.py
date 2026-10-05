import os
import subprocess
from pathlib import Path

from switchstand.stacked_delivery import (
    FocusedLayerReview,
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


def test_proportional_inert_qualification_fails_closed() -> None:
    inert = LayerQualification("a" * 40, True, "INERT_PROVEN", False)
    assert layer_qualification_is_sufficient(inert)
    assert layer_qualification_is_sufficient(
        LayerQualification("a" * 40, True, "INERT_PROVEN", True, True)
    )
    assert not layer_qualification_is_sufficient(
        LayerQualification("a" * 40, True, "UNKNOWN", False)
    )
    assert not layer_qualification_is_sufficient(
        LayerQualification("a" * 40, True, "RELIANCE_CHANGING", False)
    )
    assert not layer_qualification_is_sufficient(
        LayerQualification("a" * 40, True, "INERT_PROVEN", True, False)
    )
    assert not layer_qualification_is_sufficient(
        LayerQualification("not-a-sha", True, "INERT_PROVEN", False)
    )


def test_no_layer_lands_before_exact_cumulative_top_proof() -> None:
    ready = StackLandingEvidence("a" * 40, "a" * 40, "a" * 40, True, True)
    assert stack_is_ready_to_land(ready)
    assert not stack_is_ready_to_land(
        StackLandingEvidence("a" * 40, "a" * 40, None, True, True)
    )
    assert not stack_is_ready_to_land(
        StackLandingEvidence("a" * 40, "b" * 40, "a" * 40, True, True)
    )
    assert not stack_is_ready_to_land(
        StackLandingEvidence("a" * 40, "a" * 40, "a" * 40, False, True)
    )
    assert not stack_is_ready_to_land(
        StackLandingEvidence("x", "x", "x", True, True)
    )
