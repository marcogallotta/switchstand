"""Durable review occurrences and authenticated direct-request provenance."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Self, cast
from uuid import UUID, uuid5

from pydantic import Field, JsonValue, model_validator
from sqlalchemy import and_, insert, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncConnection

from .agent_mailboxes import AgentMailbox, AgentMailboxState, agent_mailboxes
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
COORDINATOR_MAILBOX = "Coordinator"
ReviewKind = Literal["CODE", "DESIGN", "IMPLEMENTATION", "PROCESS", "OPERATIONS", "EVIDENCE"]
ReviewMode = Literal["FULL", "FOCUSED"]
ReviewVerdict = Literal["PASS", "FINDINGS", "BLOCKED"]
ContextProvenance = Literal["INHERITED", "UNSEEDED", "UNKNOWN"]


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


def _stable(label: str, *parts: object) -> UUID:
    return uuid5(REVIEW_NAMESPACE, "\0".join((label, *(str(part) for part in parts))))


def _request_message_id(basis: ReviewBasis, recipient_id: UUID) -> UUID:
    label = "reviewer-upgrade" if basis.upgrades_focused else "reviewer-request"
    return _stable(label, basis.review_id, basis.subject_revision, recipient_id)


def _basis_message_id(basis: ReviewBasis) -> UUID:
    return _stable("basis", basis.review_id, _digest(basis.model_dump(mode="json")))


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
                raise ValueError("FULL review cannot select findings")
        elif self.prior_review_id is None or not self.finding_ids:
            raise ValueError("FOCUSED review requires prior_review_id and finding_ids")
        if len(set(self.finding_ids)) != len(self.finding_ids):
            raise ValueError("finding_ids must be distinct")
        return self


class ReviewFinding(ClosedModel):
    finding_id: str = Field(min_length=1, max_length=120)
    defect: str = Field(min_length=1, max_length=4000)
    evidence: str = Field(min_length=1, max_length=4000)
    consequence: str = Field(min_length=1, max_length=4000)
    affected_claim: str = Field(min_length=1, max_length=1000)
    minimum_clearing_condition: str = Field(min_length=1, max_length=4000)


class ReviewSubmit(ClosedModel):
    review_id: UUID
    verdict: ReviewVerdict
    findings: tuple[ReviewFinding, ...] = ()
    evidence_refs: tuple[str, ...] = ()
    context_provenance: ContextProvenance

    @model_validator(mode="after")
    def exact_verdict(self) -> Self:
        if (self.verdict == "FINDINGS") != bool(self.findings):
            raise ValueError("FINDINGS requires findings and other verdicts forbid them")
        if len({finding.finding_id for finding in self.findings}) != len(self.findings):
            raise ValueError("finding identities must be distinct")
        return self
class ReviewBasis(ClosedModel):
    protocol: Literal["switchstand.review.v1"] = REVIEW_PROTOCOL
    review_id: UUID
    subject_work_id: UUID
    subject_revision: str
    review_kind: ReviewKind
    candidate_ref: str | None = None
    mode: ReviewMode
    upgrades_focused: bool = False
    finding_ids: tuple[str, ...] = ()
    requester_endpoint_id: UUID
    requester_generation: int
    policy_version: str
    guidelines_version: str
    guidelines_digest: str = Field(pattern=r"^[0-9a-f]{64}$")


class FocusedReviewTransition(ClosedModel):
    prior_basis: ReviewBasis
    prior_verdict_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    prior_material_claim: str
    prior_named_evidence: tuple[str, ...]
    changed_basis_fields: tuple[Literal["subject_revision", "candidate_ref"], ...]


class CanonicalReviewBrief(ClosedModel):
    basis: ReviewBasis
    subject_title: str
    material_claim: str
    instructions: tuple[str, ...]
    named_evidence: tuple[str, ...] = ()
    focused_from: FocusedReviewTransition | None = None


class ReviewEnvelope(ClosedModel):
    type: Literal["REVIEW_REQUEST", "REVIEWER_ACQUISITION"]
    brief: CanonicalReviewBrief
    requester_name: str
    requester_endpoint_id: UUID
    reviewer_endpoint_id: UUID | None = None
    reviewer_principal_key: str | None = None


class ReviewerBound(ClosedModel):
    type: Literal["REVIEWER_BOUND"] = "REVIEWER_BOUND"
    review_id: UUID
    reviewer_delivery_id: UUID
    reviewer_name: str


class ReviewOutcome(ClosedModel):
    type: Literal["REVIEW_OUTCOME"] = "REVIEW_OUTCOME"
    review_id: UUID
    reviewer_delivery_id: UUID
    verdict: ReviewVerdict
    findings: tuple[ReviewFinding, ...]
    evidence_refs: tuple[str, ...]
    context_provenance: ContextProvenance
    verdict_digest: str = Field(pattern=r"^[0-9a-f]{64}$")


class ReviewOutcomeEnvelope(ClosedModel):
    brief: CanonicalReviewBrief
    outcome: ReviewOutcome


class ReviewResult(ClosedModel):
    status: Literal[
        "SENT", "SUBMITTED", "WAITING_REVIEWER", "REQUEST_UNPICKED", "RECEIVED",
        "FINDINGS", "PASS", "BLOCKED", "STALE", "DENIED", "UNKNOWN",
    ]
    review_id: UUID | None = None
    delivery_id: UUID | None = None
    verdict: ReviewVerdict | None = None
    findings: tuple[ReviewFinding, ...] = ()
    evidence_refs: tuple[str, ...] = ()
    verdict_digest: str | None = None
    reason: Literal[
        "subject_not_found", "subject_revision_changed", "prior_review_not_found",
        "requester_not_current", "coordinator_not_registered", "reviewer_not_available",
        "reviewer_not_eligible", "acquisition_not_received",
        "review_delivery_not_found", "review_delivery_not_received",
        "reviewer_binding_changed", "occurrence_conflict", "message_conflict",
        "review_basis_changed", "state_unavailable", "caller_not_owner",
        "occurrence_not_found", "duplicate_authoritative_state", "recovery_not_allowed",
    ] | None = None
    next_action: str = ""

    @model_validator(mode="after")
    def exact_result(self) -> Self:
        if self.status in {"SENT", "SUBMITTED"}:
            if self.review_id is None or self.delivery_id is None or self.reason is not None:
                raise ValueError("successful review result requires identities")
        elif self.status == "WAITING_REVIEWER":
            if self.review_id is None or self.reason is None:
                raise ValueError("waiting result requires occurrence and reason")
        elif self.status in {"REQUEST_UNPICKED", "RECEIVED", "FINDINGS", "PASS", "BLOCKED"}:
            if self.review_id is None or self.delivery_id is None or self.reason is not None:
                raise ValueError("projected review result requires identities")
        elif self.reason is None:
            raise ValueError("failed review result requires reason")
        terminal = self.status in {"FINDINGS", "PASS", "BLOCKED"}
        if terminal != (self.verdict is not None and self.verdict_digest is not None):
            raise ValueError("terminal review result requires verdict evidence only")
        if not terminal and (self.findings or self.evidence_refs):
            raise ValueError("nonterminal review result cannot expose verdict evidence")
        if not self.next_action:
            value = {
                "FINDINGS": "Apply only the bounded findings, then call review_request in FOCUSED mode on the same review_id with exactly the returned finding_ids.",
                "PASS": "Prepare/send the Human Review package now; review PASS is evidence, not implementation authority.",
                "BLOCKED": "Resolve the stated blocker or replan; do not start another broad review automatically.",
                "SUBMITTED": "Poll review_get for this review_id to read the authoritative verdict.",
            }.get(self.status)
            if value is None:
                value = ("Poll review_get for this review_id; do not stop merely because the request was sent." if self.status in {"SENT", "WAITING_REVIEWER", "REQUEST_UNPICKED", "RECEIVED"} else f"Reconcile {self.reason or 'review state'} before retrying; do not retry blindly.")
            object.__setattr__(self, "next_action", value)
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
        # The endpoint is the durable logical-agent identity.  A principal is
        # an account/client authorization boundary and can legitimately be
        # shared by multiple independent ChatGPT agents.
        return requester.endpoint_id != reviewer.endpoint_id


@dataclass(frozen=True)
class _ReviewRecord:
    delivery: PendingMessage
    reply_to: UUID | None
    created_at: datetime
    received_at: datetime | None
    dispositioned_at: datetime | None


@dataclass(frozen=True)
class ReviewBundleAccess:
    status: Literal["AUTHORIZED", "READY", "STALE", "DENIED", "UNKNOWN"]
    reason: str | None = None
    basis: ReviewBasis | None = None
    delivery_id: UUID | None = None
    subject_title: str | None = None
    material_claim_digest: str | None = None


class ReviewOccurrenceState:
    def __init__(
        self, works: CanonicalWorkRepository, mailboxes: AgentMailboxState,
        policy: ReviewPolicy,
    ):
        self.works = works
        self.mailboxes, self.policy = mailboxes, policy
        self.engine = works.engine
    async def _ensure_in_transaction(
        self, connection: AsyncConnection, brief: CanonicalReviewBrief,
    ) -> bool:
        basis = brief.basis
        title = f"Review: {brief.subject_title}"[:500]
        notes = (
            "Server-owned review occurrence. Machine state is stored in typed MessageState "
            f"payloads. Subject {basis.subject_work_id} at {basis.subject_revision}."
        )
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

    async def ensure(
        self, brief: CanonicalReviewBrief, connection: AsyncConnection | None = None,
    ) -> bool:
        if connection is not None:
            return await self._ensure_in_transaction(connection, brief)
        try:
            async with self.engine.begin() as owned:
                return await self._ensure_in_transaction(owned, brief)
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
            messages.c.created_at, message_deliveries.c.received_at,
            message_deliveries.c.dispositioned_at,
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
        sync = connection.sync_connection if connection is not None else None
        if sync is not None and sync.get_execution_options().get("review_bundle_lock"):
            query = query.with_for_update()
        if connection is None:
            async with self.engine.connect() as owned:
                rows = (await owned.execute(query)).mappings().all()
        else:
            rows = (await connection.execute(query)).mappings().all()
        return tuple(_ReviewRecord(
            PendingMessage.model_validate({
                key: value for key, value in row.items()
                if key not in {
                    "in_reply_to_delivery_id", "created_at", "received_at", "dispositioned_at",
                }
            }), row["in_reply_to_delivery_id"], row["created_at"],
            row["received_at"], row["dispositioned_at"],
        ) for row in rows)
    @staticmethod
    def request_envelope(record: _ReviewRecord) -> ReviewEnvelope | None:
        try:
            envelope = ReviewEnvelope.model_validate(record.delivery.payload)
        except ValueError:
            return None
        basis, delivery = envelope.brief.basis, record.delivery
        expected_id = _request_message_id(basis, delivery.recipient_work_id)
        if (
            envelope.type != "REVIEW_REQUEST"
            or envelope.requester_endpoint_id != basis.requester_endpoint_id
            or delivery.route_ref != "review.request"
            or delivery.kind != "request"
            or record.reply_to is not None
            or delivery.message_id != expected_id
            or envelope.reviewer_endpoint_id not in {None, delivery.recipient_work_id}
        ):
            return None
        return envelope

    @staticmethod
    def basis_envelope(record: _ReviewRecord) -> ReviewEnvelope | None:
        try:
            envelope = ReviewEnvelope.model_validate(record.delivery.payload)
        except ValueError:
            return None
        basis, delivery = envelope.brief.basis, record.delivery
        if (
            envelope.type != "REVIEWER_ACQUISITION"
            or envelope.requester_endpoint_id != basis.requester_endpoint_id
            or delivery.route_ref != "review.acquisition"
            or delivery.kind != "request"
            or record.reply_to is not None
            or delivery.message_id != _basis_message_id(basis)
            or delivery.sender_work_id != basis.requester_endpoint_id
            or delivery.recipient_work_id != basis.review_id
        ):
            return None
        return envelope

    async def basis_sources(
        self, review_id: UUID, connection: AsyncConnection | None = None,
    ) -> tuple[tuple[_ReviewRecord, ReviewEnvelope], ...]:
        return tuple(
            (record, envelope) for record in await self.records(review_id, connection)
            if (envelope := self.basis_envelope(record)) is not None
            and envelope.brief.basis.review_id == review_id
        )

    @staticmethod
    def _acquisition(
        records: tuple[_ReviewRecord, ...], basis: ReviewBasis, coordinator_id: UUID,
    ) -> _ReviewRecord | None:
        for record in records:
            try:
                envelope = ReviewEnvelope.model_validate(record.delivery.payload)
            except ValueError:
                continue
            if (
                envelope.type == "REVIEWER_ACQUISITION"
                and envelope.brief.basis == basis
                and record.delivery.route_ref == "review.acquisition"
                and record.delivery.kind == "request"
                and record.reply_to is None
                and record.delivery.message_id
                == _stable("acquisition", basis.review_id, basis.subject_revision)
                and record.delivery.sender_work_id == basis.requester_endpoint_id
                and record.delivery.recipient_work_id == coordinator_id
            ):
                return record
        return None

    @staticmethod
    def _bound(
        records: tuple[_ReviewRecord, ...], request: _ReviewRecord, basis: ReviewBasis,
    ) -> ReviewerBound | None:
        acquisition = ReviewOccurrenceState._acquisition(
            records, basis, request.delivery.sender_work_id,
        )
        if acquisition is None:
            return None
        for record in records:
            try:
                bound = ReviewerBound.model_validate(record.delivery.payload)
            except ValueError:
                continue
            if (
                record.delivery.route_ref == "review.acquisition"
                and record.delivery.kind == "result"
                and record.reply_to == acquisition.delivery.delivery_id
                and record.delivery.sender_work_id == request.delivery.sender_work_id
                and record.delivery.recipient_work_id == basis.requester_endpoint_id
                and record.delivery.message_id
                == _stable("reviewer-bound", basis.review_id, record.reply_to)
                and bound.review_id == basis.review_id
                and bound.reviewer_delivery_id == request.delivery.delivery_id
            ):
                return bound
        return None

    async def request_sources(
        self, review_id: UUID, connection: AsyncConnection | None = None,
    ) -> tuple[tuple[_ReviewRecord, ReviewEnvelope, ReviewerBound | None], ...]:
        records = await self.records(review_id, connection)
        found: list[tuple[_ReviewRecord, ReviewEnvelope, ReviewerBound | None]] = []
        for record in records:
            envelope = self.request_envelope(record)
            if envelope is None or envelope.brief.basis.review_id != review_id:
                continue
            basis = envelope.brief.basis
            reviewer_name = self.policy.reviewer_name(basis.review_kind)
            reviewer = ((await self.mailboxes.by_name(reviewer_name, connection)).mailbox
                        if reviewer_name else None)
            if (
                basis.policy_version != self.policy.version
                or reviewer is None
                or record.delivery.recipient_work_id != reviewer.endpoint_id
                or record.delivery.recipient_grant_version != reviewer.generation
                or envelope.reviewer_principal_key not in {None, reviewer.principal_key}
            ):
                continue
            bound = None
            if record.delivery.sender_work_id != basis.requester_endpoint_id:
                bound = self._bound(records, record, basis)
                if bound is None or bound.reviewer_name != reviewer.name:
                    continue
            found.append((record, envelope, bound))
        return tuple(found)

    async def stored_request_sources(
        self, review_id: UUID, connection: AsyncConnection | None = None,
    ) -> tuple[tuple[_ReviewRecord, ReviewEnvelope], ...]:
        return tuple(
            (record, envelope) for record in await self.records(review_id, connection)
            if (envelope := self.request_envelope(record)) is not None
            and envelope.brief.basis.review_id == review_id
        )

    async def request_deliveries(
        self, review_id: UUID,
    ) -> tuple[tuple[PendingMessage, ReviewEnvelope], ...]:
        return tuple((record.delivery, envelope)
            for record, envelope, _bound in await self.request_sources(review_id))

    async def acquisition_sources(
        self, review_id: UUID, connection: AsyncConnection | None = None,
    ) -> tuple[tuple[_ReviewRecord, ReviewEnvelope], ...]:
        records = await self.records(review_id, connection)
        coordinator_result = await self.mailboxes.by_name(COORDINATOR_MAILBOX, connection)
        coordinator = coordinator_result.mailbox
        if coordinator_result.status != "ok" or coordinator is None:
            return ()
        found: list[tuple[_ReviewRecord, ReviewEnvelope]] = []
        for record in records:
            try:
                envelope = ReviewEnvelope.model_validate(record.delivery.payload)
            except ValueError:
                continue
            basis, delivery = envelope.brief.basis, record.delivery
            if (
                envelope.type == "REVIEWER_ACQUISITION"
                and basis.review_id == review_id
                and envelope.requester_endpoint_id == basis.requester_endpoint_id
                and delivery.route_ref == "review.acquisition"
                and delivery.kind == "request"
                and record.reply_to is None
                and delivery.message_id
                == _stable("acquisition", review_id, basis.subject_revision)
                and delivery.sender_work_id == basis.requester_endpoint_id
                and delivery.recipient_work_id == coordinator.endpoint_id
            ):
                found.append((record, envelope))
        return tuple(found)

    async def request_delivery(
        self, review_id: UUID, *, subject_revision: str | None = None,
    ) -> tuple[PendingMessage, ReviewEnvelope] | None:
        for delivery, envelope in await self.request_deliveries(review_id):
            if subject_revision is None or envelope.brief.basis.subject_revision == subject_revision:
                return delivery, envelope
        return None

    async def outcome_sources(
        self, review_id: UUID, connection: AsyncConnection | None = None, *,
        request_source: tuple[_ReviewRecord, ReviewEnvelope] | None = None,
    ) -> tuple[tuple[_ReviewRecord, ReviewOutcomeEnvelope], ...]:
        sources = ({request_source[0].delivery.delivery_id: request_source}
                   if request_source is not None else {
            record.delivery.delivery_id: (record, envelope)
            for record, envelope, _bound in await self.request_sources(review_id, connection)
        })
        found: dict[str, tuple[_ReviewRecord, ReviewOutcomeEnvelope]] = {}
        for record in await self.records(review_id, connection):
            try:
                envelope = ReviewOutcomeEnvelope.model_validate(record.delivery.payload)
            except ValueError:
                continue
            outcome = envelope.outcome
            source = sources.get(outcome.reviewer_delivery_id)
            if source is None:
                continue
            request_record, request_envelope = source
            verdict = ReviewSubmit(
                review_id=outcome.review_id, verdict=outcome.verdict,
                findings=outcome.findings, evidence_refs=outcome.evidence_refs,
                context_provenance=outcome.context_provenance,
            )
            delivery = record.delivery
            if (
                envelope.brief == request_envelope.brief
                and outcome.review_id == review_id
                and outcome.verdict_digest == _digest(verdict.model_dump(mode="json"))
                and delivery.route_ref == "review.request"
                and delivery.kind == "result"
                and record.reply_to == request_record.delivery.delivery_id
                and delivery.sender_work_id == request_record.delivery.recipient_work_id
                and delivery.recipient_work_id == request_record.delivery.sender_work_id
                and delivery.message_id == _stable(
                    "verdict", review_id, request_record.delivery.delivery_id,
                )
            ):
                found[outcome.verdict_digest] = record, envelope
        return tuple(found.values())

    async def outcome_envelopes(
        self, review_id: UUID, connection: AsyncConnection | None = None,
    ) -> tuple[ReviewOutcomeEnvelope, ...]:
        return tuple(envelope for _record, envelope in await self.outcome_sources(
            review_id, connection,
        ))

    async def pickup_projection(
        self, connection: AsyncConnection, subject_work_id: UUID,
        subject_revision: str, captured_at: datetime,
    ) -> dict[str, object]:
        def unknown(reason: str) -> dict[str, object]:
            return {
                "status": "UNKNOWN", "reason": reason, "phase": None, "unpicked": None,
                "oldest_request_age_ms": None, "requested_at": None,
                "received_at": None, "verdict_at": None,
            }
        values = (await connection.scalars(select(
            messages.c.payload["brief"]["basis"]["review_id"].astext,
        ).where(
            messages.c.route_ref.in_(("review.request", "review.acquisition")),
            messages.c.payload["brief"]["basis"]["subject_work_id"].astext
            == str(subject_work_id),
            messages.c.payload["brief"]["basis"]["subject_revision"].astext
            == subject_revision,
        ).distinct())).all()
        try:
            review_ids = {UUID(value) for value in values if value}
        except (TypeError, ValueError):
            return unknown("INVALID_OCCURRENCE_ID")
        if len(review_ids) != 1:
            reason = "NO_CURRENT_OCCURRENCE" if not review_ids else "MULTIPLE_CURRENT_OCCURRENCES"
            return unknown(reason)
        review_id = next(iter(review_ids))
        requests = [source for source in await self.request_sources(review_id, connection)
                    if source[1].brief.basis.subject_work_id == subject_work_id
                    and source[1].brief.basis.subject_revision == subject_revision]
        acquisitions = [
            source for source in await self.acquisition_sources(review_id, connection)
            if source[1].brief.basis.subject_work_id == subject_work_id
            and source[1].brief.basis.subject_revision == subject_revision
        ]
        outcomes = [source for source in await self.outcome_sources(review_id, connection)
                    if source[1].brief.basis.subject_work_id == subject_work_id
                    and source[1].brief.basis.subject_revision == subject_revision]
        if len(requests) > 1 or len(acquisitions) > 1 or len(outcomes) > 1:
            return unknown("AMBIGUOUS_OCCURRENCE")
        acquisition_wait = not requests
        if acquisition_wait:
            if len(acquisitions) != 1 or outcomes:
                return unknown("AMBIGUOUS_OCCURRENCE")
            record = acquisitions[0][0]
        else:
            record = requests[0][0]
        verdict_at = outcomes[0][0].created_at if outcomes else None
        times = tuple(value for value in (
            record.created_at, record.received_at, record.dispositioned_at, verdict_at,
        ) if value is not None)
        if (any(value.utcoffset() is None or value > captured_at for value in times)
                or record.received_at is not None and record.received_at < record.created_at
                or record.dispositioned_at is not None and (
                    record.received_at is None or record.dispositioned_at < record.received_at
                )
                or record.delivery.state != "DISPOSITIONED"
                and record.dispositioned_at is not None
                or verdict_at is not None and verdict_at < record.created_at
                or verdict_at is not None and record.received_at is not None
                and verdict_at < record.received_at):
            return unknown("INCONSISTENT_TIMESTAMPS")
        return {
            "status": "KNOWN", "reason": None,
            "phase": (
                "VERDICT" if outcomes else "WAITING_REVIEWER" if acquisition_wait
                else "REQUEST_UNPICKED" if record.delivery.state == "AVAILABLE"
                and record.received_at is None else "RECEIVED"
            ),
            "unpicked": record.delivery.state == "AVAILABLE"
            and record.received_at is None and not outcomes,
            "oldest_request_age_ms": int(
                (captured_at - record.created_at).total_seconds() * 1000
            ),
            "requested_at": record.created_at.isoformat(),
            "received_at": None if record.received_at is None else record.received_at.isoformat(),
            "verdict_at": None if verdict_at is None else verdict_at.isoformat(),
        }

    async def pass_status_in_transaction(
        self, connection: AsyncConnection, subject_work_id: UUID, subject_revision: str,
    ) -> Literal["PASS", "NOT_PASS", "UNKNOWN"]:
        try:
            row_version = await connection.scalar(select(canonical_work.c.row_version).where(
                canonical_work.c.work_id == subject_work_id
            ).with_for_update())
            if row_version is None:
                return "UNKNOWN"
            if canonical_revision(subject_work_id, row_version) != subject_revision:
                return "NOT_PASS"
            review_ids = (await connection.execute(select(
                messages.c.payload["brief"]["basis"]["review_id"].astext,
            ).where(
                messages.c.payload["brief"]["basis"]["subject_work_id"].astext
                == str(subject_work_id),
                messages.c.payload["brief"]["basis"]["subject_revision"].astext
                == subject_revision,
            ).distinct())).scalars()
            outcomes = [
                outcome for value in review_ids if value
                for outcome in await self.outcome_envelopes(UUID(value), connection)
                if outcome.brief.basis.subject_revision == subject_revision
            ]
            return "PASS" if any(value.outcome.verdict == "PASS" for value in outcomes) else "NOT_PASS"
        except (SQLAlchemyError, TypeError, ValueError):
            return "UNKNOWN"


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
            upgrades_focused=request.mode == "FULL" and request.prior_review_id is not None,
            finding_ids=request.finding_ids, requester_endpoint_id=requester.endpoint_id,
            requester_generation=requester.generation, policy_version=self.policy.version,
            guidelines_version=self.guidelines.version,
            guidelines_digest=self.guidelines.digest,
        )

    @staticmethod
    def _brief(
        basis: ReviewBasis, subject: CurrentWork,
        focused_from: FocusedReviewTransition | None = None,
    ) -> CanonicalReviewBrief:
        focused = () if basis.mode == "FULL" else (
            "Recheck only the named findings and the directly affected boundary.",
            "Perform a bounded secondary/global-impact check; report if impact cannot be bounded.",
        )
        return CanonicalReviewBrief(
            basis=basis, subject_title=subject.title, material_claim=subject.notes,
            instructions=(
                "Independently falsify the material claim; choose your own review path.",
                (
                    "Report material findings with defect, evidence, consequence, affected "
                    "claim, and minimum clearing condition."
                ),
                "Do not treat a preferred remedy as required unless the contract requires it.",
            ) + focused,
            named_evidence=(() if basis.candidate_ref is None else (basis.candidate_ref,)),
            focused_from=focused_from,
        )

    async def _send(
        self, sender: AgentMailbox, recipient: AgentMailbox, message_id: UUID,
        route_ref: Literal["review.request", "review.acquisition", "review.outcome"],
        payload: JsonValue, *, reply_to: UUID | None = None,
    ) -> PendingMessage | None:
        route = MessageRoute(
            recipient_work_id=recipient.endpoint_id,
            recipient_grant_version=recipient.generation,
        )
        request = MessageSubmitRequest(
            api_version="1", message_id=message_id, grant_version=sender.generation,
            route_ref=route_ref, kind="result" if reply_to is not None else "request",
            payload=payload, in_reply_to_delivery_id=reply_to,
        )
        submitted = (
            await self.messages.submit_received_result(
                sender.endpoint_id, sender.generation, str(sender.generation),
                route, request, agent_binding=sender,
            ) if reply_to is not None else await self.messages.submit_admitted(
                sender.endpoint_id, route, request, agent_binding=sender,
            )
        )
        return submitted.message if submitted.status == "ok" else None

    async def _record_basis_in_transaction(
        self, connection: AsyncConnection, brief: CanonicalReviewBrief,
        requester: AgentMailbox,
    ) -> PendingMessage | None:
        basis = brief.basis
        envelope = ReviewEnvelope(
            type="REVIEWER_ACQUISITION", brief=brief, requester_name=requester.name,
            requester_endpoint_id=requester.endpoint_id,
        )
        await connection.execute(select(canonical_work.c.work_id).where(
            canonical_work.c.work_id == basis.review_id).with_for_update())
        submitted = await self.messages.submit_review_internal(
            connection, requester, MessageRoute(
                recipient_work_id=basis.review_id, recipient_grant_version=1,
            ), MessageSubmitRequest(
                api_version="1", message_id=_basis_message_id(basis),
                grant_version=requester.generation, route_ref="review.acquisition",
                kind="request", payload=cast(JsonValue, envelope.model_dump(mode="json")),
            ))
        return submitted.message if submitted.status == "ok" else None

    async def _record_basis(
        self, brief: CanonicalReviewBrief, requester: AgentMailbox,
        connection: AsyncConnection | None = None,
    ) -> PendingMessage | None:
        if connection is not None:
            return await self._record_basis_in_transaction(connection, brief, requester)
        async with self.occurrences.engine.begin() as owned:
            return await self._record_basis_in_transaction(owned, brief, requester)

    async def _advance_in_transaction(
        self, connection: AsyncConnection, review_id: UUID, requester: AgentMailbox, *,
        coordinator: AgentMailbox | None = None, acquisition: PendingMessage | None = None,
    ) -> ReviewResult:
        occurrence = (await connection.execute(select(canonical_work).where(
            canonical_work.c.work_id == review_id
        ).with_for_update())).mappings().one_or_none()
        parent = await connection.scalar(select(work_parents.c.parent_work_id).where(
            work_parents.c.child_work_id == review_id
        ).with_for_update())
        bases = await self.occurrences.basis_sources(review_id, connection)
        if occurrence is None or occurrence["work_type"] != "REVIEW" or parent is None:
            return ReviewResult(status="UNKNOWN", review_id=review_id,
                                reason="occurrence_not_found")
        subject = (await connection.execute(select(canonical_work).where(
            canonical_work.c.work_id == parent
        ).with_for_update())).mappings().one_or_none()
        if subject is None:
            return ReviewResult(status="UNKNOWN", review_id=review_id,
                                reason="subject_not_found")
        revision = canonical_revision(cast(UUID, parent), subject["row_version"])
        current_bases = tuple(source for source in bases if (
            source[1].brief.basis.subject_work_id == parent
            and source[1].brief.basis.subject_revision == revision
        ))
        if len(current_bases) != 1:
            return ReviewResult(status="UNKNOWN", review_id=review_id,
                                reason="duplicate_authoritative_state")
        _basis_record, basis_envelope = current_bases[0]
        basis = basis_envelope.brief.basis
        sender = coordinator or requester
        if coordinator is not None:
            records = await self.occurrences.records(review_id, connection)
            received = self.occurrences._acquisition(  # pyright: ignore[reportPrivateUsage]
                records, basis, coordinator.endpoint_id,
            )
            current_coordinator = await self.mailboxes.by_endpoint_id(
                coordinator.endpoint_id, connection,
            )
            if current_coordinator.mailbox != coordinator:
                return ReviewResult(status="DENIED", review_id=review_id,
                                    reason="requester_not_current")
            if received is None or received.delivery != acquisition or (
                received.delivery.state, received.delivery.receiving_generation
            ) != ("RECEIVED", str(coordinator.generation)):
                return ReviewResult(status="DENIED", review_id=review_id,
                                    reason="acquisition_not_received")
        current = await self.mailboxes.by_endpoint_id(requester.endpoint_id, connection)
        if current.status != "ok" or current.mailbox != requester or (
            requester.endpoint_id != basis.requester_endpoint_id
            or requester.generation != basis.requester_generation
        ):
            return ReviewResult(status="DENIED", review_id=review_id,
                                reason="caller_not_owner")
        if (
            revision != basis.subject_revision
            or basis.policy_version != self.policy.version
            or basis.guidelines_version != self.guidelines.version
            or basis.guidelines_digest != self.guidelines.digest
        ):
            return ReviewResult(status="STALE", review_id=review_id,
                                reason="review_basis_changed")
        requests = tuple(source for source in await self.occurrences.stored_request_sources(
            review_id, connection,
        ) if source[1].brief.basis == basis)
        outcomes = tuple(source for source in await self.occurrences.outcome_sources(
            review_id, connection, request_source=requests[0] if len(requests) == 1 else None,
        ) if source[1].brief.basis == basis)
        if len(requests) > 1 or len(outcomes) > 1:
            return ReviewResult(status="UNKNOWN", review_id=review_id,
                                reason="duplicate_authoritative_state")
        if requests:
            if coordinator is not None and len(tuple(
                source for source in await self.occurrences.request_sources(
                    review_id, connection,
                ) if source[1].brief.basis == basis
            )) != 1:
                return ReviewResult(status="UNKNOWN", review_id=review_id,
                                    reason="duplicate_authoritative_state")
            record, _envelope = requests[0]
            return ReviewResult(status="SENT", review_id=review_id,
                                delivery_id=record.delivery.delivery_id)
        reviewer_name = self.policy.reviewer_name(basis.review_kind)
        reviewer_result = None if reviewer_name is None else await self.mailboxes.by_name(
            reviewer_name, connection,
        )
        reviewer = None if reviewer_result is None else reviewer_result.mailbox
        if reviewer is None:
            return ReviewResult(status="WAITING_REVIEWER", review_id=review_id,
                                reason="reviewer_not_available")
        if not self.policy.eligible(requester, reviewer):
            return ReviewResult(status="DENIED", review_id=review_id,
                                reason="reviewer_not_eligible")
        envelope = ReviewEnvelope(
            type="REVIEW_REQUEST", brief=basis_envelope.brief,
            requester_name=requester.name,
            requester_endpoint_id=requester.endpoint_id,
            reviewer_endpoint_id=reviewer.endpoint_id,
            reviewer_principal_key=reviewer.principal_key,
        )
        submitted = await self.messages.submit_review_internal(
            connection, sender,
            MessageRoute(
                recipient_work_id=reviewer.endpoint_id,
                recipient_grant_version=reviewer.generation,
            ), MessageSubmitRequest(
                api_version="1", message_id=_request_message_id(
                    basis, reviewer.endpoint_id,
                ), grant_version=sender.generation, route_ref="review.request",
                kind="request",
                payload=cast(JsonValue, envelope.model_dump(mode="json")),
            ),
        )
        if submitted.status != "ok" or submitted.message is None:
            return ReviewResult(status="UNKNOWN", review_id=review_id,
                                reason="message_conflict")
        if coordinator is not None and acquisition is not None:
            bound = ReviewerBound(
                review_id=review_id, reviewer_delivery_id=submitted.message.delivery_id,
                reviewer_name=reviewer.name,
            )
            acknowledgement = await self.messages.submit_review_internal(
                connection, coordinator, MessageRoute(
                    recipient_work_id=requester.endpoint_id,
                    recipient_grant_version=requester.generation,
                ), MessageSubmitRequest(
                    api_version="1", message_id=_stable(
                        "reviewer-bound", review_id, acquisition.delivery_id,
                    ), grant_version=coordinator.generation,
                    route_ref="review.acquisition", kind="result",
                    payload=cast(JsonValue, bound.model_dump(mode="json")),
                    in_reply_to_delivery_id=acquisition.delivery_id,
                ), received_binding=(coordinator.generation, str(coordinator.generation)),
            )
            if acknowledgement.status != "ok":
                raise ValueError("binding conflict")
        return ReviewResult(status="SENT", review_id=review_id,
                            delivery_id=submitted.message.delivery_id)

    async def _advance(self, review_id: UUID, requester: AgentMailbox) -> ReviewResult:
        try:
            async with self.occurrences.engine.begin() as connection:
                return await self._advance_in_transaction(connection, review_id, requester)
        except (SQLAlchemyError, TypeError, ValueError):
            return ReviewResult(status="UNKNOWN", review_id=review_id,
                                reason="state_unavailable")

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
        focused_from = None
        if request.mode == "FOCUSED":
            for prior in await self.occurrences.outcome_envelopes(basis.review_id):
                prior_basis = prior.brief.basis
                if (
                    prior.outcome.review_id == basis.review_id
                    and prior_basis.subject_work_id == basis.subject_work_id
                    and prior_basis.review_kind == basis.review_kind
                    and prior_basis.requester_endpoint_id == basis.requester_endpoint_id
                    and prior_basis.policy_version == basis.policy_version
                    and prior_basis.guidelines_version == basis.guidelines_version
                    and prior_basis.guidelines_digest == basis.guidelines_digest
                    and prior_basis.subject_revision != basis.subject_revision
                    and set(request.finding_ids).issubset({
                        finding.finding_id for finding in prior.outcome.findings
                    })
                ):
                    changed: tuple[Literal["subject_revision", "candidate_ref"], ...] = (
                        ("subject_revision", "candidate_ref")
                        if prior_basis.candidate_ref != basis.candidate_ref
                        else ("subject_revision",)
                    )
                    focused_from = FocusedReviewTransition(
                        prior_basis=prior_basis,
                        prior_verdict_digest=prior.outcome.verdict_digest,
                        prior_material_claim=prior.brief.material_claim,
                        prior_named_evidence=prior.brief.named_evidence,
                        changed_basis_fields=changed,
                    )
                    break
            if focused_from is None:
                return ReviewResult(status="DENIED", reason="prior_review_not_found")
        elif request.prior_review_id is not None:
            authenticated_upgrade = any(
                prior.brief.basis.mode == "FOCUSED"
                and prior.brief.basis.subject_work_id == basis.subject_work_id
                and prior.brief.basis.subject_revision == basis.subject_revision
                and prior.brief.basis.review_kind == basis.review_kind
                and prior.brief.basis.candidate_ref == basis.candidate_ref
                and prior.brief.basis.requester_endpoint_id == basis.requester_endpoint_id
                and prior.brief.basis.requester_generation == basis.requester_generation
                and prior.brief.basis.policy_version == basis.policy_version
                and prior.brief.basis.guidelines_version == basis.guidelines_version
                and prior.brief.basis.guidelines_digest == basis.guidelines_digest
                for _record, prior, _bound in await self.occurrences.request_sources(
                    basis.review_id
                )
            )
            if not authenticated_upgrade:
                return ReviewResult(status="DENIED", reason="prior_review_not_found")
        brief = self._brief(basis, subject, focused_from)
        try:
            async with self.occurrences.engine.begin() as connection:
                if not await self.occurrences.ensure(brief, connection):
                    return ReviewResult(status="UNKNOWN", review_id=basis.review_id,
                                        reason="occurrence_conflict")
                if await self._record_basis(brief, requester, connection) is None:
                    return ReviewResult(status="UNKNOWN", review_id=basis.review_id,
                                        reason="message_conflict")
                advanced = await self._advance_in_transaction(
                    connection, basis.review_id, requester,
                )
                if (
                    advanced.status == "DENIED"
                    and advanced.reason == "reviewer_not_eligible"
                ):
                    await connection.rollback()
                return advanced
        except (SQLAlchemyError, TypeError, ValueError):
            return ReviewResult(status="UNKNOWN", review_id=basis.review_id,
                                reason="state_unavailable")

    async def get(self, review_id: UUID, requester: AgentMailbox) -> ReviewResult:
        advanced = await self._advance(review_id, requester)
        if advanced.status != "SENT":
            return advanced
        try:
            requests = tuple(source for source in await self.occurrences.stored_request_sources(
                review_id,
            ) if source[0].delivery.delivery_id == advanced.delivery_id)
            if len(requests) != 1:
                return ReviewResult(status="UNKNOWN", review_id=review_id,
                                    reason="duplicate_authoritative_state")
            request_record, request_envelope = requests[0]
            basis = request_envelope.brief.basis
            outcomes = tuple(source for source in await self.occurrences.outcome_sources(
                review_id, request_source=(request_record, request_envelope),
            ) if source[1].brief.basis == basis)
            if len(outcomes) > 1:
                return ReviewResult(status="UNKNOWN", review_id=review_id,
                                    reason="duplicate_authoritative_state")
            subject = await self.works.get(basis.subject_work_id)
            if subject is None:
                return ReviewResult(status="UNKNOWN", review_id=review_id,
                                    reason="subject_not_found")
            if canonical_revision(subject.work_id, subject.row_version) != basis.subject_revision:
                return ReviewResult(status="STALE", review_id=review_id,
                                    reason="subject_revision_changed")
            if outcomes:
                _record, outcome_envelope = outcomes[0]
                outcome = outcome_envelope.outcome
                return ReviewResult(
                    status=outcome.verdict, review_id=review_id,
                    delivery_id=request_record.delivery.delivery_id,
                    verdict=outcome.verdict, findings=outcome.findings,
                    evidence_refs=outcome.evidence_refs,
                    verdict_digest=outcome.verdict_digest,
                )
            delivery = request_record.delivery
            if delivery.state == "AVAILABLE" and delivery.receiving_generation is None:
                status = "REQUEST_UNPICKED"
            elif delivery.state == "RECEIVED" and delivery.receiving_generation is not None:
                status = "RECEIVED"
            else:
                return ReviewResult(status="UNKNOWN", review_id=review_id,
                                    reason="state_unavailable")
            return ReviewResult(status=status, review_id=review_id,
                                delivery_id=delivery.delivery_id)
        except (SQLAlchemyError, TypeError, ValueError):
            return ReviewResult(status="UNKNOWN", review_id=review_id,
                                reason="state_unavailable")

    async def recover(self, review_id: UUID, reviewer: AgentMailbox) -> ReviewResult:
        try:
            async with self.occurrences.engine.begin() as connection:
                occurrence = (await connection.execute(select(canonical_work).where(
                    canonical_work.c.work_id == review_id
                ).with_for_update())).mappings().one_or_none()
                bases = await self.occurrences.basis_sources(review_id, connection)
                parent = await connection.scalar(select(work_parents.c.parent_work_id).where(
                    work_parents.c.child_work_id == review_id
                ))
                subject = None if parent is None else (await connection.execute(
                    select(canonical_work).where(canonical_work.c.work_id == parent).with_for_update()
                )).mappings().one_or_none()
                if occurrence is None or occurrence["work_type"] != "REVIEW" or subject is None:
                    return ReviewResult(status="UNKNOWN", review_id=review_id,
                                        reason="occurrence_not_found")
                revision = canonical_revision(cast(UUID, parent), subject["row_version"])
                bases = tuple(source for source in bases if (
                    source[1].brief.basis.subject_work_id == parent
                    and source[1].brief.basis.subject_revision == revision
                ))
                if len(bases) != 1:
                    return ReviewResult(status="UNKNOWN", review_id=review_id,
                                        reason="duplicate_authoritative_state")
                basis = bases[0][1].brief.basis
                requests = tuple(
                    (record, envelope)
                    for record in await self.occurrences.records(review_id, connection)
                    if (envelope := self.occurrences.request_envelope(record)) is not None
                    and envelope.brief.basis == basis
                )
                if len(requests) != 1:
                    return ReviewResult(status="UNKNOWN", review_id=review_id,
                                        reason="duplicate_authoritative_state")
                outcomes = tuple(source for source in await self.occurrences.outcome_sources(
                    review_id, connection, request_source=requests[0],
                ) if source[1].brief.basis == basis)
                if len(outcomes) > 1:
                    return ReviewResult(status="UNKNOWN", review_id=review_id,
                                        reason="duplicate_authoritative_state")
                if outcomes:
                    return ReviewResult(status="DENIED", review_id=review_id,
                                        reason="recovery_not_allowed")
                current = await self.mailboxes.by_endpoint_id(reviewer.endpoint_id, connection)
                record, envelope = requests[0]
                delivery = record.delivery
                expected_name = self.policy.reviewer_name(basis.review_kind)
                if (
                    current.status != "ok" or current.mailbox != reviewer
                    or expected_name != reviewer.name
                    or envelope.reviewer_endpoint_id != reviewer.endpoint_id
                    or envelope.reviewer_principal_key != reviewer.principal_key
                    or delivery.recipient_work_id != reviewer.endpoint_id
                    or delivery.state != "RECEIVED"
                ):
                    return ReviewResult(status="DENIED", review_id=review_id,
                                        reason="recovery_not_allowed")
                if (
                    revision != basis.subject_revision
                    or basis.policy_version != self.policy.version
                    or basis.guidelines_version != self.guidelines.version
                    or basis.guidelines_digest != self.guidelines.digest
                ):
                    return ReviewResult(status="STALE", review_id=review_id,
                                        reason="review_basis_changed")
                receiving = int(delivery.receiving_generation or 0)
                if receiving == reviewer.generation and (
                    delivery.recipient_grant_version == reviewer.generation
                ):
                    return ReviewResult(status="RECEIVED", review_id=review_id,
                                        delivery_id=delivery.delivery_id)
                if (
                    receiving <= 0
                    or delivery.recipient_grant_version != receiving
                    or receiving >= reviewer.generation
                ):
                    return ReviewResult(status="DENIED", review_id=review_id,
                                        reason="recovery_not_allowed")
                moved = await connection.scalar(update(message_deliveries).where(
                    message_deliveries.c.delivery_id == delivery.delivery_id,
                    message_deliveries.c.state == "RECEIVED",
                    message_deliveries.c.recipient_grant_version
                    == delivery.recipient_grant_version,
                    message_deliveries.c.receiving_generation
                    == delivery.receiving_generation,
                ).values(
                    recipient_grant_version=reviewer.generation,
                    receiving_generation=str(reviewer.generation),
                ).returning(message_deliveries.c.delivery_id))
                if moved is None:
                    return ReviewResult(status="UNKNOWN", review_id=review_id,
                                        reason="state_unavailable")
                return ReviewResult(status="RECEIVED", review_id=review_id,
                                    delivery_id=delivery.delivery_id)
        except (SQLAlchemyError, TypeError, ValueError):
            return ReviewResult(status="UNKNOWN", review_id=review_id,
                                reason="state_unavailable")

    async def continue_acquisition(
        self, review_id: UUID, coordinator: AgentMailbox,
    ) -> ReviewResult:
        current = await self.mailboxes.by_endpoint_id(coordinator.endpoint_id)
        if current.status != "ok" or current.mailbox != coordinator:
            return ReviewResult(status="DENIED", reason="requester_not_current")
        records = await self.occurrences.records(review_id)
        acquisition = None
        for record in reversed(records):
            try:
                envelope = ReviewEnvelope.model_validate(record.delivery.payload)
            except ValueError:
                continue
            basis = envelope.brief.basis
            if (
                envelope.type == "REVIEWER_ACQUISITION"
                and basis.review_id == review_id
                and record.delivery.route_ref == "review.acquisition"
                and record.delivery.kind == "request"
                and record.reply_to is None
                and record.delivery.message_id
                == _stable("acquisition", review_id, basis.subject_revision)
                and record.delivery.sender_work_id == basis.requester_endpoint_id
                and record.delivery.recipient_work_id == coordinator.endpoint_id
            ):
                acquisition = record.delivery, envelope
                break
        if acquisition is None:
            return ReviewResult(status="DENIED", reason="prior_review_not_found")
        incoming, envelope = acquisition
        if (
            incoming.state != "RECEIVED"
            or incoming.receiving_generation != str(coordinator.generation)
        ):
            return ReviewResult(status="DENIED", review_id=review_id,
                                reason="acquisition_not_received")
        basis = envelope.brief.basis
        subject = await self.works.get(basis.subject_work_id)
        if subject is None:
            return ReviewResult(status="UNKNOWN", review_id=review_id,
                                reason="subject_not_found")
        requester_result = await self.mailboxes.by_endpoint_id(basis.requester_endpoint_id)
        requester = requester_result.mailbox
        reviewer_name = self.policy.reviewer_name(basis.review_kind)
        reviewer_result = None if reviewer_name is None else await self.mailboxes.by_name(
            reviewer_name
        )
        reviewer = None if reviewer_result is None else reviewer_result.mailbox
        if requester is None or reviewer is None:
            return ReviewResult(status="WAITING_REVIEWER", review_id=review_id,
                                reason="reviewer_not_available")
        frozen = (
            canonical_revision(subject.work_id, subject.row_version) == basis.subject_revision
            and basis.policy_version == self.policy.version
            and basis.guidelines_version == self.guidelines.version
            and basis.guidelines_digest == self.guidelines.digest
            and requester_result.status == "ok"
            and requester.generation == basis.requester_generation
            and self.policy.eligible(requester, reviewer)
        )
        if not frozen:
            return ReviewResult(status="STALE", review_id=review_id,
                                reason="review_basis_changed")
        try:
            async with self.occurrences.engine.begin() as connection:
                return await self._advance_in_transaction(
                    connection, review_id, requester, coordinator=coordinator,
                    acquisition=incoming,
                )
        except (SQLAlchemyError, TypeError, ValueError):
            return ReviewResult(status="UNKNOWN", review_id=review_id,
                                reason="state_unavailable")

    async def bundle_access(
        self, review_id: UUID, reviewer: AgentMailbox,
        expected: ReviewBasis | None = None,
    ) -> ReviewBundleAccess:
        try:
            final = expected is not None
            context = self.occurrences.engine.begin() if final else self.occurrences.engine.connect()
            async with context as connection:
                connection = await connection.execution_options(review_bundle_lock=final)
                occurrence = select(canonical_work).where(canonical_work.c.work_id == review_id)
                if final:
                    occurrence = occurrence.with_for_update()
                row = (await connection.execute(occurrence)).mappings().one_or_none()
                parent = await connection.scalar(select(work_parents.c.parent_work_id).where(
                    work_parents.c.child_work_id == review_id).with_for_update())
                subject_query = select(canonical_work).where(canonical_work.c.work_id == parent)
                if final:
                    subject_query = subject_query.with_for_update()
                    await connection.execute(select(agent_mailboxes).where(
                        agent_mailboxes.c.endpoint_id == reviewer.endpoint_id).with_for_update())
                subject = (await connection.execute(subject_query)).mappings().one_or_none()
                bases = await self.occurrences.basis_sources(review_id, connection)
                stored = await self.occurrences.stored_request_sources(review_id, connection)
                valid = await self.occurrences.request_sources(review_id, connection)
                if row is None or row["work_type"] != "REVIEW" or subject is None:
                    return ReviewBundleAccess("UNKNOWN", "occurrence_not_found")
                revision = canonical_revision(cast(UUID, parent), subject["row_version"])
                current_bases = tuple(source for source in bases if (
                    source[1].brief.basis.subject_work_id == parent
                    and source[1].brief.basis.subject_revision == revision
                ))
                if len(current_bases) != 1:
                    status, reason = (("STALE", "review_basis_changed") if bases else
                                      ("UNKNOWN", "duplicate_authoritative_state"))
                    return ReviewBundleAccess(status, reason)
                basis = current_bases[0][1].brief.basis
                stored = tuple(source for source in stored
                               if source[1].brief.basis == basis)
                valid = tuple(source for source in valid if source[1].brief.basis == basis)
                if len(stored) != 1 or len(valid) != 1:
                    return ReviewBundleAccess("UNKNOWN", "duplicate_authoritative_state")
                delivery = valid[0][0].delivery
                current = await self.mailboxes.by_endpoint_id(reviewer.endpoint_id, connection)
                if current.mailbox != reviewer or (
                    delivery.recipient_work_id != reviewer.endpoint_id
                    or delivery.state != "RECEIVED"
                    or delivery.recipient_grant_version != reviewer.generation
                    or delivery.receiving_generation != str(reviewer.generation)
                ):
                    return ReviewBundleAccess("DENIED", "reviewer_binding_changed")
                if basis.review_kind not in {"CODE", "IMPLEMENTATION"}:
                    return ReviewBundleAccess("UNKNOWN", "REVIEW_KIND_UNSUPPORTED_V1")
                if basis.candidate_ref is None or final and basis != expected:
                    return ReviewBundleAccess("STALE", "review_basis_changed")
                return ReviewBundleAccess(
                    "READY" if final else "AUTHORIZED",
                    basis=basis, delivery_id=delivery.delivery_id,
                    subject_title=str(subject["title"]),
                    material_claim_digest=_digest(current_bases[0][1].brief.material_claim),
                )
        except (SQLAlchemyError, TypeError, ValueError):
            return ReviewBundleAccess("UNKNOWN", "state_unavailable")

    async def submit(self, request: ReviewSubmit, reviewer: AgentMailbox) -> ReviewResult:
        current = await self.mailboxes.by_endpoint_id(reviewer.endpoint_id)
        if current.status != "ok" or current.mailbox != reviewer:
            return ReviewResult(status="DENIED", reason="reviewer_binding_changed")
        sources = await self.occurrences.request_sources(request.review_id)
        found = None
        current_source = False
        stale_subject = False
        for record, envelope, bound in sources:
            subject = await self.works.get(envelope.brief.basis.subject_work_id)
            authoritative = (
                record.delivery.recipient_work_id == reviewer.endpoint_id
                and (bound is None or bound.reviewer_name == reviewer.name)
            )
            if not authoritative or subject is None:
                continue
            received = (
                record.delivery.state == "RECEIVED"
                and record.delivery.receiving_generation == str(reviewer.generation)
            )
            if canonical_revision(subject.work_id, subject.row_version) == (
                envelope.brief.basis.subject_revision
            ):
                current_source = True
                if received:
                    found = record.delivery, envelope
                    break
                continue
            stale_subject = stale_subject or received
        if found is None:
            if stale_subject and not current_source:
                return ReviewResult(status="STALE", review_id=request.review_id,
                                    reason="subject_revision_changed")
            return ReviewResult(
                status="DENIED", review_id=request.review_id,
                reason=("review_delivery_not_received" if sources
                        else "review_delivery_not_found"),
            )
        delivery, envelope = found
        requester_result = await self.mailboxes.by_endpoint_id(envelope.requester_endpoint_id)
        requester = requester_result.mailbox
        if requester is None or not self.policy.eligible(requester, reviewer):
            return ReviewResult(status="DENIED", review_id=request.review_id,
                                reason="reviewer_not_eligible")
        verdict_digest = _digest(request.model_dump(mode="json"))
        outcome = ReviewOutcome(
            review_id=request.review_id, reviewer_delivery_id=delivery.delivery_id,
            verdict=request.verdict, findings=request.findings,
            evidence_refs=request.evidence_refs,
            context_provenance=request.context_provenance,
            verdict_digest=verdict_digest,
        )
        immediate_result = await self.mailboxes.by_endpoint_id(delivery.sender_work_id)
        immediate = immediate_result.mailbox
        if immediate is None:
            return ReviewResult(status="UNKNOWN", review_id=request.review_id,
                                reason="state_unavailable")
        route = MessageRoute(
            recipient_work_id=immediate.endpoint_id,
            recipient_grant_version=immediate.generation,
        )
        outcome_envelope = ReviewOutcomeEnvelope(brief=envelope.brief, outcome=outcome)
        outcome_request = MessageSubmitRequest(
                api_version="1",
                message_id=_stable("verdict", request.review_id, delivery.delivery_id),
                grant_version=reviewer.generation, route_ref="review.request", kind="result",
                payload=cast(JsonValue, outcome_envelope.model_dump(mode="json")),
                in_reply_to_delivery_id=delivery.delivery_id,
        )
        async with self.occurrences.engine.begin() as connection:
            submitted = await self.messages.submit_review_internal(
                connection, reviewer, route, outcome_request,
                (reviewer.generation, str(reviewer.generation)),
            )
        if submitted.status != "ok" or submitted.message is None:
            return ReviewResult(status="UNKNOWN", review_id=request.review_id,
                                reason="message_conflict")
        final_delivery = submitted.message
        if immediate.endpoint_id != requester.endpoint_id:
            notification_request = MessageSubmitRequest(
                api_version="1", message_id=_stable("review-outcome", request.review_id),
                grant_version=reviewer.generation, route_ref="review.outcome", kind="request",
                payload=cast(JsonValue, outcome_envelope.model_dump(mode="json")),
            )
            async with self.occurrences.engine.begin() as connection:
                notified = await self.messages.submit_review_internal(
                    connection, reviewer,
                    MessageRoute(
                        recipient_work_id=requester.endpoint_id,
                        recipient_grant_version=requester.generation,
                    ),
                    notification_request,
                )
            if notified.status != "ok" or notified.message is None:
                return ReviewResult(status="UNKNOWN", review_id=request.review_id,
                                    reason="message_conflict")
            final_delivery = notified.message
        return ReviewResult(
            status="SUBMITTED", review_id=request.review_id,
            delivery_id=final_delivery.delivery_id, verdict_digest=verdict_digest,
        )
