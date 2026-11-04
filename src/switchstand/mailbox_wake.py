"""Compatibility imports; implementation lives in :mod:`switchstand.mailbox.mailbox_wake`."""

from .mailbox.mailbox_wake import (
    MARKER,
    MailboxSummary,
    delivery_marker,
    delivery_notice,
    marker_receipts,
)

__all__ = [
    "MARKER",
    "MailboxSummary",
    "delivery_marker",
    "delivery_notice",
    "marker_receipts",
]
