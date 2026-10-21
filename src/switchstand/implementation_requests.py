"""Default-off authorization facade for approved implementation task requests."""

from __future__ import annotations

from contextlib import nullcontext
from typing import Literal, cast
from uuid import UUID

from pydantic import model_validator
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from .canonical_relations import work_dependencies
from .canonical_work import CanonicalWorkRepository, canonical_revision
from .contracts import ClosedModel
from .grant_state import work_grants
from .grants import PrincipalContext, WorkGrant
from .human_reviews import (
    approved_baseline_in_transaction,
    current_approved_baseline_in_transaction,
    human_review_consequences,
    human_review_record,
)
from .human_trajectory import HumanTrajectoryStore
from .priority_claims import priority_claims
from .reviews import ReviewOccurrenceState
from .task_runs import ImplementationTaskRequest, TaskRunRequest, TaskRunState

_HIHR_PREFIX = "hihr/"
_HIHR_TRAJECTORY_PREFIX = "hihr/1.0/trajectory/"


def _bound_trajectory(source_refs: tuple[str, ...]) -> tuple[UUID, UUID] | Literal["INVALID"] | None:
    """Optional V1 continuity binding, not evidence of authenticated human words."""
    matches = tuple(value for value in source_refs if value.startswith(_HIHR_PREFIX))
    if not matches:
        return None  # Existing packages keep their current admission contract.
    if len(matches) != 1 or not matches[0].startswith(_HIHR_TRAJECTORY_PREFIX):
        return "INVALID"
    parts = matches[0][len(_HIHR_TRAJECTORY_PREFIX):].split("/")
    if len(parts) != 2:
        return "INVALID"
    try:
        ids = (UUID(parts[0]), UUID(parts[1]))
    except ValueError:
        return "INVALID"
    return ids if all(str(value) == part for value, part in zip(ids, parts, strict=True)) else "INVALID"


class ImplementationRequestResult(ClosedModel):
    status: Literal["APPLIED", "REPLAYED", "STALE", "DENIED", "UNKNOWN", "CONFLICT"]
    implementation_request_id: UUID | None = None
    task_run_request_id: UUID | None = None
    delivery_state: Literal["PENDING"] | None = None
    next_action: str | None = None
    reason: str | None = None

    @model_validator(mode="after")
    def exact_shape(self) -> ImplementationRequestResult:
        ok = self.status in {"APPLIED", "REPLAYED"}
        payload = (
            self.implementation_request_id,
            self.task_run_request_id,
            self.delivery_state,
            self.next_action,
        )
        if ok != (all(value is not None for value in payload) and self.reason is None):
            raise ValueError("implementation request result shape is inconsistent")
        if not ok and (any(value is not None for value in payload) or self.reason is None):
            raise ValueError("failed implementation request requires only a reason")
        return self


