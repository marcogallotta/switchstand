"""Durable review occurrences and authenticated direct-request provenance."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal
from uuid import UUID, uuid5

from pydantic import Field
from sqlalchemy import and_, insert, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncConnection

from .agent_mailboxes import AgentMailbox, AgentMailboxState
from .canonical_relations import work_parents
from .canonical_work import CanonicalWorkRepository, canonical_work, normalize_title
from .contracts import ClosedModel
from .messages import PendingMessage, message_deliveries, messages
from .state import work_handles

REVIEW_NAMESPACE = UUID("d966cd4f-9994-4f6f-99cc-6ccca873542d")
REVIEW_PROTOCOL = "switchstand.review.v1"
ReviewKind = Literal["CODE", "DESIGN", "IMPLEMENTATION", "PROCESS", "OPERATIONS", "EVIDENCE"]
ReviewMode = Literal["FULL", "FOCUSED"]


def _stable(label: str, *parts: object) -> UUID:
    return uuid5(REVIEW_NAMESPACE, "\0".join((label, *(str(part) for part in parts))))
class ReviewBasis(ClosedModel):
    protocol: Literal["switchstand.review.v1"] = REVIEW_PROTOCOL
    review_id: UUID
    subject_work_id: UUID
    subject_revision: str
    review_kind: ReviewKind
    candidate_ref: str | None = None
    mode: ReviewMode
    finding_ids: tuple[str, ...] = ()
    requester_endpoint_id: UUID
    requester_generation: int
    policy_version: str
    guidelines_version: str
    guidelines_digest: str = Field(pattern=r"^[0-9a-f]{64}$")


class CanonicalReviewBrief(ClosedModel):
    basis: ReviewBasis
    subject_title: str
    material_claim: str
    instructions: tuple[str, ...]
    named_evidence: tuple[str, ...] = ()


class ReviewEnvelope(ClosedModel):
    type: Literal["REVIEW_REQUEST", "REVIEWER_ACQUISITION"]
    brief: CanonicalReviewBrief
    requester_name: str
    requester_endpoint_id: UUID



@dataclass(frozen=True)
class ReviewPolicy:
    version: str
    reviewer_by_kind: Mapping[ReviewKind, str]
    def reviewer_name(self, review_kind: ReviewKind) -> str | None:
        return self.reviewer_by_kind.get(review_kind)

    @staticmethod
    def eligible(requester: AgentMailbox, reviewer: AgentMailbox) -> bool:
        return (
            requester.endpoint_id != reviewer.endpoint_id
            and requester.principal_key != reviewer.principal_key
        )


@dataclass(frozen=True)
class _ReviewRecord:
    delivery: PendingMessage
    reply_to: UUID | None


class ReviewOccurrenceState:
    def __init__(
        self, works: CanonicalWorkRepository, mailboxes: AgentMailboxState,
        policy: ReviewPolicy,
    ):
        self.works = works
        self.mailboxes, self.policy = mailboxes, policy
        self.engine = works.engine
    async def ensure(self, brief: CanonicalReviewBrief) -> bool:
        basis = brief.basis
        title = f"Review: {brief.subject_title}"[:500]
        notes = (
            "Server-owned review occurrence. Machine state is stored in typed MessageState "
            f"payloads. Subject {basis.subject_work_id} at {basis.subject_revision}."
        )
        try:
            async with self.engine.begin() as connection:
                await connection.execute(pg_insert(work_handles).values(
                    id=basis.review_id, provider="postgres",
                    provider_work_id=str(basis.review_id),
                ).on_conflict_do_nothing())
                handle = (await connection.execute(select(work_handles).where(
                    work_handles.c.id == basis.review_id
                ).with_for_update())).mappings().one_or_none()
                if handle is None or (
                    handle["provider"], handle["provider_work_id"]
                ) != ("postgres", str(basis.review_id)):
                    return False
                created = await connection.scalar(
                    pg_insert(canonical_work).values(
                        work_id=basis.review_id, title=title,
                        normalized_title=normalize_title(title), completed=False,
                        notes=notes, work_type="REVIEW", lifecycle_state="CURRENT",
                        row_version=1,
                    ).on_conflict_do_nothing().returning(canonical_work.c.work_id)
                )
                if created is not None:
                    await connection.execute(insert(work_parents).values(
                        child_work_id=basis.review_id,
                        parent_work_id=basis.subject_work_id,
                    ))
                existing = (await connection.execute(select(canonical_work).where(
                    canonical_work.c.work_id == basis.review_id
                ).with_for_update())).mappings().one_or_none()
                parent = await connection.scalar(select(work_parents.c.parent_work_id).where(
                    work_parents.c.child_work_id == basis.review_id
                ))
                return (
                    existing is not None
                    and parent == basis.subject_work_id
                    and existing["work_type"] == "REVIEW"
                )
        except (IntegrityError, SQLAlchemyError, ValueError):
            return False
    async def records(
        self, review_id: UUID, connection: AsyncConnection | None = None,
    ) -> tuple[_ReviewRecord, ...]:
        query = select(
            message_deliveries.c.delivery_id, message_deliveries.c.message_id,
            message_deliveries.c.sender_work_id, message_deliveries.c.recipient_work_id,
            messages.c.route_ref, messages.c.kind, messages.c.payload,
            message_deliveries.c.state, message_deliveries.c.recipient_grant_version,
            message_deliveries.c.receiving_generation, messages.c.in_reply_to_delivery_id,
        ).join(messages, and_(
            messages.c.sender_work_id == message_deliveries.c.sender_work_id,
            messages.c.message_id == message_deliveries.c.message_id,
        )).where(
            messages.c.route_ref.in_(("review.request", "review.acquisition", "review.outcome")),
            or_(
                messages.c.payload["brief"]["basis"]["review_id"].astext == str(review_id),
                messages.c.payload["review_id"].astext == str(review_id),
            ),
        ).order_by(messages.c.created_at, message_deliveries.c.delivery_id)
        if connection is None:
            async with self.engine.connect() as owned:
                rows = (await owned.execute(query)).mappings().all()
        else:
            rows = (await connection.execute(query)).mappings().all()
        return tuple(_ReviewRecord(
            PendingMessage.model_validate({
                key: value for key, value in row.items()
                if key != "in_reply_to_delivery_id"
            }), row["in_reply_to_delivery_id"],
        ) for row in rows)
    @staticmethod
    @staticmethod
    def request_envelope(record: _ReviewRecord) -> ReviewEnvelope | None:
        try:
            envelope = ReviewEnvelope.model_validate(record.delivery.payload)
        except ValueError:
            return None
        basis, delivery = envelope.brief.basis, record.delivery
        expected_id = _stable(
            "reviewer-request", basis.review_id, basis.subject_revision,
            delivery.recipient_work_id,
        )
        if (
            envelope.type != "REVIEW_REQUEST"
            or envelope.requester_endpoint_id != basis.requester_endpoint_id
            or delivery.route_ref != "review.request"
            or delivery.kind != "request"
            or record.reply_to is not None
            or delivery.message_id != expected_id
        ):
            return None
        return envelope

    async def request_sources(
        self, review_id: UUID, connection: AsyncConnection | None = None,
    ) -> tuple[tuple[_ReviewRecord, ReviewEnvelope], ...]:
        found: list[tuple[_ReviewRecord, ReviewEnvelope]] = []
        for record in await self.records(review_id, connection):
            envelope = self.request_envelope(record)
            if envelope is None or envelope.brief.basis.review_id != review_id:
                continue
            basis = envelope.brief.basis
            reviewer_name = self.policy.reviewer_name(basis.review_kind)
            reviewer = ((await self.mailboxes.by_name(reviewer_name)).mailbox
                        if reviewer_name else None)
            if (
                basis.policy_version != self.policy.version
                or reviewer is None
                or record.delivery.sender_work_id != basis.requester_endpoint_id
                or record.delivery.recipient_work_id != reviewer.endpoint_id
            ):
                continue
            found.append((record, envelope))
        return tuple(found)

    async def request_deliveries(
        self, review_id: UUID,
    ) -> tuple[tuple[PendingMessage, ReviewEnvelope], ...]:
        return tuple((record.delivery, envelope)
                     for record, envelope in await self.request_sources(review_id))

    async def request_delivery(
        self, review_id: UUID, *, subject_revision: str | None = None,
    ) -> tuple[PendingMessage, ReviewEnvelope] | None:
        for delivery, envelope in await self.request_deliveries(review_id):
            if subject_revision is None or envelope.brief.basis.subject_revision == subject_revision:
                return delivery, envelope
        return None
