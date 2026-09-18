"""Authenticated-caller seam; authentication adapters are trusted host code, never tools."""

from collections.abc import Awaitable, Callable
from typing import cast
from uuid import UUID

from sqlalchemy.exc import SQLAlchemyError

from .attachments import AttachmentProvider, AttachmentState, WorkAttachments
from .contracts import (
    LaunchAuthority,
    SourceStoriesRequest,
    SourceStoriesResult,
    SourceStoryRequest,
    SourceStoryResult,
    SourceTaskRequest,
    SourceTaskResult,
    WorkAttachmentRequest,
    WorkAttachmentResult,
    WorkAttachmentsRequest,
    WorkAttachmentsResult,
    WorkEventRequest,
    WorkEventResult,
    WorkGetRequest,
    WorkHistoryRequest,
    WorkHistoryResult,
)
from .core import Controller, Provider, ProviderError, State
from .creates import CreateGateway
from .effects import AppendGateway
from .grant_state import GrantState
from .grants import (
    GrantedWorkResult,
    GrantResult,
    GuardOutcome,
    PrincipalContext,
    ProtectedAppend,
    ProtectedCreate,
)

PrincipalResolver = Callable[[], Awaitable[PrincipalContext | None]]


class ChatGPTService:
    def __init__(
        self, principal: PrincipalResolver, state: State,
        grants: GrantState, providers: dict[str, Provider],
    ):
        self.principal, self.state, self.grants, self.providers = principal, state, grants, providers
        self.gateway = AppendGateway(state, grants, providers)
        self.create_gateway = CreateGateway(state, grants, providers)
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

    async def attachments(self, request: WorkAttachmentsRequest) -> WorkAttachmentsResult:
        principal = await self.principal()
        if principal is None:
            return WorkAttachmentsResult(status="denied")
        try:
            async with self.grants.locked(principal.key) as grant:
                if not self.gateway.admitted(principal, grant) or grant is None:
                    return WorkAttachmentsResult(status="denied")
                if "work_attachments" not in grant.operations:
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
                return await WorkAttachments(
                    authority,
                    cast(AttachmentState, self.state),
                    cast(dict[str, AttachmentProvider], self.providers),
                ).list(request)
        except (SQLAlchemyError, ProviderError, ValueError, KeyError):
            return WorkAttachmentsResult(status="unknown")

    async def attachment(self, request: WorkAttachmentRequest) -> WorkAttachmentResult:
        principal = await self.principal()
        if principal is None:
            return WorkAttachmentResult(status="denied")
        try:
            async with self.grants.locked(principal.key) as grant:
                if not self.gateway.admitted(principal, grant) or grant is None:
                    return WorkAttachmentResult(status="denied")
                if "work_attachments" not in grant.operations:
                    return WorkAttachmentResult(status="denied")
                authority = grant.authority
                if grant.scope == "workspace":
                    if await self.state.get(request.work_id) is None:
                        return WorkAttachmentResult(status="denied")
                    authority = LaunchAuthority(active_work_id=request.work_id)
                elif not grant.can_read(request.work_id, explicit_target=True):
                    if not await self.grants.created_work_allowed(principal.key, request.work_id):
                        return WorkAttachmentResult(status="denied")
                    authority = LaunchAuthority(active_work_id=request.work_id)
                return await WorkAttachments(
                    authority,
                    cast(AttachmentState, self.state),
                    cast(dict[str, AttachmentProvider], self.providers),
                ).get(request)
        except (SQLAlchemyError, ProviderError, ValueError, KeyError):
            return WorkAttachmentResult(status="unknown")

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
