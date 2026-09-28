"""Public agent-name views over the existing durable MessageState records."""

from dataclasses import dataclass
from typing import Literal
from uuid import UUID

from pydantic import JsonValue, model_validator

from .agent_mailboxes import AgentMailbox, AgentMailboxState
from .contracts import ClosedModel
from .grants import PrincipalContext
from .messages import PendingMessage


@dataclass(frozen=True)
class AgentMessageContext:
    principal: PrincipalContext
    mailbox: AgentMailbox


class AgentRegistrationResult(ClosedModel):
    status: Literal["ok", "conflict", "denied", "stale", "recovery_required"]
    name: str | None = None
    reason: str | None = None

    @model_validator(mode="after")
    def exact_shape(self):
        if self.status == "ok":
            if self.name is None or self.reason is not None:
                raise ValueError("successful registration requires only name")
        elif self.name is not None or self.reason is None:
            raise ValueError("failed registration requires only reason")
        return self


class AgentPendingMessage(ClosedModel):
    delivery_id: UUID
    message_id: UUID
    sender_name: str
    recipient_name: str
    kind: Literal["request", "result"]
    payload: JsonValue
    state: Literal["AVAILABLE", "RECEIVED", "DISPOSITIONED"]


class AgentMessageSubmitResult(ClosedModel):
    status: Literal["ok", "conflict", "denied", "stale", "recovery_required"]
    message: AgentPendingMessage | None = None
    reason: str | None = None

    @model_validator(mode="after")
    def exact_shape(self):
        if self.status == "ok":
            if self.message is None or self.reason is not None:
                raise ValueError("successful submit requires only message")
        elif self.message is not None or self.reason is None:
            raise ValueError("failed submit requires only reason")
        return self


class AgentMessagePendingResult(ClosedModel):
    status: Literal["ok", "denied", "stale", "recovery_required"]
    messages: tuple[AgentPendingMessage, ...] = ()
    next_cursor: UUID | None = None
    has_more: bool = False
    reason: str | None = None

    @model_validator(mode="after")
    def exact_shape(self):
        if self.status == "ok":
            if self.reason is not None or self.has_more != (self.next_cursor is not None):
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
    return AgentPendingMessage(
        delivery_id=message.delivery_id,
        message_id=message.message_id,
        sender_name=sender.mailbox.name,
        recipient_name=recipient.mailbox.name,
        kind=message.kind,
        payload=message.payload,
        state=message.state,
    )
