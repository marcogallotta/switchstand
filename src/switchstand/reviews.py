"""Durable review occurrences and authenticated direct-request provenance."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal, Self, cast
from uuid import UUID, uuid5

from pydantic import Field, JsonValue, model_validator
from sqlalchemy import and_, insert, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncConnection

from .agent_mailboxes import AgentMailbox, AgentMailboxState
from .canonical_relations import work_parents
from .canonical_work import (
    CanonicalWorkRepository,
    CurrentWork,
    canonical_revision,
    canonical_work,
    normalize_title,
)
from .contracts import ClosedModel
from .messages import (
    MessageRoute,
    MessageState,
    MessageSubmitRequest,
    PendingMessage,
    message_deliveries,
    messages,
)
from .state import work_handles

REVIEW_NAMESPACE = UUID("d966cd4f-9994-4f6f-99cc-6ccca873542d")
REVIEW_PROTOCOL = "switchstand.review.v1"
ReviewKind = Literal["CODE", "DESIGN", "IMPLEMENTATION", "PROCESS", "OPERATIONS", "EVIDENCE"]
ReviewMode = Literal["FULL", "FOCUSED"]


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


def _stable(label: str, *parts: object) -> UUID:
    return uuid5(REVIEW_NAMESPACE, "\0".join((label, *(str(part) for part in parts))))


class ReviewRequest(ClosedModel):
    subject_work_id: UUID
    observed_revision: str = Field(min_length=1)
    review_kind: ReviewKind
    candidate_ref: str | None = Field(default=None, min_length=1, max_length=500)
    mode: ReviewMode = "FULL"
    prior_review_id: UUID | None = None
    finding_ids: tuple[str, ...] = ()

    @model_validator(mode="after")
    def exact_mode(self) -> Self:
        if self.mode == "FULL":
            if self.finding_ids:
                raise ValueError("FULL review cannot select prior findings")
        elif self.prior_review_id is None or not self.finding_ids:
            raise ValueError("FOCUSED review requires prior_review_id and finding_ids")
        if len(set(self.finding_ids)) != len(self.finding_ids):
            raise ValueError("finding_ids must be distinct")
        return self
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


class ReviewResult(ClosedModel):
    status: Literal["SENT", "WAITING_REVIEWER", "STALE", "DENIED", "UNKNOWN"]
    review_id: UUID | None = None
    delivery_id: UUID | None = None
    reason: Literal[
        "subject_not_found", "subject_revision_changed", "prior_review_not_found",
        "requester_not_current", "reviewer_not_available", "reviewer_not_eligible",
        "occurrence_conflict", "message_conflict",
    ] | None = None

    @model_validator(mode="after")
    def exact_result(self) -> Self:
        if self.status == "SENT":
            if self.review_id is None or self.delivery_id is None or self.reason is not None:
                raise ValueError("successful review result requires identities")
        elif self.status == "WAITING_REVIEWER":
            if self.review_id is None or self.reason is None:
                raise ValueError("waiting result requires occurrence and reason")
        elif self.reason is None:
            raise ValueError("failed review result requires reason")
        return self


@dataclass(frozen=True)
class ReviewGuidelines:
    version: str
    digest: str

    def __post_init__(self) -> None:
        if not self.version or len(self.digest) != 64:
            raise ValueError("review guidelines require version and sha256 digest")



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


class ReviewService:
    def __init__(
        self, occurrences: ReviewOccurrenceState, mailboxes: AgentMailboxState,
        message_state: MessageState, policy: ReviewPolicy, guidelines: ReviewGuidelines,
    ):
        self.occurrences, self.works = occurrences, occurrences.works
        self.mailboxes, self.messages = mailboxes, message_state
        self.policy, self.guidelines = policy, guidelines

    @property
    def policy(self) -> ReviewPolicy:
        return self.occurrences.policy

    @policy.setter
    def policy(self, value: ReviewPolicy) -> None:
        self.occurrences.policy = value

    def _basis(self, request: ReviewRequest, requester: AgentMailbox) -> ReviewBasis:
        identity = _digest({
            "protocol": REVIEW_PROTOCOL, "subject": str(request.subject_work_id),
            "revision": request.observed_revision, "kind": request.review_kind,
            "candidate": request.candidate_ref, "requester": str(requester.endpoint_id),
        })
        return ReviewBasis(
            review_id=request.prior_review_id or _stable("occurrence", identity),
            subject_work_id=request.subject_work_id,
            subject_revision=request.observed_revision, review_kind=request.review_kind,
            candidate_ref=request.candidate_ref, mode=request.mode,
            finding_ids=request.finding_ids, requester_endpoint_id=requester.endpoint_id,
            requester_generation=requester.generation, policy_version=self.policy.version,
            guidelines_version=self.guidelines.version,
            guidelines_digest=self.guidelines.digest,
        )

    @staticmethod
    def _brief(basis: ReviewBasis, subject: CurrentWork) -> CanonicalReviewBrief:
        return CanonicalReviewBrief(
            basis=basis, subject_title=subject.title, material_claim=subject.notes,
            instructions=(
                "Independently falsify the material claim; choose your own review path.",
                (
                    "Report material findings with defect, evidence, consequence, affected "
                    "claim, and minimum clearing condition."
                ),
                "Do not treat a preferred remedy as required unless the contract requires it.",
            ),
            named_evidence=(() if basis.candidate_ref is None else (basis.candidate_ref,)),
        )

    async def _send(
        self, sender: AgentMailbox, recipient: AgentMailbox, message_id: UUID,
        payload: JsonValue,
    ) -> PendingMessage | None:
        route = MessageRoute(
            recipient_work_id=recipient.endpoint_id,
            recipient_grant_version=recipient.generation,
        )
        submitted = await self.messages.submit_admitted(
            sender.endpoint_id, route,
            MessageSubmitRequest(
                api_version="1", message_id=message_id, grant_version=sender.generation,
                route_ref="review.request", kind="request", payload=payload,
            ),
            agent_binding=sender,
        )
        return submitted.message if submitted.status == "ok" else None

    async def request(self, request: ReviewRequest, requester: AgentMailbox) -> ReviewResult:
        current = await self.mailboxes.by_endpoint_id(requester.endpoint_id)
        if current.status != "ok" or current.mailbox != requester:
            return ReviewResult(status="DENIED", reason="requester_not_current")
        subject = await self.works.get(request.subject_work_id)
        if subject is None:
            return ReviewResult(status="UNKNOWN", reason="subject_not_found")
        basis = self._basis(request, requester)
        if canonical_revision(subject.work_id, subject.row_version) != request.observed_revision:
            return ReviewResult(status="STALE", review_id=basis.review_id,
                                reason="subject_revision_changed")
        if request.mode != "FULL":
            return ReviewResult(status="DENIED", reason="prior_review_not_found")
        brief = self._brief(basis, subject)
        if not await self.occurrences.ensure(brief):
            return ReviewResult(status="UNKNOWN", review_id=basis.review_id,
                                reason="occurrence_conflict")
        reviewer_name = self.policy.reviewer_name(request.review_kind)
        result = None if reviewer_name is None else await self.mailboxes.by_name(reviewer_name)
        reviewer = None if result is None else result.mailbox
        if reviewer is None:
            return ReviewResult(status="WAITING_REVIEWER", review_id=basis.review_id,
                                reason="reviewer_not_available")
        if not self.policy.eligible(requester, reviewer):
            return ReviewResult(status="DENIED", review_id=basis.review_id,
                                reason="reviewer_not_eligible")
        existing = await self.occurrences.request_delivery(
            basis.review_id, subject_revision=basis.subject_revision,
        )
        if existing is not None:
            delivery, envelope = existing
            if envelope.brief.basis != basis:
                return ReviewResult(status="DENIED", review_id=basis.review_id,
                                    reason="occurrence_conflict")
            return ReviewResult(status="SENT", review_id=basis.review_id,
                                delivery_id=delivery.delivery_id)
        envelope = ReviewEnvelope(
            type="REVIEW_REQUEST", brief=brief, requester_name=requester.name,
            requester_endpoint_id=requester.endpoint_id,
        )
        delivery = await self._send(
            requester, reviewer,
            _stable("reviewer-request", basis.review_id, request.observed_revision,
                    reviewer.endpoint_id),
            cast(JsonValue, envelope.model_dump(mode="json")),
        )
        if delivery is None:
            return ReviewResult(status="UNKNOWN", review_id=basis.review_id,
                                reason="message_conflict")
        return ReviewResult(status="SENT", review_id=basis.review_id,
                            delivery_id=delivery.delivery_id)
