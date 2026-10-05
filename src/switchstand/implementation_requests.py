"""Default-off authorization facade for approved implementation task requests."""

from __future__ import annotations

from typing import Literal, cast
from uuid import UUID

from pydantic import model_validator
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine

from .canonical_work import CanonicalWorkRepository, canonical_revision
from .contracts import ClosedModel
from .grant_state import work_grants
from .grants import PrincipalContext, WorkGrant
from .human_reviews import human_review_consequences, human_review_record
from .reviews import ReviewOccurrenceState
from .task_runs import ImplementationTaskRequest, TaskRunRequest, TaskRunState


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
    ) -> ImplementationRequestResult:
        try:
            async with self.engine.begin() as connection:
                replay = await self.tasks.get_operation_in_transaction(connection, operation_id)
                if replay.request is not None:
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
                if replay.reason != "request_not_found":
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
                if not reviews:
                    return ImplementationRequestResult(
                        status="DENIED", reason="human_review_not_approved"
                    )
                if len(reviews) != 1:
                    return ImplementationRequestResult(
                        status="UNKNOWN", reason="human_review_ambiguous"
                    )
                review = human_review_record(reviews[0])
                if review.decision != "APPROVED" or review.state != "READY_FOR_IMPLEMENTATION":
                    return ImplementationRequestResult(
                        status="DENIED", reason="human_review_not_approved"
                    )

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

                pass_status = await self.review_occurrences.pass_status_in_transaction(
                    connection, package_work_id, observed_revision
                )
                if pass_status == "UNKNOWN":
                    return ImplementationRequestResult(
                        status="UNKNOWN", reason="independent_review_unavailable"
                    )
                if pass_status != "PASS":
                    return ImplementationRequestResult(
                        status="DENIED", reason="independent_review_not_passed"
                    )
                authorization_ref = (
                    f"human-review/{review.consequence_id}/{review.consequence_digest}"
                )
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
                intent = ImplementationTaskRequest(
                    execution_work_id=package_work_id,
                    observed_revision=observed_revision,
                    authorization_ref=authorization_ref,
                    send_authority_ref=f"grant/{grant.id}/{grant.version}/{principal.key}",
                    objective=f"Implement approved package {package_work_id} at {observed_revision}.",
                    result_contract={
                        "implementation_request_id": str(review.consequence_id),
                        "authorization_ref": str(review.consequence_id),
                        "return_to_work_id": str(package_work_id),
                        "implementation_scope": list(review.consequence.implementation_scope),
                        "implementation_target": review.consequence.implementation_target,
                        "milestones": ["IMPLEMENTED", "MERGED", "ACTIVATED"],
                        "excluded_effects": list(review.consequence.excluded_effects),
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
