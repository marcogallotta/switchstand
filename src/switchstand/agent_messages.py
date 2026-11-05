"""Compatibility imports; implementation lives in :mod:`switchstand.mailbox.agent_messages`."""

from .mailbox.agent_messages import (
    AgentMessageContext,
    AgentMessagePendingResult,
    AgentMessageSubmitResult,
    AgentPendingMessage,
    AgentRegistrationResult,
    AgentTransferRequestResult,
    public_message,
    replay_agent_request,
    send_agent_request,
)

__all__ = [
    "AgentMessageContext",
    "AgentMessagePendingResult",
    "AgentMessageSubmitResult",
    "AgentPendingMessage",
    "AgentRegistrationResult",
    "AgentTransferRequestResult",
    "public_message",
    "replay_agent_request",
    "send_agent_request",
]
