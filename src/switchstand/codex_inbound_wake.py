"""Compatibility imports; implementation lives in :mod:`switchstand.mailbox.codex_inbound_wake`."""

from .mailbox.codex_inbound_wake import (
    LOG,
    SOURCES,
    DaemonConnection,
    DaemonRejected,
    InboundWake,
    ThreadNotFound,
    UncertainTurn,
    receipts,
    resolve_thread,
)

__all__ = [
    "LOG",
    "SOURCES",
    "DaemonConnection",
    "DaemonRejected",
    "InboundWake",
    "ThreadNotFound",
    "UncertainTurn",
    "receipts",
    "resolve_thread",
]
