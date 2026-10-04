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
    ProtectedRelation,
    ProtectedUpdate,
    RelationReceipt,
    UpdateReceipt,
)
from .mutation_effect import blocked_effect_next_action
from .relations import RelationGateway
from .state import work_handles
from .work_policy import SEMANTIC_FIELDS, validate_resultant_state

_SCALAR_FIELDS = frozenset({
    "title", "notes", "completed", "priority", "work_type", "lifecycle_state",
    "review_next_action", "canonical_root", "owner_key", "wait_kind",
    "unblock_condition", "next_due", "next_action_class", "next_action_ref",
})


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
                    fields = request.patch.model_fields_set
                    if not fields <= _SCALAR_FIELDS:
                        return self._guard(request, "denied", "invalid_database_metadata")
                    values = {field: getattr(request.patch, field) for field in fields}
                    candidate = replace(current, **values)
                    _validate_semantic_write(candidate, fields)
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
                            inherited = current.canonical_root or "UNKNOWN"
                            if root is not None and root != inherited:
                                return CreateGateway.guard(
                                    request, "denied", "child_root_conflicts_with_parent"
                                )
                            root = inherited
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
                            root = str(work_id) if root is None else root
                        created = CurrentWork(
                            work_id, request.title, False, request.notes,
                            priority=request.priority, work_type=request.work_type,
                            lifecycle_state=request.lifecycle_state, canonical_root=root,
                            owner_key=request.owner_key, wait_kind=request.wait_kind,
                            unblock_condition=request.unblock_condition, next_due=request.next_due,
                            next_action_class=request.next_action_class,
                            next_action_ref=request.next_action_ref,
                        )
                        _validate_semantic_write(created, SEMANTIC_FIELDS)
                        root_id = _exact_root(root)
                        if (root_id is not None
                                and not (parent is None and root_id == work_id)
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
                        else:
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
                        if stored != created or stored_handle != work_id or relation != (
                            parent if parent is not None else project_id
                        ):
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
                            next_action="Use the recorded create receipt.", receipt=receipt,
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
                    if patch.kind not in {"parent", "dependency"}:
                        return RelationGateway.guard(
                            request, "denied", "invalid_database_relation"
                        )
                    target = patch.target_work_id
                    if target is not None and not grant.can_read(target, explicit_target=True):
                        return RelationGateway.guard(
                            request, "denied", "relation_target_not_granted"
                        )
                    if patch.kind == "parent":
                        await self.relations.set_parent(
                            request.work_id, target, current.row_version,
                            connection=connection,
                        )
                    else:
                        assert target is not None
                        await self.relations.change_dependency(
                            request.work_id, target, add=patch.action == "add",
                            observed_version=current.row_version, connection=connection,
                        )
                    receipt = RelationReceipt(
                        operation_id=request.operation_id, principal=principal,
                        grant_id=grant.id, grant_version=grant.version,
                        work_id=request.work_id, provider="postgres",
                        task_gid=str(request.work_id),
                        observed_revision=request.observed_revision,
                        patch=patch, qualification=qualification,
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
