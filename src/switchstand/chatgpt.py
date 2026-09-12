"""Authenticated-caller seam; authentication adapters are trusted host code, never tools."""

from collections.abc import Awaitable, Callable
from uuid import UUID

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
from .core import Controller, Provider, State
from .effects import AppendGateway
from .grant_state import GrantState
from .grants import GrantResult, GrantedWorkResult, GuardOutcome, PrincipalContext, ProtectedAppend

PrincipalResolver = Callable[[], Awaitable[PrincipalContext | None]]


class ChatGPTService:
    def __init__(
        self, principal: PrincipalResolver, state: State,
        grants: GrantState, providers: dict[str, Provider],
    ):
        self.principal, self.state, self.grants, self.providers = principal, state, grants, providers
        self.gateway = AppendGateway(state, grants, providers)
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
        except Exception:
            return GrantResult(status="unknown", principal=principal)

    async def get(self, work_id: UUID | None = None) -> GrantedWorkResult:
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
                    WorkGetRequest(api_version="1", work_id=target)
                )
                return GrantedWorkResult(status=result.status, item=result.item)
        except Exception:
            return GrantedWorkResult(status="unknown")

    async def append(self, request: ProtectedAppend) -> GuardOutcome:
        principal = await self.principal()
        if principal is None:
            return self.gateway.guard(request, "denied", "authenticated_principal_required")
        return await self.gateway.append(principal, request)

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
