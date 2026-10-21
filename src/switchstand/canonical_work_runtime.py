"""Inert DB-only projection for current work reads, search, and scalar updates."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from typing import Protocol, cast
from uuid import UUID

from sqlalchemy import Text, exists, insert, select, update
from sqlalchemy import cast as sql_cast
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncConnection

from . import pull_requests
from .agent_mailboxes import CODEX_COORDINATOR_NAME, AgentMailbox
from .canonical_relations import (
    CanonicalRelationsRepository,
    WorkRelations,
    project_memberships,
    projects,
    work_parents,
)
from .canonical_work import (
    CanonicalWorkRepository,
    CurrentWork,
    canonical_revision,
    canonical_work,
    normalize_title,
)
from .contracts import (
    Routing,
    WorkContext,
    WorkItem,
    WorkPatch,
    WorkPlacement,
    WorkResult,
    WorkSearchItem,
    WorkSearchRequest,
    WorkSearchResult,
    WorkUpdateRequest,
)
from .creates import CreateGateway
from .grant_state import GrantState, effect_intents
from .grants import (
    CreateReceipt,
    EffectBlocker,
    EffectOutcomeView,
    FeedbackClassificationReceipt,
    GuardOutcome,
    PrincipalContext,
    ProtectedCreate,
    ProtectedFeedbackClassification,
    ProtectedRelation,
    ProtectedUpdate,
    RelationReceipt,
    UpdateReceipt,
)
from .human_reviews import (
    HumanReviewRecord,
    agent_notes_fit,
    current_approved_baseline_in_transaction,
)
from .messages import messages
from .mutation_effect import blocked_effect_next_action
from .relations import RelationGateway
from .state import work_handles
from .work_policy import SEMANTIC_FIELDS, validate_create_state, validate_resultant_state

_SCALAR_FIELDS = frozenset({
    "title", "notes", "completed", "priority", "work_type", "lifecycle_state",
    "review_next_action", "canonical_root", "owner_key", "wait_kind",
    "unblock_condition", "next_due", "next_action_class", "next_action_ref",
})

CONFIRM_CANCELS_REVIEW_MESSAGE = (
    "This item is waiting for review {review_id}. This edit will cancel that review. "
    "If you meant it, repeat the same call with confirm_cancels_review=true."
)

_START_HERE_PROVIDER_WORK_ID = "1218327002478382"


async def pending_review_id(
    connection: AsyncConnection, work_id: UUID, revision: str,
) -> UUID | None:
    """Return one deterministic pending review bound to this exact work revision."""
    review_id = sql_cast(canonical_work.c.work_id, Text)
    has_basis = exists(select(1).where(
        messages.c.kind == "request",
        messages.c.route_ref.in_(("review.request", "review.acquisition")),
        messages.c.payload["brief"]["basis"]["review_id"].astext == review_id,
        messages.c.payload["brief"]["basis"]["subject_work_id"].astext
        == str(work_id),
        messages.c.payload["brief"]["basis"]["subject_revision"].astext == revision,
    ))
    has_outcome = exists(select(1).where(
        messages.c.kind == "result",
        messages.c.payload["outcome"]["type"].astext == "REVIEW_OUTCOME",
        messages.c.payload["outcome"]["review_id"].astext == review_id,
        messages.c.payload["brief"]["basis"]["subject_work_id"].astext
        == str(work_id),
        messages.c.payload["brief"]["basis"]["subject_revision"].astext == revision,
    ))
    return await connection.scalar(select(canonical_work.c.work_id).join(
        work_parents,
        work_parents.c.child_work_id == canonical_work.c.work_id,
    ).where(
        work_parents.c.parent_work_id == work_id,
        canonical_work.c.work_type == "REVIEW",
        has_basis,
        ~has_outcome,
    ).order_by(canonical_work.c.work_id).limit(1))


def confirmation_message(review_id: UUID) -> str:
    return CONFIRM_CANCELS_REVIEW_MESSAGE.format(review_id=review_id)


class _TypedUpdateRequest(Protocol):
    patch: WorkPatch


def _routing(work: CurrentWork) -> Routing:
    return Routing(
        priority=work.priority,
        work_type=work.work_type,
        lifecycle_state=work.lifecycle_state,
        review_next_action=work.review_next_action,
        canonical_root=work.canonical_root,
        owner_key=work.owner_key,
        wait_kind=work.wait_kind,
        unblock_condition=work.unblock_condition,
        next_due=work.next_due,
        next_action_class=work.next_action_class,
        next_action_ref=work.next_action_ref,
    )


def _validate_semantic_write(work: CurrentWork, fields: set[str] | frozenset[str]) -> None:
    if fields & SEMANTIC_FIELDS:
        validate_resultant_state(
            lifecycle_state=work.lifecycle_state, canonical_root=work.canonical_root,
            owner_key=work.owner_key, wait_kind=work.wait_kind,
            unblock_condition=work.unblock_condition, next_due=work.next_due,
            next_action_class=work.next_action_class, next_action_ref=work.next_action_ref,
        )


def _exact_root(value: str | None) -> UUID | None:
    return None if value in {None, "NONE", "UNKNOWN"} else UUID(value)


def _start_here_child_shape(request: ProtectedCreate) -> bool:
    """Match the post-MCP form of the documented direct-assignment create."""
    owner = request.owner_key
    return (
        request.canonical_root is None
        and request.work_type == "Task"
        and request.lifecycle_state == "CURRENT"
        and (owner == CODEX_COORDINATOR_NAME or owner.startswith("agent:"))
        and (request.wait_kind, request.unblock_condition, request.next_due)
        == ("NONE", "NONE", "NONE")
        and request.next_action_class == "OWNER_CAN_DO"
        and request.next_action_ref not in {"NONE", "UNKNOWN"}
    )


def _context(work: CurrentWork, relations: WorkRelations) -> WorkContext:
    return WorkContext(
        assignee=work.assignee,
        placements=tuple(
            WorkPlacement(area=value.name, stage=value.section_name)
            for value in relations.placements
        ),
    )


class CanonicalWorkRuntime:
    """Project compact canonical storage through existing public work contracts."""

    def __init__(
        self, works: CanonicalWorkRepository, relations: CanonicalRelationsRepository,
    ):
        self.works = works
        self.relations = relations

    async def _item(self, work: CurrentWork) -> WorkItem | None:
        relations = await self.relations.get(work.work_id)
        approved = await self._task_approval(work.work_id)
        if await self.works.get(work.work_id) != work:
            return None
        return WorkItem(
            id=work.work_id,
            title=work.title,
            notes=work.notes,
            task_text=None if approved is None else approved.consequence.task_text,
            task_approval_id=None if approved is None else approved.consequence_id,
            task_approval_digest=None if approved is None else approved.consequence_digest,
            completed=work.completed,
            revision=canonical_revision(work.work_id, work.row_version),
            routing=_routing(work),
            context=_context(work, relations),
            admitted_at=work.admitted_at,
        )

    async def _task_approval(self, work_id: UUID) -> HumanReviewRecord | None:
        async with self.works.engine.begin() as connection:
            await self.works.get_locked(connection, work_id)
            approved = await current_approved_baseline_in_transaction(connection, work_id)
            return approved if approved is not None and approved.consequence.task_text is not None else None

    async def get(self, work_id: UUID) -> WorkResult:
        for _ in range(2):
            work = await self.works.get(work_id)
            if work is None:
                return WorkResult(status="unknown")
            item = await self._item(work)
            if item is not None:
                return WorkResult(status="ok", item=item)
        return WorkResult(status="unknown")

    async def search(self, request: WorkSearchRequest) -> WorkSearchResult:
        for _ in range(2):
            filters = {
                name: value for name, value in (
                    ("lifecycle_state", request.lifecycle_state),
                    ("owner_key", request.owner_key), ("priority", request.priority),
                    ("work_type", request.work_type),
                    ("canonical_root", request.canonical_root),
                ) if value is not None
            }
            page = await self.works.search(
                request.text,
                completed=request.completed,
                cursor=request.cursor,
                limit=request.limit,
                **filters,
            )
            items: list[WorkSearchItem] = []
            for work in page.items:
                public = await self._item(work)
                if public is None:
                    break
                items.append(WorkSearchItem(
                    id=public.id,
                    title=public.title,
                    completed=public.completed,
                    revision=public.revision,
                    routing=public.routing,
                    context=public.context,
                    admitted_at=public.admitted_at,
                ))
            else:
                return WorkSearchResult(
                    status="ok", items=tuple(items), next_cursor=page.next_cursor,
                )
        return WorkSearchResult(status="unknown")

    async def feedback_candidates(
        self, title: str, source_work_id: UUID, feedback_types: frozenset[str],
    ) -> tuple[WorkSearchItem, ...]:
        tokens = sorted(set(normalize_title(title).split()), key=lambda token: (-len(token), token))[:3]
        found: dict[UUID, WorkSearchItem] = {}
        for token in tokens:
            for work_type in sorted(feedback_types):
                result = await self.search(WorkSearchRequest(
                    api_version="1", text=token, completed=False,
                    work_type=work_type, limit=10,
                ))
                if result.status != "ok":
                    raise SQLAlchemyError("feedback suggestion search unavailable")
                for item in result.items:
                    if item.id != source_work_id and token in normalize_title(item.title).split():
                        found[item.id] = item
        return tuple(sorted(found.values(), key=lambda item: (
            -len(set(normalize_title(title).split()) & set(normalize_title(item.title).split())),
            normalize_title(item.title), str(item.id),
        ))[:10])

    @staticmethod
    def _classification_guard(
        request: ProtectedFeedbackClassification, status: str, reason: str,
        *, possible: bool = False,
    ) -> GuardOutcome:
        return GuardOutcome.model_validate({
            "status": status, "operation": "feedback_classify",
            "work_id": request.source_work_id, "operation_id": request.operation_id,
            "reason": reason, "effect": "unknown" if possible else "not_sent",
            "retry": "reconcile" if possible else "refresh" if status == "stale" else "none",
            "next_action": (
                "Replay this exact classification OperationId."
                if possible else "Refresh the source, target, or reporting grant."
            ),
        })

    async def protected_feedback_classification(
        self, grants: GrantState, principal: PrincipalContext,
        request: ProtectedFeedbackClassification, source_type: str,
        feedback_types: frozenset[str],
    ) -> GuardOutcome:
        request_json = request.model_dump(mode="json")
        fingerprint = hashlib.sha256(json.dumps([principal.key, request_json], sort_keys=True).encode()).hexdigest()
        try:
            async with grants.locked(principal.key) as grant, grants.engine.begin() as connection:
                exact = (await connection.execute(select(effect_intents).where(
                    effect_intents.c.operation_id == str(request.operation_id)
                ))).mappings().one_or_none()
                if exact is not None:
                    if exact["principal_key"] != principal.key or exact["fingerprint"] != fingerprint:
                        return self._classification_guard(request, "denied", "operation_identity_conflict")
                    return GuardOutcome.model_validate(exact["outcome"])
                if (grant is None or grant.principal != principal or not grant.current()
                        or "file_issue" not in grant.operations):
                    return self._classification_guard(request, "denied", "report_not_granted")
                if request.grant_version != grant.version:
                    return self._classification_guard(request, "stale", "grant_version_changed")
                qualification = grant.report_qualification
                expected = "test:" if principal.assurance == "test" else "real:"
                if qualification is None or not qualification.startswith(expected):
                    return self._classification_guard(request, "denied", "report_not_qualified")
                ids = sorted(filter(None, (request.source_work_id, request.target_work_id)), key=str)
                locked = {work_id: await self.works.get_locked(connection, work_id) for work_id in ids}
                source, target = locked.get(request.source_work_id), locked.get(cast(UUID, request.target_work_id))
                if source is None or source.completed or source.work_type != source_type:
                    return self._classification_guard(request, "denied", "invalid_feedback_source")
                if request.observed_source_revision != canonical_revision(source.work_id, source.row_version):
                    return self._classification_guard(request, "stale", "source_revision_changed")
                if request.target_work_id is not None:
                    if target is None or target.completed or target.work_type not in feedback_types:
                        return self._classification_guard(request, "denied", "invalid_feedback_target")
                    if request.observed_target_revision != canonical_revision(target.work_id, target.row_version):
                        return self._classification_guard(request, "stale", "target_revision_changed")
                origin = (await connection.execute(select(effect_intents).where(
                    (effect_intents.c.work_id == str(source.work_id))
                    & (effect_intents.c.principal_key == principal.key) & (effect_intents.c.outcome["receipt"]["qualification"].astext == qualification)
                    & (effect_intents.c.outcome["operation"].astext == "work_create")
                    & (effect_intents.c.intent["request"]["work_type"].astext == source.work_type)
                ).limit(1))).mappings().one_or_none()
                if origin is None:
                    return self._classification_guard(request, "denied", "source_not_filed_here")
                blocked = (await connection.execute(select(effect_intents).where(
                    effect_intents.c.work_id.in_([str(value) for value in ids])
                    & (effect_intents.c.operation_id != str(request.operation_id))
                    & (effect_intents.c.outcome["effect"].astext == "unknown")
                ).limit(1))).mappings().one_or_none()
                if blocked is not None:
                    return self._classification_guard(request, "unknown", "feedback_has_unresolved_effect")
                code = "" if request.code_status is None else (
                    f" Code reconciliation: {request.code_status}; fork {request.fork_runtime_seconds}s; "
                    f"critical wait {request.critical_path_wait_seconds}s."
                )
                ref = "" if target is None else f" {target.work_id}"
                source_note, target_note, close, parent = {
                    "NEW": ("Classification: NEW", None, False, None),
                    "AMEND": (f"Classification: AMEND{ref}", f"Amended from {source.work_id}: {source.notes}", True, None),
                    "DUPLICATE": (f"Classification: DUPLICATE of{ref}", None, True, None),
                    "SUB-ITEM": (f"Classification: SUB-ITEM of{ref}", None, False, request.target_work_id),
                    "RELATED": (f"Classification: RELATED to{ref}", None, False, None),
                }[request.classification]
                source_notes = f"{source.notes.rstrip()}\n\n{source_note}.{code}".strip()
                target_notes = None if target is None or target_note is None else f"{target.notes.rstrip()}\n\n{target_note}".strip()
                if len(source_notes) > 8000 or (target_notes is not None and len(target_notes) > 8000):
                    return self._classification_guard(request, "denied", "classification_notes_overflow")
                if (not await agent_notes_fit(connection, source.work_id, source_notes)
                        or (target is not None and target_notes is not None
                            and not await agent_notes_fit(connection, target.work_id, target_notes))):
                    return self._classification_guard(request, "denied", "agent_notes_overflow")
                if target_notes is not None:
                    target = await self.works.replace_locked(
                        connection, replace(cast(CurrentWork, target), notes=target_notes)
                    )
                source = await self.works.replace_locked(connection, replace(
                    source, notes=source_notes, completed=close,
                    lifecycle_state="TERMINAL" if close else source.lifecycle_state,
                    wait_kind="NONE" if close else source.wait_kind,
                    unblock_condition="NONE" if close else source.unblock_condition,
                    next_due="NONE" if close else source.next_due,
                    next_action_class="NONE" if close else source.next_action_class,
                    next_action_ref="NONE" if close else source.next_action_ref,
                ))
                if parent is not None:
                    version = await self.relations.set_parent(
                        source.work_id, parent, source.row_version, connection=connection,
                    )
                    source = replace(source, row_version=version)
                receipt = FeedbackClassificationReceipt(
                    operation_id=request.operation_id, principal=principal,
                    grant_id=grant.id, grant_version=grant.version, work_id=source.work_id,
                    provider="postgres", task_gid=str(source.work_id),
                    observed_revision=request.observed_source_revision,
                    resulting_revision=canonical_revision(source.work_id, source.row_version),
                    classification=request.classification, target_work_id=request.target_work_id,
                    qualification=qualification,
                )
                outcome = GuardOutcome(
                    status="ok", operation="feedback_classify", work_id=source.work_id,
                    operation_id=request.operation_id, reason="classification_converged",
                    effect="applied", retry="none", next_action="Use the recorded receipt.",
                    receipt=receipt,
                )
                await connection.execute(insert(effect_intents).values(
                    operation_id=str(request.operation_id), fingerprint=fingerprint,
                    principal_key=principal.key, work_id=str(source.work_id),
                    grant_id=str(grant.id), grant_version=grant.version,
                    intent={"request": request_json, "authority": "postgres"},
                    outcome=outcome.model_dump(mode="json", exclude_none=True),
                ))
                return outcome
        except (IntegrityError, TypeError, ValueError, KeyError, LookupError):
            return self._classification_guard(request, "denied", "invalid_classification")
        except SQLAlchemyError:
            return self._classification_guard(
                request, "unknown", "state_or_effect_unavailable", possible=True,
            )

    async def update(self, request: WorkUpdateRequest) -> WorkResult:
        current = await self.works.get(request.work_id)
        current_item = None if current is None else await self._item(current)
        if current is None or current_item is None:
            return WorkResult(status="unknown")
        if request.observed_revision != current_item.revision:
            return WorkResult(status="stale", item=current_item)
        patch: WorkPatch = cast(_TypedUpdateRequest, request).patch  # pyright: ignore[reportUnknownMemberType,reportUnknownVariableType]
        fields = patch.model_fields_set
        if not fields <= _SCALAR_FIELDS:
            return WorkResult(status="denied")
        values = {field: getattr(patch, field) for field in fields}  # pyright: ignore[reportUnknownArgumentType]
        try:
            candidate = replace(current, **values)
            _validate_semantic_write(candidate, fields)
            root_id = _exact_root(candidate.canonical_root)
            if root_id is not None and await self.works.get(root_id) is None:
                return WorkResult(status="denied")
            changed = await self.works.replace(candidate)
        except ValueError as error:
            if "stale canonical work version" not in str(error):
                return WorkResult(status="denied")
            latest = await self.works.get(request.work_id)
            latest_item = None if latest is None else await self._item(latest)
            return (
                WorkResult(status="unknown") if latest_item is None
                else WorkResult(status="stale", item=latest_item)
            )
        changed_item = await self._item(changed)
        if changed_item is None:
            return WorkResult(status="unknown")
        return WorkResult(status="ok", item=changed_item)

    @staticmethod
    def _guard(
        request: ProtectedUpdate, status: str, reason: str, *, possible: bool = False,
    ) -> GuardOutcome:
        return GuardOutcome.model_validate({
            "status": status, "operation": "work_update", "work_id": request.work_id,
            "operation_id": request.operation_id, "reason": reason,
            "effect": "unknown" if possible else "not_sent",
            "retry": "reconcile" if possible else "refresh" if status == "stale" else "none",
            "next_action": (
                "Retry this exact work_update OperationId; do not start a new effect."
                if possible else "Refresh work/grant or ask the trusted issuer."
            ),
        })

    @staticmethod
    def _fingerprint(principal: PrincipalContext, request: ProtectedUpdate) -> str:
        payload = [principal.key, str(request.work_id), request.grant_version,
                   request.observed_revision,
                   request.patch.model_dump(mode="json", exclude_unset=True)]
        return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()

    async def protected_update(
        self, grants: GrantState, principal: PrincipalContext, request: ProtectedUpdate,
    ) -> GuardOutcome:
        """Apply and journal one canonical scalar update in one transaction."""
        fingerprint = self._fingerprint(principal, request)
        for attempt in range(2):
            try:
                async with (
                    grants.locked(principal.key) as grant,
                    grants.engine.begin() as connection,
                ):
                    current = await self.works.get_locked(connection, request.work_id)
                    exact = (await connection.execute(select(effect_intents).where(
                        effect_intents.c.operation_id == str(request.operation_id)
                    ))).mappings().one_or_none()
                    if exact is not None:
                        if (exact["principal_key"] != principal.key
                                or exact["fingerprint"] != fingerprint):
                            return self._guard(request, "denied", "operation_identity_conflict")
                        return GuardOutcome.model_validate(exact["outcome"])
                    if grant is None or grant.principal != principal or not grant.current():
                        return self._guard(request, "denied", "no_current_grant")
                    if (not grant.can_write(request.work_id)
                            or "work_update" not in grant.operations):
                        return self._guard(
                            request, "denied", "operation_or_work_not_granted"
                        )
                    if request.grant_version != grant.version:
                        return self._guard(request, "stale", "grant_version_changed")
                    qualification = grant.update_qualification
                    if (qualification is None or (principal.assurance == "test")
                            != qualification.startswith("test:")):
                        return self._guard(
                            request, "denied", "update_not_qualified_for_this_surface"
                        )
                    blocked = (await connection.execute(select(effect_intents).where(
                        (effect_intents.c.work_id == str(request.work_id))
                        & (effect_intents.c.outcome["effect"].astext == "unknown")
                    ).limit(1))).mappings().one_or_none()
                    if blocked is not None:
                        prior = GuardOutcome.model_validate(blocked["outcome"])
                        assert prior.operation_id is not None and prior.work_id is not None
                        return self._guard(
                            request, "unknown", "target_has_unresolved_effect"
                        ).model_copy(update={
                            "next_action": blocked_effect_next_action(prior.operation),
                            "blocked_by": EffectBlocker(
                                operation=prior.operation, operation_id=prior.operation_id,
                                work_id=prior.work_id, outcome=EffectOutcomeView(
                                    status=prior.status, reason=prior.reason,
                                    effect=prior.effect, retry=prior.retry,
                                ),
                            ),
                        })
                    if current is None:
                        return self._guard(request, "denied", "work_not_bound")
                    if request.observed_revision != canonical_revision(
                        request.work_id, current.row_version
                    ):
                        return self._guard(request, "stale", "source_revision_changed")
                    if not request.confirm_cancels_review:
                        review_id = await pending_review_id(
                            connection, request.work_id, request.observed_revision,
                        )
                        if review_id is not None:
                            return self._guard(
                                request, "CONFIRM_REQUIRED", "pending_review",
                            ).model_copy(update={
                                "next_action": confirmation_message(review_id),
                            })
                    fields = request.patch.model_fields_set
                    if not fields <= _SCALAR_FIELDS:
                        return self._guard(request, "denied", "invalid_database_metadata")
                    values = {field: getattr(request.patch, field) for field in fields}
                    candidate = replace(current, **values)
                    if not await agent_notes_fit(connection, candidate.work_id, candidate.notes):
                        return self._guard(request, "denied", "agent_notes_overflow")
                    try:
                        _validate_semantic_write(candidate, fields)
                    except ValueError:
                        return self._guard(request, "denied", "invalid_resultant_state")
                    root_id = _exact_root(candidate.canonical_root)
                    if root_id is not None and await self.works.get_locked(
                        connection, root_id
                    ) is None:
                        return self._guard(request, "denied", "canonical_root_not_bound")
                    changed = await self.works.replace_locked(
                        connection, candidate
                    )
                    receipt = UpdateReceipt(
                        operation_id=request.operation_id, principal=principal,
                        grant_id=grant.id, grant_version=grant.version,
                        work_id=request.work_id, provider="postgres",
                        task_gid=str(request.work_id),
                        observed_revision=request.observed_revision,
                        resulting_revision=canonical_revision(
                            request.work_id, changed.row_version
                        ), patch=request.patch, qualification=qualification,
                    )
                    outcome = GuardOutcome(
                        status="ok", operation="work_update", work_id=request.work_id,
                        operation_id=request.operation_id, reason="scalar_state_converged",
                        effect="applied", retry="none",
                        next_action="Use the recorded receipt.", receipt=receipt,
                    )
                    await connection.execute(insert(effect_intents).values(
                        operation_id=str(request.operation_id), fingerprint=fingerprint,
                        principal_key=principal.key, work_id=str(request.work_id),
                        grant_id=str(grant.id), grant_version=grant.version,
                        intent={"request": request.model_dump(mode="json"),
                                "authority": "postgres"},
                        outcome=outcome.model_dump(mode="json", exclude_none=True),
                    ))
                    return outcome
            except IntegrityError:
                if attempt == 0:
                    continue
                return self._guard(request, "denied", "invalid_database_metadata")
            except (TypeError, ValueError, KeyError):
                return self._guard(request, "denied", "invalid_database_metadata")
            except SQLAlchemyError:
                return self._guard(
                    request, "unknown", "state_or_effect_unavailable", possible=True
                )
        raise AssertionError("bounded retry exhausted")

    async def protected_create(
        self, grants: GrantState, principal: PrincipalContext, request: ProtectedCreate,
        *, required_operation: str = "work_create", qualification_field: str = "create_qualification",
    ) -> GuardOutcome:
        """Create and journal one canonical work item in one transaction."""
        try:
            work_id = CreateGateway.work_id(request.operation_id)
            request_json = request.model_dump(mode="json")
            for attempt in range(2):
                try:
                    async with (
                        grants.locked(principal.key) as grant,
                        grants.engine.begin() as connection,
                    ):
                        exact = (await connection.execute(select(effect_intents).where(
                            effect_intents.c.operation_id == str(request.operation_id)
                        ))).mappings().one_or_none()
                        if exact is not None:
                            prior_request = cast(dict[str, object], exact["intent"]).get("request")
                            normalized_prior = ProtectedCreate.model_validate(
                                prior_request
                            ).model_dump(mode="json")
                            if exact["principal_key"] != principal.key or normalized_prior != request_json:
                                return CreateGateway.guard(
                                    request, "denied", "operation_identity_conflict"
                                )
                            return GuardOutcome.model_validate(exact["outcome"])
                        if grant is None or grant.principal != principal or not grant.current():
                            return CreateGateway.guard(request, "denied", "no_current_grant")
                        if required_operation not in grant.operations:
                            return CreateGateway.guard(request, "denied", "operation_not_granted")
                        if request.grant_version != grant.version:
                            return CreateGateway.guard(request, "stale", "grant_version_changed")
                        qualification = getattr(grant, qualification_field)
                        expected_qualification = (
                            "test:" if principal.assurance == "test" else "real:"
                        )
                        if (qualification is None
                                or not qualification.startswith(expected_qualification)):
                            return CreateGateway.guard(
                                request, "denied", "create_not_qualified_for_this_surface"
                            )
                        parent = request.parent_work_id
                        project_id = request.project_id
                        root = request.canonical_root
                        if parent is not None:
                            if not grant.can_write(parent):
                                return CreateGateway.guard(
                                    request, "denied", "parent_not_granted"
                                )
                            current = await self.works.get_locked(connection, parent)
                            if current is None or current.completed:
                                return CreateGateway.guard(
                                    request, "denied", "parent_not_writable"
                                )
                            inherited = current.canonical_root
                            try:
                                inherited_root = _exact_root(inherited)
                            except ValueError:
                                return CreateGateway.guard(
                                    request, "denied", "parent_root_not_exact"
                                ).model_copy(update={"next_action": (
                                    "Omit parent_work_id to capture independently; retain the intended relation in notes."
                                )})
                            if inherited_root is not None and inherited != str(inherited_root):
                                return CreateGateway.guard(
                                    request, "denied", "parent_root_not_exact"
                                ).model_copy(update={"next_action": (
                                    "Omit parent_work_id to capture independently; retain the intended relation in notes."
                                )})
                            if inherited_root is None:
                                start_here = await connection.scalar(select(
                                    work_handles.c.id
                                ).where(
                                    work_handles.c.id == parent,
                                    work_handles.c.provider == "asana",
                                    work_handles.c.provider_work_id
                                    == _START_HERE_PROVIDER_WORK_ID,
                                ))
                                if (
                                    inherited is not None
                                    or current.work_type != "Context"
                                    or start_here != parent
                                    or not _start_here_child_shape(request)
                                ):
                                    return CreateGateway.guard(
                                        request, "denied", "parent_root_not_exact"
                                    ).model_copy(update={"next_action": (
                                        "Omit parent_work_id to capture independently; retain the intended relation in notes."
                                    )})
                                # The sanctioned imported START HERE route is placement-only;
                                # its direct-assignment child starts an exact canonical tree.
                                root = str(work_id)
                            elif root is not None and root != inherited:
                                return CreateGateway.guard(
                                    request, "denied", "child_root_conflicts_with_parent"
                                )
                            else:
                                root = inherited
                        else:
                            if grant.scope != "workspace" and required_operation != "file_issue":
                                return CreateGateway.guard(
                                    request, "denied", "workspace_create_required"
                                )
                            if project_id is None and request.project_gid is not None:
                                project_id = await connection.scalar(select(
                                    projects.c.project_id
                                ).where(projects.c.asana_project_gid == request.project_gid))
                            if project_id is None and (
                                not request.is_capture or request.project_gid is not None
                            ):
                                return CreateGateway.guard(
                                    request, "denied", "project_not_admitted"
                                )
                            expected_root = str(work_id)
                            if root is not None and root != expected_root:
                                return CreateGateway.guard(
                                    request, "denied", "root_must_be_self"
                                )
                            root = expected_root
                        created = CurrentWork(
                            work_id, request.title, False, request.notes,
                            priority=request.priority, work_type=request.work_type,
                            lifecycle_state=request.lifecycle_state, canonical_root=root,
                            owner_key=request.owner_key, wait_kind=request.wait_kind,
                            unblock_condition=request.unblock_condition, next_due=request.next_due,
                            next_action_class=request.next_action_class,
                            next_action_ref=request.next_action_ref,
                        )
                        if not await agent_notes_fit(connection, work_id, created.notes):
                            return CreateGateway.guard(request, "denied", "agent_notes_overflow")
                        try:
                            validate_create_state(
                                parented=parent is not None,
                                work_type=created.work_type,
                                lifecycle_state=created.lifecycle_state,
                                canonical_root=created.canonical_root,
                                owner_key=created.owner_key,
                                wait_kind=created.wait_kind,
                                unblock_condition=created.unblock_condition,
                                next_due=created.next_due,
                                next_action_class=created.next_action_class,
                                next_action_ref=created.next_action_ref,
                            )
                        except ValueError:
                            return CreateGateway.guard(
                                request, "denied", "invalid_resultant_state"
                            )
                        root_id = _exact_root(root)
                        if (root_id is not None
                                and root_id != work_id
                                and await self.works.get_locked(connection, root_id) is None):
                            return CreateGateway.guard(
                                request, "denied", "canonical_root_not_bound"
                            )
                        resolved = {field: getattr(created, field) for field in SEMANTIC_FIELDS}
                        fingerprint = hashlib.sha256(json.dumps(
                            [principal.key, request_json, resolved], sort_keys=True
                        ).encode()).hexdigest()
                        await connection.execute(insert(work_handles).values(
                            id=work_id, provider="postgres", provider_work_id=str(work_id),
                        ))
                        await self.works.create_locked(
                            connection, created
                        )
                        if parent is not None:
                            await connection.execute(insert(work_parents).values(
                                child_work_id=work_id, parent_work_id=parent
                            ))
                        elif project_id is not None:
                            await connection.execute(insert(project_memberships).values(
                                project_id=project_id, work_id=work_id, section_name=None
                            ))
                        stored = await self.works.get_locked(connection, work_id)
                        stored_handle = await connection.scalar(select(work_handles.c.id).where(
                            work_handles.c.id == work_id
                        ))
                        relation = await connection.scalar(
                            select(work_parents.c.parent_work_id).where(
                                work_parents.c.child_work_id == work_id
                            ) if parent is not None else select(
                                project_memberships.c.project_id
                            ).where(project_memberships.c.work_id == work_id)
                        )
                        if (stored is None or stored.admitted_at is None
                                or replace(stored, admitted_at=None) != created
                                or stored_handle != work_id or relation != (
                            parent if parent is not None else project_id
                        )):
                            raise ValueError("canonical create readback mismatch")
                        receipt = CreateReceipt(
                            operation_id=request.operation_id, principal=principal,
                            grant_id=grant.id, grant_version=grant.version, work_id=work_id,
                            provider="postgres", task_gid=str(work_id),
                            parent_task_gid=None if parent is None else str(parent),
                            project_gid=request.project_gid, title=request.title,
                            qualification=qualification,
                        )
                        outcome = GuardOutcome(
                            status="ok", operation="work_create", work_id=work_id,
                            operation_id=request.operation_id, reason="exact_create_verified",
                            effect="applied", retry="none",
                            next_action=(
                                "Captured unassigned work; this is not an executable assignment."
                                if request.is_capture else "Use the recorded create receipt."
                            ), receipt=receipt,
                        )
                        await connection.execute(insert(effect_intents).values(
                            operation_id=str(request.operation_id), fingerprint=fingerprint,
                            principal_key=principal.key, work_id=str(work_id),
                            grant_id=str(grant.id), grant_version=grant.version,
                            intent={"request": request_json, "resolved_state": resolved,
                                    "authority": "postgres"},
                            outcome=outcome.model_dump(mode="json", exclude_none=True),
                        ))
                        return outcome
                except IntegrityError:
                    if attempt == 0:
                        continue
                    return CreateGateway.guard(request, "denied", "invalid_database_create")
            raise AssertionError("bounded retry exhausted")
        except (TypeError, ValueError, KeyError):
            return CreateGateway.guard(request, "denied", "invalid_database_create")
        except SQLAlchemyError:
            return CreateGateway.guard(
                request, "unknown", "state_or_effect_unavailable", possible_send=True
            )

    async def protected_relation(
        self, grants: GrantState, principal: PrincipalContext, request: ProtectedRelation,
        *, caller: AgentMailbox | None = None,
    ) -> GuardOutcome:
        """Apply and journal one canonical parent or dependency change atomically."""
        fingerprint = RelationGateway.fingerprint(principal, request)
        for attempt in range(2):
            try:
                async with (
                    grants.locked(principal.key) as grant,
                    grants.engine.begin() as connection,
                ):
                    current = await self.works.get_locked(connection, request.work_id)
                    exact = (await connection.execute(select(effect_intents).where(
                        effect_intents.c.operation_id == str(request.operation_id)
                    ))).mappings().one_or_none()
                    if exact is not None:
                        if (exact["principal_key"] != principal.key
                                or exact["fingerprint"] != fingerprint):
                            return RelationGateway.guard(
                                request, "denied", "operation_identity_conflict"
                            )
                        return GuardOutcome.model_validate(exact["outcome"])
                    if grant is None or grant.principal != principal or not grant.current():
                        return RelationGateway.guard(request, "denied", "no_current_grant")
                    if (not grant.can_write(request.work_id)
                            or "work_relate" not in grant.operations):
                        return RelationGateway.guard(
                            request, "denied", "operation_or_work_not_granted"
                        )
                    if request.grant_version != grant.version:
                        return RelationGateway.guard(
                            request, "stale", "grant_version_changed"
                        )
                    qualification = grant.relation_qualification
                    expected = "test:" if principal.assurance == "test" else "real:"
                    if qualification is None or not qualification.startswith(expected):
                        return RelationGateway.guard(
                            request, "denied", "relation_not_qualified_for_this_surface"
                        )
                    blocked = (await connection.execute(select(effect_intents).where(
                        (effect_intents.c.work_id == str(request.work_id))
                        & (effect_intents.c.outcome["effect"].astext == "unknown")
                    ).limit(1))).mappings().one_or_none()
                    if blocked is not None:
                        prior = GuardOutcome.model_validate(blocked["outcome"])
                        assert prior.operation_id is not None and prior.work_id is not None
                        return RelationGateway.guard(
                            request, "unknown", "target_has_unresolved_effect"
                        ).model_copy(update={
                            "next_action": blocked_effect_next_action(prior.operation),
                            "blocked_by": EffectBlocker(
                                operation=prior.operation, operation_id=prior.operation_id,
                                work_id=prior.work_id, outcome=EffectOutcomeView(
                                    status=prior.status, reason=prior.reason,
                                    effect=prior.effect, retry=prior.retry,
                                ),
                            ),
                        })
                    if current is None:
                        return RelationGateway.guard(request, "denied", "work_not_bound")
                    if request.observed_revision != canonical_revision(
                        request.work_id, current.row_version
                    ):
                        return RelationGateway.guard(
                            request, "stale", "source_revision_changed"
                        )
                    patch = request.patch
                    if patch.kind not in {"parent", "dependency", "placement", "pull_request"}:
                        return RelationGateway.guard(
                            request, "denied", "invalid_database_relation"
                        )
                    target = patch.target_work_id
                    if target is not None and not grant.can_read(target, explicit_target=True):
                        return RelationGateway.guard(
                            request, "denied", "relation_target_not_granted"
                        )
                    pr_record = None
                    if patch.kind == "pull_request":
                        if caller is None or patch.pull_request is None:
                            return RelationGateway.guard(request, "denied", "agent_not_registered")
                        pr_record = await pull_requests.change(
                            connection, request.work_id, patch.action, patch.pull_request,
                            caller, principal.key, request.operation_id,
                        )
                        await connection.execute(update(canonical_work).where(
                            canonical_work.c.work_id == request.work_id
                        ).values(row_version=current.row_version + 1))
                    elif patch.kind == "parent":
                        await self.relations.set_parent(
                            request.work_id, target, current.row_version,
                            connection=connection,
                        )
                    elif patch.kind == "dependency":
                        assert target is not None
                        await self.relations.change_dependency(
                            request.work_id, target, add=patch.action == "add",
                            observed_version=current.row_version, connection=connection,
                        )
                    else:
                        assert patch.project_id is not None
                        if await connection.scalar(select(projects.c.project_id).where(
                            projects.c.project_id == patch.project_id
                        )) is None:
                            return RelationGateway.guard(
                                request, "denied", "project_not_admitted"
                            )
                        await self.relations.change_project_membership(
                            request.work_id, patch.project_id,
                            add=patch.action == "add",
                            observed_version=current.row_version, connection=connection,
                        )
                    receipt = RelationReceipt(
                        operation_id=request.operation_id, principal=principal,
                        grant_id=grant.id, grant_version=grant.version,
                        work_id=request.work_id, provider="postgres",
                        task_gid=str(request.work_id),
                        observed_revision=request.observed_revision,
                        patch=patch, qualification=qualification, pull_request=pr_record,
                    )
                    outcome = GuardOutcome(
                        status="ok", operation="work_relate", work_id=request.work_id,
                        operation_id=request.operation_id, reason="relation_state_converged",
                        effect="applied", retry="none",
                        next_action="Use the recorded receipt.", receipt=receipt,
                    )
                    await connection.execute(insert(effect_intents).values(
                        operation_id=str(request.operation_id), fingerprint=fingerprint,
                        principal_key=principal.key, work_id=str(request.work_id),
                        grant_id=str(grant.id), grant_version=grant.version,
                        intent={"request": request.model_dump(mode="json"),
                                "authority": "postgres"},
                        outcome=outcome.model_dump(mode="json", exclude_none=True),
                    ))
                    return outcome
            except pull_requests.StaleVersion:
                return RelationGateway.guard(request, "stale", "pr_revision_changed")
            except IntegrityError:
                if attempt == 0:
                    continue
                return RelationGateway.guard(
                    request, "denied", "invalid_database_relation"
                )
            except (LookupError, TypeError, ValueError, KeyError):
                return RelationGateway.guard(request, "denied", "invalid_database_relation")
            except SQLAlchemyError:
                return RelationGateway.guard(
                    request, "unknown", "state_or_effect_unavailable", possible_send=True
                )
        raise AssertionError("bounded retry exhausted")
