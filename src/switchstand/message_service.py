"""Semantic agent-message adapter over the durable MessageState protocol."""

from collections.abc import Awaitable, Callable
from typing import Literal, Self
from uuid import UUID

from pydantic import Field, JsonValue, model_validator
from sqlalchemy.exc import SQLAlchemyError

from .contracts import ApiVersion, ClosedModel
from .core import State
from .grant_state import GrantState
from .grants import PrincipalContext, WorkGrant
from .messages import (
    DispositionEvidence,
    MessageDispositionRequest,
    MessagePendingRequest,
    MessagePendingResult,
    MessageReceiveRequest,
    MessageReplyContext,
    MessageRoute,
    MessageState,
    MessageSubmitRequest,
    MessageSubmitResult,
    MessageTransitionResult,
    RuntimeCurrentness,
    disposition_digest,
)

PrincipalResolver = Callable[[], Awaitable[PrincipalContext | None]]
GenerationResolver = Callable[[], Awaitable[str | None]]
RuntimeFailure = tuple[
    Literal["stale", "recovery_required"],
    Literal["runtime_generation_changed", "runtime_currentness_unavailable"],
]


class MessageSend(ClosedModel):
    api_version: ApiVersion
    message_id: UUID
    recipient_work_id: UUID
    route_ref: str = Field(min_length=1, pattern=r"^[a-z][a-z0-9_.-]*$")
    payload: JsonValue


class MessageReply(ClosedModel):
    api_version: ApiVersion
    message_id: UUID
    in_reply_to_delivery_id: UUID
    payload: JsonValue


class MessagePending(ClosedModel):
    api_version: ApiVersion
    cursor: UUID | None = None
    limit: int = Field(default=50, ge=1, le=100)


class MessageDelivery(ClosedModel):
    api_version: ApiVersion
    delivery_id: UUID


class MessageDisposition(ClosedModel):
    api_version: ApiVersion
    delivery_id: UUID
    result_message_id: UUID | None = None
    operation_id: UUID | None = None

    @model_validator(mode="after")
    def exact_evidence(self) -> Self:
        if (self.result_message_id is None) == (self.operation_id is None):
            raise ValueError("disposition requires exactly one result or provider-effect identity")
        return self


