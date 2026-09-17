"""Authenticated-caller seam; authentication adapters are trusted host code, never tools."""

import hashlib
from collections.abc import Awaitable, Callable
from typing import Literal
from uuid import UUID, uuid5

from pydantic import Field
from sqlalchemy.exc import SQLAlchemyError

from .contracts import (
    ClosedModel,
    LaunchAuthority,
    SourceStoriesRequest,
    SourceStoriesResult,
    SourceStoryRequest,
    SourceStoryResult,
    SourceTaskRequest,
    SourceTaskResult,
    WorkGetRequest,
)
from .core import Controller, Provider, ProviderError, State
from .effects import AppendGateway
from .grant_state import GrantState
from .grants import GrantedWorkResult, GrantResult, GuardOutcome, PrincipalContext, ProtectedAppend
from .lifecycle import LifecycleEvent, ProfileState, RequiredResultPersistence

PrincipalResolver = Callable[[], Awaitable[PrincipalContext | None]]
REQUIRED_RESULT_NAMESPACE = UUID("12ddf4c9-f608-46b6-9150-3be7841e85da")


class RequiredResultSaveRequest(ClosedModel):
    api_version: Literal["1"]
    work_id: UUID
    grant_version: int = Field(ge=1)
    observed_revision: str = Field(min_length=1)
    text: str = Field(min_length=1, max_length=8000)


