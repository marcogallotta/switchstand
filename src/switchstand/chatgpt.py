"""Authenticated-caller seam; authentication adapters are trusted host code, never tools."""

import hashlib
from collections.abc import Awaitable, Callable
from typing import Literal, cast
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
    WorkAttachmentsRequest,
    WorkAttachmentsResult,
    WorkEventRequest,
    WorkEventResult,
    WorkGetRequest,
    WorkHistoryRequest,
    WorkHistoryResult,
    WorkResolveReferenceRequest,
    WorkSearchRequest,
    WorkSearchResult,
    WorkStructureRequest,
    WorkStructureResult,
)
from .core import Controller, Provider, ProviderError, State
from .creates import CreateGateway
from .discovery import DiscoveryProvider, WorkDiscovery
from .effects import AppendGateway
from .grant_state import GrantState
from .grants import (
    EffectReceipt,
    GrantedWorkResult,
    GrantResult,
    GuardOutcome,
    PrincipalContext,
    ProtectedAppend,
    ProtectedCreate,
    ProtectedUpdate,
    WorkGrant,
)
from .lifecycle import LifecycleEvent, ProfileState, RequiredResultPersistence
from .messages import (
    MessagePendingRequest,
    MessagePendingResult,
    MessageRoute,
    MessageSendRequest,
    MessageState,
    MessageSubmitRequest,
    MessageSubmitResult,
)
from .task_ref import parse_legacy_task_reference
from .updates import UpdateGateway

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
        grants: GrantState, providers: dict[str, Provider], messages: MessageState | None = None,
        required_results: RequiredResultPersistence | None = None,
    ):
        self.principal, self.state, self.grants, self.providers = principal, state, grants, providers
        self.gateway = AppendGateway(state, grants, providers)
        self.create_gateway = CreateGateway(state, grants, providers)
        self.update_gateway = UpdateGateway(state, grants, providers)
        self.messages = messages
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

    async def search(self, request: WorkSearchRequest) -> WorkSearchResult:
        principal = await self.principal()
        if principal is None:
            return WorkSearchResult(status="denied")
        try:
            async with self.grants.locked(principal.key) as grant:
                if not self.gateway.admitted(principal, grant) or grant is None:
                    return WorkSearchResult(status="denied")
                if grant.scope != "workspace" or "work_search" not in grant.operations:
                    return WorkSearchResult(status="denied")
                provider = self.providers.get("asana")
                if provider is None:
                    return WorkSearchResult(status="provider_error")
                return await WorkDiscovery(
                    "asana", cast(DiscoveryProvider, provider), self.state
                ).search(request)
        except (SQLAlchemyError, ProviderError, ValueError, KeyError):
            return WorkSearchResult(status="unknown")

    async def structure(self, request: WorkStructureRequest) -> WorkStructureResult:
        principal = await self.principal()
        if principal is None:
            return WorkStructureResult(status="denied")
        try:
            async with self.grants.locked(principal.key) as grant:
                if not self.gateway.admitted(principal, grant) or grant is None:
                    return WorkStructureResult(status="denied")
                if (grant.scope != "workspace"
                        or not {"work_get", "work_search"} <= grant.operations):
                    return WorkStructureResult(status="denied")
                handle = await self.state.get(request.work_id)
                if handle is None:
                    return WorkStructureResult(status="denied")
                provider = self.providers.get(handle.provider)
                if provider is None:
                    return WorkStructureResult(status="provider_error")
                result = await WorkDiscovery(
                    handle.provider, cast(DiscoveryProvider, provider), self.state
                ).structure(handle.provider_work_id, request.observed_revision)
                if result is None:
                    return WorkStructureResult(status="provider_error")
                if result.status == "stale":
                    return WorkStructureResult(
                        status="stale", work_id=request.work_id, revision=result.revision,
                    )
                return WorkStructureResult(
                    status="ok", work_id=request.work_id, revision=result.revision,
                    parent=result.parent, children=result.children,
                )
        except (SQLAlchemyError, ValueError, KeyError):
            return WorkStructureResult(status="unknown")

    async def resolve_reference(
        self, request: WorkResolveReferenceRequest,
    ) -> GrantedWorkResult:
        try:
            parsed = parse_legacy_task_reference(request.reference)
        except ValueError:
            return GrantedWorkResult(status="unknown")
        principal = await self.principal()
        if principal is None:
            return GrantedWorkResult(status="denied")
        try:
            async with self.grants.locked(principal.key) as grant:
                if not self.gateway.admitted(principal, grant) or grant is None:
                    return GrantedWorkResult(status="denied")
                if "work_get" not in grant.operations:
                    return GrantedWorkResult(status="denied")
                handle = await self.state.get_by_provider(
                    parsed.provider, parsed.provider_work_id
                )
                if grant.scope == "launch":
                    if handle is None:
                        return GrantedWorkResult(status="denied")
                    if (not grant.can_read(handle.id, explicit_target=True)
                            and not await self.grants.created_work_allowed(principal.key, handle.id)):
                        return GrantedWorkResult(status="denied")
                elif handle is None:
                    provider = self.providers.get(parsed.provider)
                    if provider is None:
                        return GrantedWorkResult(status="provider_error")
                    work = await provider.get(parsed.provider_work_id)
                    if work is None:
                        return GrantedWorkResult(status="unknown")
                    if not work.canonical:
                        return GrantedWorkResult(status="denied")
                    handle = await self.state.bind(parsed.provider, parsed.provider_work_id)
                authority = LaunchAuthority(active_work_id=handle.id)
                result = await Controller(authority, self.state, self.providers).get(
                    WorkGetRequest(api_version="1", work_id=handle.id)
                )
                return GrantedWorkResult(status=result.status, item=result.item)
        except ProviderError:
            return GrantedWorkResult(status="provider_error")
        except (SQLAlchemyError, ValueError, KeyError):
            return GrantedWorkResult(status="unknown")

    async def get(self, work_id: UUID | None = None, *, include_related: bool = False) -> GrantedWorkResult:
        principal = await self.principal()
        if principal is None:
            return GrantedWorkResult(status="denied", guard=self.denied("work_get"))
        try:
            async with self.grants.locked(principal.key) as grant:
                if not self.gateway.admitted(principal, grant) or grant is None:
                    return GrantedWorkResult(status="denied",
                                             guard=self.denied("work_get", "no_current_grant"))
                explicit_target = work_id is not None
                if grant.scope == "workspace" and not explicit_target:
                    return GrantedWorkResult(
                        status="denied",
                        guard=self.denied("work_get", "explicit_work_id_required"),
                    )
                target = work_id or grant.authority.active_work_id
                if "work_get" not in grant.operations:
                    return GrantedWorkResult(status="denied",
                                             guard=self.denied("work_get", "work_not_granted"))
                authority = grant.authority
                if grant.scope == "workspace":
                    if await self.state.get(target) is None:
                        return GrantedWorkResult(
                            status="denied",
                            guard=self.denied("work_get", "work_not_granted"),
                        )
                    authority = LaunchAuthority(active_work_id=target)
                elif not grant.can_read(target, explicit_target=explicit_target):
                    if not await self.grants.created_work_allowed(principal.key, target):
                        return GrantedWorkResult(status="denied",
                                                 guard=self.denied("work_get", "work_not_granted"))
                    authority = LaunchAuthority(active_work_id=target)
                result = await Controller(authority, self.state, self.providers).get(
                    WorkGetRequest(api_version="1", work_id=target, include_related=include_related)
                )
                return GrantedWorkResult(status=result.status, item=result.item,
                                         related=result.related)
        except (SQLAlchemyError, ProviderError, ValueError, KeyError):
            return GrantedWorkResult(status="unknown")

    async def history(self, request: WorkHistoryRequest) -> WorkHistoryResult:
        principal = await self.principal()
        if principal is None:
            return WorkHistoryResult(status="denied")
        try:
            async with self.grants.locked(principal.key) as grant:
                if not self.gateway.admitted(principal, grant) or grant is None:
                    return WorkHistoryResult(status="denied")
                if "work_get" not in grant.operations:
                    return WorkHistoryResult(status="denied")
                authority = grant.authority
                if grant.scope == "workspace":
                    if await self.state.get(request.work_id) is None:
                        return WorkHistoryResult(status="denied")
                    authority = LaunchAuthority(active_work_id=request.work_id)
                elif not grant.can_read(request.work_id, explicit_target=True):
                    if not await self.grants.created_work_allowed(principal.key, request.work_id):
                        return WorkHistoryResult(status="denied")
                    authority = LaunchAuthority(active_work_id=request.work_id)
                return await Controller(authority, self.state, self.providers).history(request)
        except (SQLAlchemyError, ProviderError, ValueError, KeyError):
            return WorkHistoryResult(status="unknown")

    async def attachments(self, request: WorkAttachmentsRequest) -> WorkAttachmentsResult:
        principal = await self.principal()
        if principal is None:
            return WorkAttachmentsResult(status="denied")
        try:
            async with self.grants.locked(principal.key) as grant:
                if not self.gateway.admitted(principal, grant) or grant is None:
                    return WorkAttachmentsResult(status="denied")
                if "work_get" not in grant.operations:
                    return WorkAttachmentsResult(status="denied")
                authority = grant.authority
                if grant.scope == "workspace":
                    if await self.state.get(request.work_id) is None:
                        return WorkAttachmentsResult(status="denied")
                    authority = LaunchAuthority(active_work_id=request.work_id)
                elif not grant.can_read(request.work_id, explicit_target=True):
                    if not await self.grants.created_work_allowed(principal.key, request.work_id):
                        return WorkAttachmentsResult(status="denied")
                    authority = LaunchAuthority(active_work_id=request.work_id)
                return await Controller(authority, self.state, self.providers).attachments(request)
        except (SQLAlchemyError, ProviderError, ValueError, KeyError):
            return WorkAttachmentsResult(status="unknown")

    async def event(self, request: WorkEventRequest) -> WorkEventResult:
        principal = await self.principal()
        if principal is None:
            return WorkEventResult(status="denied")
        try:
            async with self.grants.locked(principal.key) as grant:
                if not self.gateway.admitted(principal, grant) or grant is None:
                    return WorkEventResult(status="denied")
                if "work_get" not in grant.operations:
                    return WorkEventResult(status="denied")
                authority = grant.authority
                if grant.scope == "workspace":
                    if await self.state.get(request.work_id) is None:
                        return WorkEventResult(status="denied")
                    authority = LaunchAuthority(active_work_id=request.work_id)
                elif not grant.can_read(request.work_id, explicit_target=True):
                    if not await self.grants.created_work_allowed(principal.key, request.work_id):
                        return WorkEventResult(status="denied")
                    authority = LaunchAuthority(active_work_id=request.work_id)
                return await Controller(authority, self.state, self.providers).event(request)
        except (SQLAlchemyError, ProviderError, ValueError, KeyError):
            return WorkEventResult(status="unknown")

    async def append(self, request: ProtectedAppend) -> GuardOutcome:
        principal = await self.principal()
        if principal is None:
            return self.gateway.guard(request, "denied", "authenticated_principal_required")
        return await self.gateway.append(principal, request)

    async def create(self, request: ProtectedCreate) -> GuardOutcome:
        principal = await self.principal()
        if principal is None:
            return self.create_gateway.guard(request, "denied", "authenticated_principal_required")
        return await self.create_gateway.create(principal, request)

    async def update(self, request: ProtectedUpdate) -> GuardOutcome:
        principal = await self.principal()
        if principal is None:
            return self.update_gateway.guard(request, "denied", "authenticated_principal_required")
        return await self.update_gateway.update(principal, request)

    @staticmethod
    def _result_guard(
        request: RequiredResultSaveRequest,
        status: Literal["denied", "stale", "unknown"],
        reason: str,
        operation_id: UUID | None = None,
        possible_send: bool = False,
    ) -> GuardOutcome:
        return GuardOutcome(
            status=status, operation="required_result_save", work_id=request.work_id,
            operation_id=operation_id, reason=reason,
            effect="unknown" if possible_send else "not_sent",
            retry="reconcile" if possible_send else "refresh" if status == "stale" else "none",
            next_action=("Reconcile the recorded effect; do not send a new operation."
                         if possible_send else "Refresh work/grant or ask the trusted issuer."),
        )

    async def required_result_save(self, request: RequiredResultSaveRequest) -> GuardOutcome:
        """Save one server-identified result through Lifecycle and the existing effect journal."""
        principal = await self.principal()
        if principal is None:
            return self._result_guard(request, "denied", "authenticated_principal_required")
        if self.required_results is None:
            return self._result_guard(request, "denied", "required_result_persistence_unavailable")

        operation_id: UUID | None = None
        possible_send = False
        try:
            grant = await self.grants.current(principal.key)
            if not self.gateway.admitted(principal, grant) or grant is None:
                return self._result_guard(request, "denied", "no_current_grant")
            if request.grant_version != grant.version:
                return self._result_guard(request, "stale", "grant_version_changed")
            if not grant.can_write(request.work_id) or "work_append" not in grant.operations:
                return self._result_guard(request, "denied", "operation_or_work_not_granted")
            handle = await self.state.get(request.work_id)
            if handle is None:
                return self._result_guard(request, "denied", "work_not_bound")

            currentness = hashlib.sha256(
                f"{principal.key}:{grant.id}:{grant.version}".encode()
            ).hexdigest()
            destination = f"{handle.provider}:task:{handle.provider_work_id}"
            correlation = hashlib.sha256(request.text.encode()).hexdigest()
            operation_id = uuid5(
                REQUIRED_RESULT_NAMESPACE, f"{principal.key}:{request.work_id}:{destination}"
            )
            repository = self.required_results.repository
            obligation = await repository.get(operation_id)
            if obligation is None:
                try:
                    obligation = await repository.create(operation_id, request.work_id, currentness)
                except SQLAlchemyError:
                    obligation = await repository.get(operation_id)
                    if obligation is None:
                        raise
            if obligation.work_id_ref != request.work_id:
                return self._result_guard(
                    request, "denied", "lifecycle_obligation_work_conflict", operation_id
                )
            if obligation.state is ProfileState.PENDING_RESULT:
                identity = (None, None)
            else:
                identity = (obligation.destination_ref, obligation.result_correlation)
            if identity not in {(None, None), (destination, correlation)}:
                return self._result_guard(
                    request, "denied", "lifecycle_result_identity_conflict", operation_id
                )
            if obligation.currentness_token != currentness:
                async with self.grants.locked(principal.key, request.work_id) as locked_grant:
                    if (locked_grant is None or not self.gateway.admitted(principal, locked_grant)
                            or locked_grant.id != grant.id
                            or locked_grant.version != grant.version
                            or not locked_grant.can_write(request.work_id)
                            or "work_append" not in locked_grant.operations):
                        return self._result_guard(
                            request, "stale", "lifecycle_currentness_changed", operation_id
                        )
                    effect = await self.grants.exact(operation_id)
                    if effect is None:
                        obligation = await repository.adopt_currentness(obligation, currentness)
                    elif (
                        effect.principal_key == principal.key
                        and effect.outcome.effect == "applied"
                        and isinstance(effect.outcome.receipt, EffectReceipt)
                        and effect.outcome.receipt.operation_id == operation_id
                        and effect.outcome.receipt.work_id == request.work_id
                        and effect.outcome.receipt.text == request.text
                        and effect.outcome.receipt.provider == handle.provider
                        and effect.outcome.receipt.task_gid == handle.provider_work_id
                    ):
                        if obligation.state is ProfileState.TERMINAL:
                            return GuardOutcome(
                                status="ok", operation="required_result_save",
                                work_id=request.work_id, operation_id=operation_id,
                                reason="required_result_already_persisted",
                                next_action="Use the durable Lifecycle terminal evidence.",
                            )
                        await self.required_results.transition(
                            operation_id, obligation.currentness_token,
                            LifecycleEvent.PERSIST_READBACK_MATCHED,
                            evidence={
                                "destination_ref": destination,
                                "result_correlation": correlation,
                                "operation_id": str(operation_id),
                                "provider": effect.outcome.receipt.provider,
                                "task_gid": effect.outcome.receipt.task_gid,
                                "story_gid": effect.outcome.receipt.story_gid,
                            },
                        )
                        return effect.outcome.model_copy(
                            update={"operation": "required_result_save"}
                        )
                    else:
                        return self._result_guard(
                            request, "stale", "lifecycle_currentness_changed", operation_id
                        )
            if obligation.state is ProfileState.PENDING_RESULT:
                try:
                    obligation = await self.required_results.transition(
                        operation_id, currentness, LifecycleEvent.RESULT_READY,
                        destination_ref=destination, result_correlation=correlation,
                    )
                except ValueError:
                    obligation = await repository.get(operation_id)
                    if obligation is None:
                        raise
            if (obligation.destination_ref, obligation.result_correlation) != (destination, correlation):
                return self._result_guard(
                    request, "denied", "lifecycle_result_identity_conflict", operation_id
                )
            if obligation.state is ProfileState.TERMINAL:
                return GuardOutcome(
                    status="ok", operation="required_result_save", work_id=request.work_id,
                    operation_id=operation_id, reason="required_result_already_persisted",
                    next_action="Use the durable Lifecycle terminal evidence.",
                )
            if (obligation.state is ProfileState.UNKNOWN
                    and obligation.unknown_reason != LifecycleEvent.PERSIST_OUTCOME_AMBIGUOUS.value):
                return self._result_guard(
                    request, "unknown", "lifecycle_currentness_unresolved", operation_id
                )

            outcome = await self.gateway.append(principal, ProtectedAppend(
                api_version=request.api_version, operation_id=operation_id,
                work_id=request.work_id, grant_version=request.grant_version,
                observed_revision=request.observed_revision, text=request.text,
            ))
            possible_send = outcome.effect != "not_sent"
            if outcome.effect == "applied" and isinstance(outcome.receipt, EffectReceipt):
                evidence = {
                    "destination_ref": destination, "result_correlation": correlation,
                    "operation_id": str(operation_id), "provider": outcome.receipt.provider,
                    "task_gid": outcome.receipt.task_gid, "story_gid": outcome.receipt.story_gid,
                }
                try:
                    async with self.grants.locked(principal.key, request.work_id) as current:
                        if (current is None or not self.gateway.admitted(principal, current)
                                or current.id != grant.id or current.version != grant.version
                                or not current.can_write(request.work_id)
                                or "work_append" not in current.operations):
                            return self._result_guard(
                                request, "stale", "lifecycle_currentness_changed_before_terminal",
                                operation_id, True,
                            )
                        await self.required_results.transition(
                            operation_id, currentness, LifecycleEvent.PERSIST_READBACK_MATCHED,
                            evidence=evidence,
                        )
                except (SQLAlchemyError, ValueError):
                    confirmed = await repository.get(operation_id)
                    if confirmed is None or confirmed.state is not ProfileState.TERMINAL:
                        return self._result_guard(
                            request, "unknown", "lifecycle_terminal_commit_unconfirmed",
                            operation_id, True,
                        )
                return outcome.model_copy(update={"operation": "required_result_save"})
            if outcome.effect == "unknown" and obligation.state is ProfileState.PERSIST_REQUIRED:
                try:
                    await self.required_results.transition(
                        operation_id, currentness, LifecycleEvent.PERSIST_OUTCOME_AMBIGUOUS,
                        evidence={"operation_id": str(operation_id), "reason": outcome.reason},
                    )
                except (SQLAlchemyError, ValueError):
                    pass
            return outcome.model_copy(update={"operation": "required_result_save"})
        except (SQLAlchemyError, ProviderError, ValueError, KeyError):
            return self._result_guard(
                request, "unknown", "lifecycle_or_effect_state_unavailable",
                operation_id, possible_send,
            )

    async def message_send(self, request: MessageSendRequest) -> MessageSubmitResult:
        principal = await self.principal()
        if principal is None:
            return MessageSubmitResult(status="denied", reason="actor_not_admitted")
        if self.messages is None:
            return MessageSubmitResult(status="recovery_required", reason="state_unavailable")
        try:
            context = None
            recipient_work_id = request.recipient_work_id
            if request.in_reply_to_delivery_id is not None:
                context = await self.messages.reply_context(request.in_reply_to_delivery_id)
                recipient_work_id = None if context is None else context.recipient_work_id
            async with self.grants.locked_message_route(
                principal.key, request.work_id, recipient_work_id,
            ) as (grant, recipient):
                failure = await self._message_actor(
                    principal, grant, request.work_id, request.grant_version
                )
                if failure is not None:
                    return MessageSubmitResult(status=failure[0], reason=failure[1])
                replay = await self.messages.committed_public_replay(request.work_id, request)
                if replay is not None:
                    return replay
                if context is None and request.in_reply_to_delivery_id is not None:
                    return MessageSubmitResult(
                        status="conflict", reason="reply_delivery_not_found"
                    )
                assert recipient_work_id is not None
                if recipient is None:
                    return MessageSubmitResult(
                        status="denied", reason="recipient_route_unavailable"
                    )
                handle = await self.state.get(recipient_work_id)
                if handle is None or handle.provider != "asana":
                    return MessageSubmitResult(
                        status="denied", reason="recipient_route_unavailable"
                    )
                return await self.messages.submit_admitted(
                        request.work_id,
                        MessageRoute(
                            recipient_work_id=recipient_work_id,
                            recipient_grant_version=recipient.version,
                            projection_provider="asana",
                            projection_target=handle.provider_work_id,
                        ),
                        MessageSubmitRequest(
                            api_version=request.api_version,
                            message_id=request.message_id,
                            grant_version=request.grant_version,
                            route_ref=(cast(str, request.route_ref) if context is None
                                       else context.route_ref),
                            kind="request" if context is None else "result",
                            payload=request.payload,
                            in_reply_to_delivery_id=request.in_reply_to_delivery_id,
                        ),
                    )
        except (SQLAlchemyError, ProviderError, ValueError, KeyError):
            return MessageSubmitResult(status="recovery_required", reason="state_unavailable")

    async def message_pending(
        self, work_id: UUID, request: MessagePendingRequest,
    ) -> MessagePendingResult:
        principal = await self.principal()
        if principal is None:
            return MessagePendingResult(status="denied", reason="actor_not_admitted")
        if self.messages is None:
            return MessagePendingResult(status="recovery_required", reason="state_unavailable")
        try:
            async with self.grants.locked(principal.key, work_id) as grant:
                failure = await self._message_actor(principal, grant, work_id, request.grant_version)
                if failure is not None:
                    return MessagePendingResult(status=failure[0], reason=failure[1])
                return await self.messages.pending_admitted(work_id, request)
        except (SQLAlchemyError, ProviderError, ValueError, KeyError):
            return MessagePendingResult(status="recovery_required", reason="state_unavailable")

    async def _message_actor(
        self, principal: PrincipalContext, grant: WorkGrant | None,
        work_id: UUID, grant_version: int,
    ) -> tuple[Literal["denied", "stale"], Literal[
        "actor_not_admitted", "grant_version_changed", "message_not_granted"
    ]] | None:
        if not self.gateway.admitted(principal, grant) or grant is None:
            return "denied", "actor_not_admitted"
        if grant.version != grant_version:
            return "stale", "grant_version_changed"
        if "message" not in grant.operations:
            return "denied", "message_not_granted"
        if grant.scope == "launch" and grant.authority.active_work_id != work_id:
            return "denied", "actor_not_admitted"
        if await self.state.get(work_id) is None:
            return "denied", "actor_not_admitted"
        return None

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
