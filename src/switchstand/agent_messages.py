"""Agent-name façade over durable MessageState."""

from typing import Literal
from uuid import UUID

from pydantic import JsonValue

from .agents import AgentBinding, AgentDirectory
from .core import ProviderError, State
from .grant_state import GrantState
from .grants import PrincipalContext, WorkGrant
from .messages import (
    DispositionEvidence,
    MessageDispositionRequest,
    MessagePendingRequest,
    MessagePendingResult,
    MessageReceiveRequest,
    MessageRoute,
    MessageState,
    MessageSubmitRequest,
    MessageSubmitResult,
    MessageTransitionResult,
    RuntimeCurrentness,
    disposition_digest,
)


class AgentMessageFacade:
    def __init__(
        self, state: State, grants: GrantState, messages: MessageState, agents: AgentDirectory,
    ):
        self.state, self.grants, self.messages, self.agents = state, grants, messages, agents

    @staticmethod
    def admitted(principal: PrincipalContext, grant: WorkGrant) -> bool:
        return (
            grant.principal == principal and grant.current() and grant.scope == "workspace"
            and "message" in grant.operations
        )

    async def _route(self, binding: AgentBinding) -> tuple[MessageRoute, WorkGrant] | None:
        recipient = await self.grants.current(binding.principal_key)
        if recipient is None or not recipient.current() or recipient.scope != "workspace":
            return None
        if "message" not in recipient.operations:
            return None
        handle = await self.state.get(binding.mailbox_work_id)
        if handle is None or handle.provider != "asana":
            return None
        return MessageRoute(
            recipient_work_id=binding.mailbox_work_id,
            recipient_grant_version=recipient.version,
            projection_provider="asana",
            projection_target=handle.provider_work_id,
        ), recipient

    async def send(
        self, principal: PrincipalContext, grant: WorkGrant, sender: AgentBinding,
        recipient_name: str, message_id: UUID, payload: JsonValue,
    ) -> MessageSubmitResult:
        if not self.admitted(principal, grant):
            return MessageSubmitResult(status="denied", reason="actor_not_admitted")
        recipient = await self.agents.resolve(recipient_name)
        if recipient is None:
            return MessageSubmitResult(status="denied", reason="recipient_route_unavailable")
        route_data = await self._route(recipient)
        if route_data is None:
            return MessageSubmitResult(status="denied", reason="recipient_route_unavailable")
        route, _ = route_data
        request = MessageSubmitRequest(
            api_version="1", message_id=message_id, grant_version=grant.version,
            route_ref=recipient_name, kind="request", payload=payload,
        )
        try:
            return await self.messages.submit_admitted(
                sender.mailbox_work_id, route, request
            )
        except (ProviderError, ValueError):
            return MessageSubmitResult(status="recovery_required", reason="state_unavailable")

    async def pending(
        self, binding: AgentBinding, grant: WorkGrant, cursor: UUID | None, limit: int,
    ) -> MessagePendingResult:
        if not grant.current() or grant.scope != "workspace" or "message" not in grant.operations:
            return MessagePendingResult(status="denied", reason="actor_not_admitted")
        return await self.messages.pending_admitted(
            binding.mailbox_work_id,
            MessagePendingRequest(
                api_version="1", grant_version=grant.version, cursor=cursor, limit=limit,
            ),
        )

    async def transition(
        self, operation: Literal["receive", "recover"], principal: PrincipalContext,
        grant: WorkGrant, binding: AgentBinding, session_generation: str, delivery_id: UUID,
    ) -> MessageTransitionResult:
        if not self.admitted(principal, grant):
            return MessageTransitionResult(status="denied", reason="no_current_grant")
        request = MessageReceiveRequest(
            api_version="1", delivery_id=delivery_id, grant_version=grant.version,
        )
        runtime = RuntimeCurrentness(
            generation=session_generation, current_generation=session_generation,
        )
        return await getattr(self.messages, operation)(
            principal, runtime, request, work_id=binding.mailbox_work_id
        )

    async def result_send(
        self, principal: PrincipalContext, grant: WorkGrant, sender: AgentBinding,
        session_generation: str, delivery_id: UUID, message_id: UUID, payload: JsonValue,
    ) -> MessageSubmitResult:
        if not self.admitted(principal, grant):
            return MessageSubmitResult(status="denied", reason="actor_not_admitted")
        context = await self.messages.reply_context(delivery_id)
        if context is None:
            return MessageSubmitResult(status="conflict", reason="reply_delivery_not_found")
        recipient = await self.agents.resolve_mailbox(context.recipient_work_id)
        if recipient is None:
            return MessageSubmitResult(status="denied", reason="recipient_route_unavailable")
        route_data = await self._route(recipient)
        if route_data is None:
            return MessageSubmitResult(status="denied", reason="recipient_route_unavailable")
        route, _ = route_data
        request = MessageSubmitRequest(
            api_version="1", message_id=message_id, grant_version=grant.version,
            route_ref=context.route_ref, kind="result", payload=payload,
            in_reply_to_delivery_id=delivery_id,
        )
        return await self.messages.submit_received_result(
            sender.mailbox_work_id, grant.version, session_generation, route, request
        )

    async def disposition(
        self, principal: PrincipalContext, grant: WorkGrant, binding: AgentBinding,
        session_generation: str, delivery_id: UUID, result_message_id: UUID,
    ) -> MessageTransitionResult:
        if not self.admitted(principal, grant):
            return MessageTransitionResult(status="denied", reason="no_current_grant")
        evidence = DispositionEvidence(kind="result", result_message_id=result_message_id)
        return await self.messages.disposition(
            principal,
            RuntimeCurrentness(
                generation=session_generation, current_generation=session_generation,
            ),
            MessageDispositionRequest(
                api_version="1", delivery_id=delivery_id, grant_version=grant.version,
                disposition_digest=disposition_digest(evidence), evidence=evidence,
            ),
            work_id=binding.mailbox_work_id,
        )