class ChatGPTService:
    def __init__(
        self, principal: PrincipalResolver, state: State,
        grants: GrantState, providers: dict[str, Provider],
        required_results: RequiredResultPersistence | None = None,
    ):
        self.principal, self.state, self.grants, self.providers = principal, state, grants, providers
        self.gateway = AppendGateway(state, grants, providers)
        self.required_results = required_results
        # Only exact source methods use this controller; its dummy authority is
        # never consulted for work reads or writes on the ChatGPT surface.
        self.sources = Controller(LaunchAuthority(active_work_id=UUID(int=0)), state, providers)

    @staticmethod
    def denied(operation: str, reason: str = "authenticated_principal_required") -> GuardOutcome:
        return GuardOutcome(status="denied", operation=operation, reason=reason,
                            next_action="Use the authenticated connection and trusted work issuer.")

    @staticmethod
    def _required_guard(
        request: RequiredResultSaveRequest,
        status: Literal["denied", "stale", "unknown"],
        reason: str,
        *,
        operation_id: UUID | None = None,
        possible_send: bool = False,
    ) -> GuardOutcome:
        return GuardOutcome(
            status=status,
            operation="required_result_save",
            work_id=request.work_id,
            operation_id=operation_id,
            reason=reason,
            effect="unknown" if possible_send else "not_sent",
            retry="reconcile" if possible_send else "refresh" if status == "stale" else "none",
            next_action=(
                "Reconcile the recorded effect; do not send a new operation."
                if possible_send else "Refresh work/grant or ask the trusted issuer."
            ),
        )

    @staticmethod
    def _required_outcome(outcome: GuardOutcome) -> GuardOutcome:
        return outcome.model_copy(update={"operation": "required_result_save"})

    async def grant_get(self) -> GrantResult:
        principal = await self.principal()
        if principal is None:
            return GrantResult(status="denied", guard=self.denied("grant_get"))
        try:
            grant = await self.grants.current(principal.key)
            if not self.gateway.admitted(principal, grant):
                return GrantResult(status="denied", principal=principal,
                                   guard=self.denied("grant_get", "no_current_grant"))
            return GrantResult(status="ok", principal=principal, grant=grant)
        except (SQLAlchemyError, ProviderError, ValueError, KeyError):
            return GrantResult(status="unknown", principal=principal)

    async def get(self, work_id: UUID | None = None, *, include_related: bool = False) -> GrantedWorkResult:
        principal = await self.principal()
        if principal is None:
            return GrantedWorkResult(status="denied", guard=self.denied("work_get"))
        try:
            async with self.grants.locked(principal.key) as grant:
                if not self.gateway.admitted(principal, grant) or grant is None:
                    return GrantedWorkResult(status="denied",
                                             guard=self.denied("work_get", "no_current_grant"))
                target = work_id or grant.authority.active_work_id
                if "work_get" not in grant.operations or not grant.authority.can_read(target):
                    return GrantedWorkResult(status="denied",
                                             guard=self.denied("work_get", "work_not_granted"))
                result = await Controller(grant.authority, self.state, self.providers).get(
                    WorkGetRequest(api_version="1", work_id=target, include_related=include_related)
                )
                return GrantedWorkResult(status=result.status, item=result.item,
                                         related=result.related)
        except (SQLAlchemyError, ProviderError, ValueError, KeyError):
            return GrantedWorkResult(status="unknown")

    async def append(self, request: ProtectedAppend) -> GuardOutcome:
        principal = await self.principal()
        if principal is None:
            return self.gateway.guard(request, "denied", "authenticated_principal_required")
        return await self.gateway.append(principal, request)

    async def required_result_save(self, request: RequiredResultSaveRequest) -> GuardOutcome:
        """Persist one required result through the existing protected append/readback path.

        The service derives one stable obligation/effect identity from trusted caller/work
        context. Callers cannot mint a second OperationId for the same required-result duty.
        """
        principal = await self.principal()
        if principal is None:
            return self._required_guard(request, "denied", "authenticated_principal_required")
        if self.required_results is None:
            return self._required_guard(request, "denied", "required_result_persistence_unavailable")

        possible_send = False
        operation_id: UUID | None = None
        try:
            grant = await self.grants.current(principal.key)
            if not self.gateway.admitted(principal, grant) or grant is None:
                return self._required_guard(request, "denied", "no_current_grant")
            if request.work_id != grant.authority.active_work_id or "work_append" not in grant.operations:
                return self._required_guard(request, "denied", "operation_or_work_not_granted")
            if request.grant_version != grant.version:
                return self._required_guard(request, "stale", "grant_version_changed")

            handle = await self.state.get(request.work_id)
            if handle is None:
                return self._required_guard(request, "denied", "work_not_bound")

            currentness_token = hashlib.sha256(
                f"{principal.key}:{grant.id}:{grant.version}".encode()
            ).hexdigest()
            destination_ref = f"{handle.provider}:task:{handle.provider_work_id}"
            result_correlation = hashlib.sha256(request.text.encode()).hexdigest()
            operation_id = uuid5(
                REQUIRED_RESULT_NAMESPACE,
                f"{principal.key}:{request.work_id}:{destination_ref}",
            )
            effect = ProtectedAppend(
                api_version=request.api_version,
                operation_id=operation_id,
                work_id=request.work_id,
                grant_version=request.grant_version,
                observed_revision=request.observed_revision,
                text=request.text,
            )
            repository = self.required_results.repository
            obligation = await repository.get(operation_id)
            if obligation is None:
                try:
                    obligation = await repository.create(
                        operation_id,
                        request.work_id,
                        currentness_token,
                    )
                except SQLAlchemyError:
                    # Concurrent admission may have committed first. Only the exact
                    # deterministic identity/binding is reusable.
                    obligation = await repository.get(operation_id)
                    if obligation is None:
                        raise

            if obligation.work_id_ref != request.work_id:
                return self._required_guard(
                    request, "denied", "lifecycle_obligation_work_conflict",
                    operation_id=operation_id,
                )
            if obligation.currentness_token != currentness_token:
                return self._required_guard(
                    request, "stale", "lifecycle_currentness_changed",
                    operation_id=operation_id,
                )

            if obligation.state is ProfileState.PENDING_RESULT:
                try:
                    obligation = await self.required_results.transition(
                        operation_id,
                        currentness_token,
                        LifecycleEvent.RESULT_READY,
                        destination_ref=destination_ref,
                        result_correlation=result_correlation,
                    )
                except ValueError:
                    obligation = await repository.get(operation_id)
                    if obligation is None:
                        raise
            if (
                obligation.destination_ref != destination_ref
                or obligation.result_correlation != result_correlation
            ):
                return self._required_guard(
                    request, "denied", "lifecycle_result_identity_conflict",
                    operation_id=operation_id,
                )

            if obligation.state is ProfileState.TERMINAL:
                return GuardOutcome(
                    status="ok",
                    operation="required_result_save",
                    work_id=request.work_id,
                    operation_id=operation_id,
                    reason="required_result_already_persisted",
                    effect="not_sent",
                    retry="none",
                    next_action="Use the durable Lifecycle terminal evidence.",
                )
            if (
                obligation.state is ProfileState.UNKNOWN
                and obligation.unknown_reason != LifecycleEvent.PERSIST_OUTCOME_AMBIGUOUS.value
            ):
                return self._required_guard(
                    request, "unknown", "lifecycle_currentness_unresolved",
                    operation_id=operation_id,
                )

            outcome = await self.gateway.append(principal, effect)
            possible_send = outcome.effect != "not_sent"
            if outcome.effect == "applied" and outcome.receipt is not None:
                evidence = {
                    "destination_ref": destination_ref,
                    "result_correlation": result_correlation,
                    "operation_id": str(operation_id),
                    "provider": outcome.receipt.provider,
                    "task_gid": outcome.receipt.task_gid,
                    "story_gid": outcome.receipt.story_gid,
                }
                try:
                    # Re-establish governing currentness after the effect gateway has
                    # released its locks, then hold that grant/work fence until the
                    # Lifecycle terminal transition has durably committed.
                    async with self.grants.locked(principal.key, request.work_id) as terminal_grant:
                        if terminal_grant is None or not self.gateway.admitted(
                            principal, terminal_grant
                        ):
                            return self._required_guard(
                                request,
                                "stale",
                                "lifecycle_currentness_changed_before_terminal",
                                operation_id=operation_id,
                                possible_send=True,
                            )
                        if (
                            terminal_grant.id != grant.id
                            or terminal_grant.version != grant.version
                            or terminal_grant.authority.active_work_id != request.work_id
                            or "work_append" not in terminal_grant.operations
                        ):
                            return self._required_guard(
                                request,
                                "stale",
                                "lifecycle_currentness_changed_before_terminal",
                                operation_id=operation_id,
                                possible_send=True,
                            )
                        await self.required_results.transition(
                            operation_id,
                            currentness_token,
                            LifecycleEvent.PERSIST_READBACK_MATCHED,
                            evidence=evidence,
                        )
                except (SQLAlchemyError, ValueError):
                    # A concurrent caller may have committed the same terminal state.
                    confirmed = await repository.get(operation_id)
                    if (
                        confirmed is not None
                        and confirmed.state is ProfileState.TERMINAL
                        and confirmed.destination_ref == destination_ref
                        and confirmed.result_correlation == result_correlation
                    ):
                        return self._required_outcome(outcome)
                    return self._required_guard(
                        request,
                        "unknown",
                        "lifecycle_terminal_commit_unconfirmed",
                        operation_id=operation_id,
                        possible_send=True,
                    )
                return self._required_outcome(outcome)

            if outcome.effect == "unknown" and obligation.state is ProfileState.PERSIST_REQUIRED:
                try:
                    await self.required_results.transition(
                        operation_id,
                        currentness_token,
                        LifecycleEvent.PERSIST_OUTCOME_AMBIGUOUS,
                        evidence={"operation_id": str(operation_id), "reason": outcome.reason},
                    )
                except (SQLAlchemyError, ValueError):
                    pass
            return self._required_outcome(outcome)
        except (SQLAlchemyError, ProviderError, ValueError, KeyError):
            return self._required_guard(
                request,
                "unknown",
                "lifecycle_or_effect_state_unavailable",
                operation_id=operation_id,
                possible_send=possible_send,
            )

    async def source_task(self, request: SourceTaskRequest) -> SourceTaskResult:
        if await self.principal() is None:
            return SourceTaskResult(status="denied")
        return await self.sources.source_task(request)

    async def source_stories(self, request: SourceStoriesRequest) -> SourceStoriesResult:
        if await self.principal() is None:
            return SourceStoriesResult(status="denied")
        return await self.sources.source_stories(request)

    async def source_story(self, request: SourceStoryRequest) -> SourceStoryResult:
        if await self.principal() is None:
            return SourceStoryResult(status="denied")
        return await self.sources.source_story(request)
