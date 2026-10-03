"""Inert DB-only projection for current work reads, search, and scalar updates."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from typing import Protocol, cast
from uuid import UUID

from sqlalchemy import insert, select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from .canonical_relations import (
    CanonicalRelationsRepository,
    WorkRelations,
    project_memberships,
    projects,
    work_parents,
)
from .canonical_work import CanonicalWorkRepository, CurrentWork, canonical_revision
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
    GuardOutcome,
    PrincipalContext,
    ProtectedCreate,
    ProtectedUpdate,
    UpdateReceipt,
)
from .mutation_effect import blocked_effect_next_action

_SCALAR_FIELDS = frozenset({
    "title", "notes", "completed", "priority", "work_type", "lifecycle_state",
    "review_next_action", "wait_kind", "unblock_condition", "next_due",
})


class _TypedUpdateRequest(Protocol):
    patch: WorkPatch


def _routing(work: CurrentWork) -> Routing:
    return Routing(
        priority=work.priority,
        work_type=work.work_type,
        lifecycle_state=work.lifecycle_state,
        review_next_action=work.review_next_action,
        wait_kind=work.wait_kind,
        unblock_condition=work.unblock_condition,
        next_due=work.next_due,
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
        if await self.works.get(work.work_id) != work:
            return None
        return WorkItem(
            id=work.work_id,
            title=work.title,
            notes=work.notes,
            completed=work.completed,
            revision=canonical_revision(work.work_id, work.row_version),
            routing=_routing(work),
            context=_context(work, relations),
        )

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
            page = await self.works.search(
                request.text,
                completed=request.completed,
                cursor=request.cursor,
                limit=request.limit,
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
                ))
            else:
                return WorkSearchResult(
                    status="ok", items=tuple(items), next_cursor=page.next_cursor,
                )
        return WorkSearchResult(status="unknown")

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
            changed = await self.works.replace(replace(current, **values))
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
                    fields = request.patch.model_fields_set
                    if not fields <= _SCALAR_FIELDS:
                        return self._guard(request, "denied", "invalid_database_metadata")
                    values = {field: getattr(request.patch, field) for field in fields}
                    changed = await self.works.replace_locked(
                        connection, replace(current, **values)
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
    ) -> GuardOutcome:
        """Create and journal one canonical work item in one transaction."""
        try:
            work_id = CreateGateway.work_id(request.operation_id)
            fingerprint = CreateGateway.fingerprint(principal, request)
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
                            if (exact["principal_key"] != principal.key
                                    or exact["fingerprint"] != fingerprint):
                                return CreateGateway.guard(
                                    request, "denied", "operation_identity_conflict"
                                )
                            return GuardOutcome.model_validate(exact["outcome"])
                        if grant is None or grant.principal != principal or not grant.current():
                            return CreateGateway.guard(request, "denied", "no_current_grant")
                        if "work_create" not in grant.operations:
                            return CreateGateway.guard(request, "denied", "operation_not_granted")
                        if request.grant_version != grant.version:
                            return CreateGateway.guard(request, "stale", "grant_version_changed")
                        qualification = grant.create_qualification
                        expected_qualification = (
                            "test:" if principal.assurance == "test" else "real:"
                        )
                        if (qualification is None
                                or not qualification.startswith(expected_qualification)):
                            return CreateGateway.guard(
                                request, "denied", "create_not_qualified_for_this_surface"
                            )
                        parent = request.parent_work_id
                        project_id = None
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
                        else:
                            if grant.scope != "workspace":
                                return CreateGateway.guard(
                                    request, "denied", "workspace_create_required"
                                )
                            project_id = await connection.scalar(select(
                                projects.c.project_id
                            ).where(projects.c.asana_project_gid == request.project_gid))
                            if project_id is None:
                                return CreateGateway.guard(
                                    request, "denied", "project_not_admitted"
                                )
                        await self.works.create_locked(
                            connection, CurrentWork(work_id, request.title, False, request.notes)
                        )
                        if parent is not None:
                            await connection.execute(insert(work_parents).values(
                                child_work_id=work_id, parent_work_id=parent
                            ))
                        else:
                            await connection.execute(insert(project_memberships).values(
                                project_id=project_id, work_id=work_id, section_name=None
                            ))
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
                            next_action="Use the recorded create receipt.", receipt=receipt,
                        )
                        await connection.execute(insert(effect_intents).values(
                            operation_id=str(request.operation_id), fingerprint=fingerprint,
                            principal_key=principal.key, work_id=str(work_id),
                            grant_id=str(grant.id), grant_version=grant.version,
                            intent={"request": request.model_dump(mode="json"),
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
