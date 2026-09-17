"""Authenticated-caller seam; authentication adapters are trusted host code, never tools."""

import hashlib
from collections.abc import Awaitable, Callable
from uuid import UUID

from sqlalchemy.exc import SQLAlchemyError

from .contracts import (
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
from .lifecycle import (
    LifecycleEvent,
    ProfileState,
    RequiredResultPersistence,
)

PrincipalResolver = Callable[[], Awaitable[PrincipalContext | None]]


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

    async def required_result_save(
        self,
        obligation_id: UUID,
        request: ProtectedAppend,
    ) -> GuardOutcome:
        """Persist one required result through the existing protected append/readback path.

        The caller supplies a stable obligation id and OperationId. Lifecycle owns only
        the durable obligation; WorkGrant and AppendGateway remain the authority/effect
        truth. Retrying the same logical result after a service restart is therefore
        safe without copying the result payload into Lifecycle storage.
        """
        principal = await self.principal()
        if principal is None:
            return self.gateway.guard(request, "denied", "authenticated_principal_required")
        if self.required_results is None:
            return self.gateway.guard(request, "denied", "required_result_persistence_unavailable")

        possible_send = False
        try:
            grant = await self.grants.current(principal.key)
            if not self.gateway.admitted(principal, grant) or grant is None:
                return self.gateway.guard(request, "denied", "no_current_grant")
            if request.work_id != grant.authority.active_work_id or "work_append" not in grant.operations:
                return self.gateway.guard(request, "denied", "operation_or_work_not_granted")
            if request.grant_version != grant.version:
                return self.gateway.guard(request, "stale", "grant_version_changed")

            handle = await self.state.get(request.work_id)
            if handle is None:
                return self.gateway.guard(request, "denied", "work_not_bound")

            currentness_token = hashlib.sha256(
                f"{principal.key}:{grant.id}:{grant.version}".encode()
            ).hexdigest()
            destination_ref = f"{handle.provider}:task:{handle.provider_work_id}"
            result_correlation = hashlib.sha256(request.text.encode()).hexdigest()
            repository = self.required_results.repository
            obligation = await repository.get(obligation_id)
            if obligation is None:
                try:
                    obligation = await repository.create(
                        obligation_id,
                        request.work_id,
                        currentness_token,
                    )
                except SQLAlchemyError:
                    # Concurrent/retried admission may have committed first. Only an
                    # exact durable binding is reusable; every other DB error stays unknown.
                    obligation = await repository.get(obligation_id)
                    if obligation is None:
                        raise

            if obligation.work_id_ref != request.work_id:
                return self.gateway.guard(request, "denied", "lifecycle_obligation_work_conflict")
            if obligation.currentness_token != currentness_token:
                return self.gateway.guard(request, "stale", "lifecycle_currentness_changed")

            if obligation.state is ProfileState.PENDING_RESULT:
                obligation = await self.required_results.transition(
                    obligation_id,
                    currentness_token,
                    LifecycleEvent.RESULT_READY,
                    destination_ref=destination_ref,
                    result_correlation=result_correlation,
                )
            elif (
                obligation.destination_ref != destination_ref
                or obligation.result_correlation != result_correlation
            ):
                return self.gateway.guard(request, "denied", "lifecycle_result_identity_conflict")

            if obligation.state is ProfileState.TERMINAL:
                return GuardOutcome(
                    status="ok",
                    operation="required_result_save",
                    work_id=request.work_id,
                    operation_id=request.operation_id,
                    reason="required_result_already_persisted",
                    effect="not_sent",
                    retry="none",
                    next_action="Use the durable Lifecycle terminal evidence.",
                )
            if (
                obligation.state is ProfileState.UNKNOWN
                and obligation.unknown_reason != LifecycleEvent.PERSIST_OUTCOME_AMBIGUOUS.value
            ):
                return self.gateway.guard(request, "unknown", "lifecycle_currentness_unresolved")

            outcome = await self.gateway.append(principal, request)
            possible_send = outcome.effect != "not_sent"
            if outcome.effect == "applied" and outcome.receipt is not None:
                evidence = {
                    "destination_ref": destination_ref,
                    "result_correlation": result_correlation,
                    "operation_id": str(request.operation_id),
                    "provider": outcome.receipt.provider,
                    "task_gid": outcome.receipt.task_gid,
                    "story_gid": outcome.receipt.story_gid,
                }
                try:
                    await self.required_results.transition(
                        obligation_id,
                        currentness_token,
                        LifecycleEvent.PERSIST_READBACK_MATCHED,
                        evidence=evidence,
                    )
                except (SQLAlchemyError, ValueError):
                    # The provider effect is durably owned by AppendGateway. If the
                    # Lifecycle terminal commit failed, do not report completion;
                    # the same OperationId can be replayed without duplicating the write.
                    return self.gateway.guard(
                        request,
                        "unknown",
                        "lifecycle_terminal_commit_unconfirmed",
                        possible_send=True,
                    )
                return outcome

            if outcome.effect == "unknown" and obligation.state is ProfileState.PERSIST_REQUIRED:
                try:
                    await self.required_results.transition(
                        obligation_id,
                        currentness_token,
                        LifecycleEvent.PERSIST_OUTCOME_AMBIGUOUS,
                        evidence={
                            "operation_id": str(request.operation_id),
                            "reason": outcome.reason,
                        },
                    )
                except (SQLAlchemyError, ValueError):
                    pass
            return outcome
        except (SQLAlchemyError, ProviderError, ValueError, KeyError):
            return self.gateway.guard(
                request,
                "unknown",
                "lifecycle_or_effect_state_unavailable",
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
