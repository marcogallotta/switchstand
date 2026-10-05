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