class MessageService:
    """Resolve all provider/grant/runtime details on the trusted server side."""

    def __init__(
        self,
        principal: PrincipalResolver,
        state: State,
        grants: GrantState,
        messages: MessageState,
        generation: str,
        current_generation: GenerationResolver | None = None,
    ):
        if not generation:
            raise ValueError("message runtime generation must be non-empty")
        self.principal = principal
        self.state = state
        self.grants = grants
        self.messages = messages
        self.generation = generation
        self.current_generation = current_generation

    async def _current(self) -> tuple[PrincipalContext, WorkGrant] | None:
        principal = await self.principal()
        if principal is None:
            return None
        grant = await self.grants.current(principal.key)
        if grant is None or grant.principal != principal or not grant.current():
            return None
        return principal, grant

    async def _runtime(self) -> RuntimeCurrentness:
        current = None
        if self.current_generation is not None:
            current = await self.current_generation()
        return RuntimeCurrentness(
            generation=self.generation,
            current_generation=current,
        )

    async def _runtime_failure(self) -> RuntimeFailure | None:
        runtime = await self._runtime()
        if runtime.current_generation is None:
            return "recovery_required", "runtime_currentness_unavailable"
        if runtime.generation != runtime.current_generation:
            return "stale", "runtime_generation_changed"
        return None

    async def _route(self, recipient_work_id: UUID) -> MessageRoute | None:
        handle = await self.state.get(recipient_work_id)
        if handle is None or handle.provider != "asana":
            return None
        recipient = await self.grants.current_for_active_work(recipient_work_id)
        if recipient is None:
            return None
        return MessageRoute(
            recipient_work_id=recipient_work_id,
            recipient_grant_version=recipient.version,
            projection_provider="asana",
            projection_target=handle.provider_work_id,
        )

    async def send(self, request: MessageSend) -> MessageSubmitResult:
        try:
            current = await self._current()
            if current is None:
                return MessageSubmitResult(status="denied", reason="no_current_grant")
            principal, grant = current
            runtime_failure = await self._runtime_failure()
            if runtime_failure is not None:
                return MessageSubmitResult(
                    status=runtime_failure[0], reason=runtime_failure[1]
                )
            route = await self._route(request.recipient_work_id)
            if route is None:
                return MessageSubmitResult(
                    status="recovery_required", reason="recipient_route_unavailable"
                )
            return await self.messages.submit(
                principal,
                route,
                MessageSubmitRequest(
                    api_version=request.api_version,
                    message_id=request.message_id,
                    grant_version=grant.version,
                    route_ref=request.route_ref,
                    kind="request",
                    payload=request.payload,
                ),
            )
        except (SQLAlchemyError, ValueError):
            return MessageSubmitResult(status="recovery_required", reason="state_unavailable")

    async def reply(self, request: MessageReply) -> MessageSubmitResult:
        try:
            current = await self._current()
            if current is None:
                return MessageSubmitResult(status="denied", reason="no_current_grant")
            principal, grant = current
            runtime_failure = await self._runtime_failure()
            if runtime_failure is not None:
                return MessageSubmitResult(
                    status=runtime_failure[0], reason=runtime_failure[1]
                )
            context: MessageReplyContext | None = await self.messages.reply_context(
                request.in_reply_to_delivery_id
            )
            if context is None:
                return MessageSubmitResult(status="conflict", reason="reply_delivery_not_found")
            route = await self._route(context.recipient_work_id)
            if route is None:
                return MessageSubmitResult(
                    status="recovery_required", reason="recipient_route_unavailable"
                )
            return await self.messages.submit(
                principal,
                route,
                MessageSubmitRequest(
                    api_version=request.api_version,
                    message_id=request.message_id,
                    grant_version=grant.version,
                    route_ref=context.route_ref,
                    kind="result",
                    payload=request.payload,
                    in_reply_to_delivery_id=request.in_reply_to_delivery_id,
                ),
            )
        except (SQLAlchemyError, ValueError):
            return MessageSubmitResult(status="recovery_required", reason="state_unavailable")

    async def pending(self, request: MessagePending) -> MessagePendingResult:
        try:
            current = await self._current()
            if current is None:
                return MessagePendingResult(status="denied", reason="no_current_grant")
            principal, grant = current
            runtime_failure = await self._runtime_failure()
            if runtime_failure is not None:
                return MessagePendingResult(
                    status=runtime_failure[0], reason=runtime_failure[1]
                )
            return await self.messages.pending(
                principal,
                MessagePendingRequest(
                    api_version=request.api_version,
                    grant_version=grant.version,
                    cursor=request.cursor,
                    limit=request.limit,
                ),
            )
        except (SQLAlchemyError, ValueError):
            return MessagePendingResult(status="recovery_required", reason="state_unavailable")

    async def receive(self, request: MessageDelivery) -> MessageTransitionResult:
        return await self._transition("receive", request)

    async def recover(self, request: MessageDelivery) -> MessageTransitionResult:
        return await self._transition("recover", request)

    async def _transition(
        self, operation: str, request: MessageDelivery,
    ) -> MessageTransitionResult:
        try:
            current = await self._current()
            if current is None:
                return MessageTransitionResult(status="denied", reason="no_current_grant")
            principal, grant = current
            internal = MessageReceiveRequest(
                api_version=request.api_version,
                delivery_id=request.delivery_id,
                grant_version=grant.version,
            )
            runtime = await self._runtime()
            if operation == "receive":
                return await self.messages.receive(principal, runtime, internal)
            if operation == "recover":
                return await self.messages.recover(principal, runtime, internal)
            raise ValueError("unsupported message transition")
        except (SQLAlchemyError, ValueError):
            return MessageTransitionResult(status="recovery_required", reason="state_unavailable")

    async def disposition(self, request: MessageDisposition) -> MessageTransitionResult:
        try:
            current = await self._current()
            if current is None:
                return MessageTransitionResult(status="denied", reason="no_current_grant")
            principal, grant = current
            evidence = (
                DispositionEvidence(kind="result", result_message_id=request.result_message_id)
                if request.result_message_id is not None
                else DispositionEvidence(kind="provider_effect", operation_id=request.operation_id)
            )
            internal = MessageDispositionRequest(
                api_version=request.api_version,
                delivery_id=request.delivery_id,
                grant_version=grant.version,
                disposition_digest=disposition_digest(evidence),
                evidence=evidence,
            )
            return await self.messages.disposition(
                principal, await self._runtime(), internal
            )
        except (SQLAlchemyError, ValueError):
            return MessageTransitionResult(status="recovery_required", reason="state_unavailable")