class ImplementationRequestState:
    """Validate current authorization and atomically reuse task-run admission."""

    def __init__(
        self, engine: AsyncEngine, works: CanonicalWorkRepository,
        review_occurrences: ReviewOccurrenceState,
    ):
        self.engine = engine
        self.works = works
        self.review_occurrences = review_occurrences
        self.tasks = TaskRunState(engine, works)

    @staticmethod
    def _success(
        status: Literal["APPLIED", "REPLAYED"], request: TaskRunRequest
    ) -> ImplementationRequestResult:
        return ImplementationRequestResult(
            status=status,
            implementation_request_id=UUID(
                str(request.result_contract["implementation_request_id"])
            ),
            task_run_request_id=request.request_id,
            delivery_state="PENDING",
            next_action="await managed worker pickup",
        )

    async def request(
        self,
        principal: PrincipalContext,
        operation_id: UUID,
        package_work_id: UUID,
        observed_revision: str,
        *,
        transaction: AsyncConnection | None = None,
        current_request: TaskRunRequest | None = None,
    ) -> ImplementationRequestResult:
        try:
            async with (self.engine.begin() if transaction is None else nullcontext(transaction)) as connection:
                replay = await self.tasks.get_operation_in_transaction(connection, operation_id)
                if replay.request is not None and current_request is None:
                    request = replay.request
                    exact = (
                        request.task_kind == "IMPLEMENTATION"
                        and request.requester_work_id == package_work_id
                        and request.execution_work_id == package_work_id
                        and request.observed_revision == observed_revision
                    )
                    if not exact:
                        return ImplementationRequestResult(
                            status="CONFLICT", reason="operation_identity_conflict"
                        )
                    if not str(request.send_authority_ref).endswith(f"/{principal.key}"):
                        return ImplementationRequestResult(
                            status="DENIED", reason="send_authority_mismatch"
                        )
                    return self._success("REPLAYED", request)
                if replay.request is None and replay.reason != "request_not_found":
                    return ImplementationRequestResult(
                        status="UNKNOWN", reason="task_run_replay_unavailable"
                    )

                package = await self.works.get_locked(connection, package_work_id)
                if package is None:
                    return ImplementationRequestResult(status="DENIED", reason="package_not_found")
                if canonical_revision(package.work_id, package.row_version) != observed_revision:
                    return ImplementationRequestResult(
                        status="STALE", reason="package_revision_changed"
                    )
                if package.completed or package.lifecycle_state != "CURRENT":
                    return ImplementationRequestResult(status="DENIED", reason="package_not_active")
                if current_request is not None:
                    # The package lock also serializes dependency and HUMAN WORK claim writes.
                    held = await connection.scalar(select(priority_claims.c.claim_id).where(
                        priority_claims.c.subject_kind == "WORK",
                        priority_claims.c.subject_id == package_work_id,
                        priority_claims.c.claim_kind == "HUMAN_PRIORITY",
                        priority_claims.c.relation_kind == "HOLD",
                        priority_claims.c.state == "CURRENT",
                    ).limit(1))
                    if held is not None:
                        return ImplementationRequestResult(status="DENIED", reason="human_priority_hold")
                    dependency = await connection.scalar(select(work_dependencies.c.depends_on_work_id).where(
                        work_dependencies.c.work_id == package_work_id
                    ).limit(1))
                    if dependency is not None:
                        return ImplementationRequestResult(
                            status="UNKNOWN", reason="dependency_eligibility_unavailable"
                        )
                reviews = (
                    (
                        await connection.execute(
                            select(human_review_consequences)
                            .where(
                                human_review_consequences.c.package_work_id == package_work_id,
                                human_review_consequences.c.package_revision == observed_revision,
                            )
                            .with_for_update()
                        )
                    )
                    .mappings()
                    .all()
                )
                if len(reviews) > 1:
                    return ImplementationRequestResult(
                        status="UNKNOWN", reason="human_review_ambiguous"
                    )
                exact_review = human_review_record(reviews[0]) if reviews else None
                current_baseline = await current_approved_baseline_in_transaction(
                    connection, package_work_id
                )
                legacy_review = exact_review if exact_review is not None and (
                    exact_review.consequence.baseline is None
                    and exact_review.decision == "APPROVED"
                    and exact_review.state == "READY_FOR_IMPLEMENTATION"
                ) else None
                baseline_proof = (
                    await self.review_occurrences.baseline_pass_status_in_transaction(
                        connection, package_work_id, observed_revision
                    )
                )
                if baseline_proof.status == "UNKNOWN":
                    return ImplementationRequestResult(
                        status="UNKNOWN", reason="baseline_review_unavailable"
                    )
                baseline_review = None
                if baseline_proof.status == "PASS":
                    assert baseline_proof.baseline_id is not None
                    baseline_review = await approved_baseline_in_transaction(
                        connection, baseline_proof.baseline_id, package_work_id
                    )
                    if baseline_review is None:
                        return ImplementationRequestResult(
                            status="DENIED", reason="approved_baseline_not_found"
                        )
                    if current_baseline is None or (
                        current_baseline.consequence_id != baseline_review.consequence_id
                    ):
                        return ImplementationRequestResult(
                            status="DENIED", reason="approved_baseline_not_found"
                        )
                    legacy_review = None
                elif legacy_review is None:
                    reason = ("baseline_review_not_passed" if current_baseline is not None
                              else "human_review_not_approved")
                    return ImplementationRequestResult(status="DENIED", reason=reason)

                # An explicitly HIHR-bound package must retain its current trajectory.
                # Do not turn missing chat provenance into a global admission hold.
                hihr_binding: tuple[UUID, UUID] | None = None
                if baseline_review is not None:
                    baseline = baseline_review.consequence.baseline
                    if baseline is None:
                        return ImplementationRequestResult(
                            status="UNKNOWN", reason="approved_baseline_unavailable"
                        )
                    binding = _bound_trajectory(baseline.human_source_refs)
                    if binding == "INVALID":
                        return ImplementationRequestResult(
                            status="DENIED", reason="hihr_binding_invalid"
                        )
                    if binding is not None:
                        checked = await HumanTrajectoryStore(self.engine).check_in_transaction(
                            connection, binding[0], binding[1],
                        )
                        if checked.status == "STALE":
                            return ImplementationRequestResult(
                                status="DENIED", reason="hihr_trajectory_stale"
                            )
                        if checked.status != "CURRENT":
                            return ImplementationRequestResult(
                                status="UNKNOWN", reason="hihr_trajectory_unavailable"
                            )
                        hihr_binding = binding

                raw_grant = (
                    await connection.execute(
                        select(work_grants.c.document)
                        .where(work_grants.c.principal_key == principal.key)
                        .with_for_update()
                    )
                ).scalar_one_or_none()
                if raw_grant is None:
                    return ImplementationRequestResult(status="DENIED", reason="no_send_authority")
                grant = WorkGrant.model_validate(raw_grant)
                if not grant.current():
                    return ImplementationRequestResult(
                        status="STALE", reason="send_authority_not_current"
                    )
                if (
                    grant.principal != principal
                    or grant.authority.active_work_id != package_work_id
                    or "implementation_request" not in grant.operations
                ):
                    return ImplementationRequestResult(status="DENIED", reason="no_send_authority")

                if legacy_review is not None:
                    pass_status = await self.review_occurrences.pass_status_in_transaction(
                        connection, package_work_id, observed_revision, "IMPLEMENTATION", None
                    )
                    if pass_status == "UNKNOWN":
                        return ImplementationRequestResult(
                            status="UNKNOWN", reason="independent_review_unavailable"
                        )
                    if pass_status != "PASS":
                        return ImplementationRequestResult(
                            status="DENIED", reason="independent_review_not_passed"
                        )
                    review = legacy_review
                    continuation_effects = ("IMPLEMENT",)
                    authorization_ref = (
                        f"human-review/{review.consequence_id}/{review.consequence_digest}"
                    )
                else:
                    assert baseline_review is not None
                    review = baseline_review
                    baseline = review.consequence.baseline
                    if baseline is None:  # The approved reader promises V2; fail closed if violated.
                        return ImplementationRequestResult(
                            status="UNKNOWN", reason="approved_baseline_unavailable"
                        )
                    continuation_effects = baseline.continuation_effects
                    authorization_ref = (
                        f"human-review-v2/{review.consequence_id}/{review.consequence_digest}"
                        f"/review/{baseline_proof.review_id}/{baseline_proof.verdict_digest}"
                    )
                if current_request is not None and (
                    current_request.authorization_ref != authorization_ref
                    or current_request.send_authority_ref
                    != f"grant/{grant.id}/{grant.version}/{principal.key}"
                ):
                    return ImplementationRequestResult(
                        status="STALE", reason="implementation_authority_changed"
                    )
                milestone_for = {"IMPLEMENT": "IMPLEMENTED", "LAND": "MERGED",
                                 "ACTIVATE": "ACTIVATED"}
                stable = await self.tasks.get_implementation_in_transaction(
                    connection, package_work_id, observed_revision, authorization_ref
                )
                if stable.request is not None:
                    if not str(stable.request.send_authority_ref).endswith(f"/{principal.key}"):
                        return ImplementationRequestResult(
                            status="DENIED", reason="send_authority_mismatch"
                        )
                    return self._success("REPLAYED", stable.request)
                if stable.reason != "request_not_found":
                    return ImplementationRequestResult(
                        status="UNKNOWN", reason="task_run_replay_unavailable"
                    )
                if current_request is not None:
                    return ImplementationRequestResult(
                        status="UNKNOWN", reason="implementation_request_unavailable"
                    )
                intent = ImplementationTaskRequest(
                    execution_work_id=package_work_id,
                    observed_revision=observed_revision,
                    authorization_ref=authorization_ref,
                    send_authority_ref=f"grant/{grant.id}/{grant.version}/{principal.key}",
                    objective=f"Implement approved package {package_work_id} at {observed_revision}.",
                    result_contract={
                        "implementation_request_id": str(review.consequence_id),
                        "authorization_ref": authorization_ref,
                        "return_to_work_id": str(package_work_id),
                        "implementation_scope": list(review.consequence.implementation_scope),
                        "implementation_target": review.consequence.implementation_target,
                        "milestones": [milestone_for[value] for value in continuation_effects],
                        "excluded_effects": list(review.consequence.excluded_effects),
                        **({} if hihr_binding is None else {
                            "hihr_trajectory_ref": (
                                f"{_HIHR_TRAJECTORY_PREFIX}{hihr_binding[0]}/{hihr_binding[1]}"
                            ),
                        }),
                        **({} if legacy_review is not None else {
                            "baseline_id": str(review.consequence_id),
                            "baseline_digest": review.consequence_digest,
                            "preimplementation_review_id": str(baseline_proof.review_id),
                            "preimplementation_verdict_digest": baseline_proof.verdict_digest,
                            "continuation_effects": list(continuation_effects),
                        }),
                    },
                )
                admitted = await self.tasks.request_in_transaction(
                    connection, package_work_id, operation_id, intent
                )
                if admitted.status != "ok" or admitted.request is None:
                    status = {"stale": "STALE", "denied": "DENIED", "conflict": "CONFLICT"}.get(
                        admitted.status, "UNKNOWN"
                    )
                    return ImplementationRequestResult(
                        status=cast(Literal["STALE", "DENIED", "UNKNOWN", "CONFLICT"], status),
                        reason=admitted.reason or "task_run_admission_failed",
                    )
                return self._success("APPLIED", admitted.request)
        except SQLAlchemyError, TypeError, ValueError, KeyError:
            return ImplementationRequestResult(status="UNKNOWN", reason="state_unavailable")
