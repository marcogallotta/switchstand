"""Pure mailbox notice projections; observation never acknowledges or starts work."""

from __future__ import annotations

import json
from collections.abc import Collection, Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Self, cast
from uuid import UUID

if TYPE_CHECKING:
    from .messages import PendingMessage

MARKER = "SWITCHSTAND_INBOUND_DELIVERIES="


def delivery_marker(deliveries: Iterable[UUID]) -> str:
    return MARKER + json.dumps([str(delivery) for delivery in deliveries])


def marker_receipts(text: str) -> set[str]:
    """Caller must first establish that this text is an exact user-input receipt."""
    return set(json.loads(text.split("\n", 1)[0][len(MARKER):])) if text.startswith(MARKER) else set()


def delivery_notice(deliveries: Iterable[tuple[UUID, PendingMessage]],
                    review_routes: Collection[str]) -> str:
    selected = list(deliveries)
    hints: list[str] = []
    for delivery, message in selected:
        if message.route_ref not in review_routes:
            hints.append(f"{delivery}: ordinary; use your own pending/receive/reply/disposition APIs.")
            continue
        payload = message.payload
        brief = payload.get("brief") if isinstance(payload, dict) else None
        basis = brief.get("basis") if isinstance(brief, dict) else None
        try:
            if message.route_ref == "review.acquisition" and getattr(message, "kind", None) == "result":
                if not isinstance(payload, dict):
                    raise TypeError("missing reviewer binding")
                review_id = str(UUID(str(payload["review_id"])))
            else:
                if not isinstance(basis, dict):
                    raise TypeError("missing review basis")
                review_id = str(UUID(str(basis["review_id"])))
        except (KeyError, TypeError, ValueError) as error:
            raise RuntimeError("UNKNOWN: typed review identity unavailable") from error
        action = ("receive this review and use review_submit, never ordinary result_send"
                  if message.route_ref == "review.request" and message.kind == "request" else
                  "use review_get; never ordinary result_send")
        hints.append(f"{delivery}: {message.route_ref} review_id={review_id}; {action}.")
    return (delivery_marker(delivery for delivery, _ in selected)
            + "\nInbound durable mailbox message.\n" + "\n".join(hints))


@dataclass(frozen=True)
class MailboxSummary:
    groups: tuple[tuple[str, int], ...]

    @property
    def count(self) -> int:
        return sum(count for _, count in self.groups)

    def response(self, name: str) -> dict[str, object]:
        return {"status": "ok", "name": name, "count": self.count,
                "senders": [{"name": sender, "count": count} for sender, count in self.groups]}

    @classmethod
    def parse(cls, value: Any) -> Self:
        if not isinstance(value, dict):
            raise ValueError("mailbox read did not return status ok")  # noqa: TRY004 -- retain watcher protocol
        value = cast(dict[str, Any], value)
        if value.get("status") != "ok":
            raise ValueError("mailbox read did not return status ok")
        count, senders = value.get("count"), value.get("senders")
        if type(count) is not int or count < 0 or not isinstance(senders, list):
            raise ValueError("invalid mailbox summary")
        groups: list[tuple[str, int]] = []
        for sender in cast(list[Any], senders):
            if not isinstance(sender, dict):
                raise ValueError("invalid sender summary")  # noqa: TRY004 -- retain watcher protocol
            sender = cast(dict[str, Any], sender)
            name, amount = sender.get("name"), sender.get("count")
            if not isinstance(name, str) or type(amount) is not int or amount <= 0:
                raise ValueError("invalid sender summary")
            groups.append((name, amount))
        summary = cls(tuple(groups))
        if summary.count != count:
            raise ValueError("inconsistent mailbox summary")
        return summary

    def notice(self, name: str) -> str:
        senders = ", ".join(f"{count} from {sender}" for sender, count in self.groups)
        return (f"Messages for {name}: {self.count} unacknowledged ({senders}). "
                "Use agent_message_pending to process; watcher acknowledged nothing.")
