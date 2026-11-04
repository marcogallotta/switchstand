"""Public agent-name views over the existing durable MessageState records."""

from dataclasses import dataclass
from typing import Literal
from uuid import UUID

from pydantic import Field, JsonValue, model_validator
from sqlalchemy import select

from .agent_mailboxes import AgentMailbox, AgentMailboxState
from .contracts import ClosedModel
from .durable_capture import (
    DurableCaptureGuidance,
    durable_capture_guidance,
    message_capture_guidance,
)
from .grants import PrincipalContext
from .messages import (
    AGENT_MESSAGE_PAYLOAD_LIMIT,
    MessageRoute,
    MessageSendRequest,
    MessageState,
    MessageSubmitRequest,
    MessageSubmitResult,
    PendingMessage,
    canonical_payload_chars,
    message_deliveries,
    messages,
)


@dataclass(frozen=True)
class AgentMessageContext:
    principal: PrincipalContext
    mailbox: AgentMailbox


class AgentRegistrationResult(ClosedModel):
    status: Literal["ok", "conflict", "denied", "stale", "recovery_required"]
    name: str | None = None
    reason: str | None = None
    next_action: str | None = None

    @model_validator(mode="after")
    def exact_shape(self):
        if self.status == "ok":
            if self.name is None or self.reason is not None:
                raise ValueError("successful registration requires a name and no reason")
        elif self.name is not None or self.reason is None:
            raise ValueError("failed registration requires a reason and no name")
        return self


class AgentTransferRequestResult(ClosedModel):
    status: Literal["ok", "conflict", "denied", "stale", "recovery_required"]
    request_id: UUID | None = None
    name: str | None = None
    reason: str | None = None

    @model_validator(mode="after")
    def exact_shape(self):
        if self.status == "ok":
            if self.request_id is None or self.name is None or self.reason is not None:
                raise ValueError("successful transfer request requires identity and name")
        elif self.request_id is not None or self.name is not None or self.reason is None:
            raise ValueError("failed transfer request requires only reason")
        return self


class AgentPendingMessage(ClosedModel):
    delivery_id: UUID
    message_id: UUID
    sender_name: str
    recipient_name: str
    kind: Literal["request", "result"]
    payload: JsonValue
    state: Literal["AVAILABLE", "RECEIVED", "DISPOSITIONED"]
    durable_capture: DurableCaptureGuidance = Field(
        default_factory=lambda: durable_capture_guidance((), ()),
    )


class AgentMessageSubmitResult(ClosedModel):
    status: Literal["ok", "conflict", "denied", "stale", "recovery_required"]
    message: AgentPendingMessage | None = None
    reason: str | None = None
    next_action: str | None = None

    @model_validator(mode="after")
    def exact_shape(self):
        if self.status == "ok":
            if self.message is None or self.reason is not None:
                raise ValueError("successful submit requires only message")
        elif self.message is not None or self.reason is None:
            raise ValueError("failed submit requires a reason and no message")
        return self


class AgentMessagePendingResult(ClosedModel):
    status: Literal["ok", "denied", "stale", "recovery_required"]
    messages: tuple[AgentPendingMessage, ...] = ()
    next_cursor: UUID | None = None
    has_more: bool = False
    reason: str | None = None
    next_action: str | None = None

    @model_validator(mode="after")
    def exact_shape(self):
        if self.status == "ok":
            if (
                self.reason is not None or self.next_action is not None
                or self.has_more != (self.next_cursor is not None)
            ):
                raise ValueError("successful pending page shape invalid")
        elif self.messages or self.next_cursor is not None or self.has_more or self.reason is None:
            raise ValueError("failed pending page shape invalid")
        return self


async def public_message(
    mailboxes: AgentMailboxState, message: PendingMessage,
) -> AgentPendingMessage | None:
    sender = await mailboxes.by_endpoint_id(message.sender_work_id)
    recipient = await mailboxes.by_endpoint_id(message.recipient_work_id)
    if (
        sender.status != "ok" or sender.mailbox is None
        or recipient.status != "ok" or recipient.mailbox is None
    ):
        return None
    payload_chars = canonical_payload_chars(message.payload)
    payload_omitted = payload_chars > AGENT_MESSAGE_PAYLOAD_LIMIT
    return AgentPendingMessage(
        delivery_id=message.delivery_id,
        message_id=message.message_id,
        sender_name=sender.mailbox.name,
        recipient_name=recipient.mailbox.name,
        kind=message.kind,
        payload=None if payload_omitted else message.payload,
        state="RECEIVED" if message.state == "ACKNOWLEDGED" else message.state,
        durable_capture=(
            durable_capture_guidance((), ())
            if payload_omitted
            else message_capture_guidance(message.payload)
        ),
    )


async def replay_agent_request(
    state: MessageState, sender: AgentMailbox, message_id: UUID, payload: JsonValue,
) -> MessageSubmitResult | None:
    """Exact committed envelope before current evidence/recipient lookup; sender still fenced."""
    async with state.engine.connect() as connection:
        row = (await connection.execute(select(
            message_deliveries.c.recipient_work_id, messages.c.route_ref,
        ).join(messages, (messages.c.sender_work_id == message_deliveries.c.sender_work_id)
               & (messages.c.message_id == message_deliveries.c.message_id)).where(
            messages.c.sender_work_id == sender.endpoint_id,
            messages.c.message_id == message_id,
        ))).mappings().one_or_none()
    if row is None:
        return None
    return await state.committed_public_replay(sender.endpoint_id, MessageSendRequest(
        api_version="1", work_id=sender.endpoint_id, grant_version=sender.generation,
        message_id=message_id, route_ref=row["route_ref"], payload=payload,
        recipient_work_id=row["recipient_work_id"],
    ), agent_binding=sender)


async def send_agent_request(
    mailboxes: AgentMailboxState, state: MessageState, sender: AgentMailbox,
    recipient_name: str, message_id: UUID, payload: JsonValue,
) -> MessageSubmitResult | AgentMessageSubmitResult:
    """Shared named-send owner. Caller MUST derive sender from its admitted actor context."""
    recipient = await mailboxes.by_name(recipient_name)
    if recipient.status == "recovery_required":
        return AgentMessageSubmitResult(status="recovery_required", reason="state_unavailable")
    if recipient.status != "ok" or recipient.mailbox is None:
        return AgentMessageSubmitResult(status="denied", reason="recipient_not_registered")
    replay = await state.committed_public_replay(sender.endpoint_id, MessageSendRequest(
        api_version="1", work_id=sender.endpoint_id, grant_version=sender.generation,
        message_id=message_id, route_ref=f"agent.{recipient.mailbox.name_key}", payload=payload,
        recipient_work_id=recipient.mailbox.endpoint_id,
    ), agent_binding=sender)
    if replay is not None:
        return replay
    if canonical_payload_chars(payload) > AGENT_MESSAGE_PAYLOAD_LIMIT:
        return AgentMessageSubmitResult(status="denied", reason="payload_too_large", next_action=(
            "Keep this mailbox notification within 800 canonical JSON characters; "
            "put detail on the exact tracker WorkId and send only the bounded pointer."
        ))
    return await state.submit_admitted(sender.endpoint_id, MessageRoute(
        recipient_work_id=recipient.mailbox.endpoint_id,
        recipient_grant_version=recipient.mailbox.generation,
    ), MessageSubmitRequest(api_version="1", message_id=message_id,
        grant_version=sender.generation, route_ref=f"agent.{recipient.mailbox.name_key}",
        kind="request", payload=payload), agent_binding=sender)
