"""Compatibility imports; implementation lives in :mod:`switchstand.mailbox.agent_mailboxes`."""

from .mailbox.agent_mailboxes import (
    CODEX_COORDINATOR_KEY,
    CODEX_COORDINATOR_NAME,
    LEGACY_ROOT_KEY,
    AgentMailbox,
    AgentMailboxResult,
    AgentMailboxState,
    AgentMailboxTransferResult,
    agent_mailbox_transfer_requests,
    agent_mailboxes,
    agent_name_key,
    chat_session_key,
    coordinator_name_keys,
    session_agent_type,
)

__all__ = [
    "CODEX_COORDINATOR_KEY",
    "CODEX_COORDINATOR_NAME",
    "LEGACY_ROOT_KEY",
    "AgentMailbox",
    "AgentMailboxResult",
    "AgentMailboxState",
    "AgentMailboxTransferResult",
    "agent_mailbox_transfer_requests",
    "agent_mailboxes",
    "agent_name_key",
    "chat_session_key",
    "coordinator_name_keys",
    "session_agent_type",
]
