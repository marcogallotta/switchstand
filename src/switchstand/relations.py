"""Durable bounded relational task mutations using the existing effect journal."""

import hashlib
import json
from typing import Protocol, cast
from uuid import UUID

from .core import (
    Provider,
    ProviderError,
    ProviderRelation,
    ProviderWork,
    State,
    UnknownEffect,
    observed_revision_matches,
    provider_rejection_reason,
)
from .grant_state import EffectRecord, GrantState
from .grants import (
    GuardOutcome,
    PrincipalContext,
    ProtectedRelation,
    RelationReceipt,
    WorkGrant,
)
from .mutation_effect import PreparedMutation, run_update_or_relation
from .work_index import WorkIndex
from .work_metadata import authority_generation


class RelationProvider(Protocol):
    async def update_relation(self, provider_work_id: str, patch: ProviderRelation) -> None: ...
    async def relation_matches(self, provider_work_id: str, patch: ProviderRelation) -> bool: ...


class Stage2State(Protocol):
    work_index: WorkIndex


class RelationGateway:
    def __init__(self, state: State, grants: GrantState, providers: dict[str, Provider]):
        self.state, self.grants, self.providers = state, grants, providers

    @staticmethod
    def fingerprint(principal: PrincipalContext, request: ProtectedRelation) -> str:
        payload = [
            principal.key, str(request.work_id), request.grant_version,
            request.observed_revision, request.patch.model_dump(mode="json"),
        ]
        return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()

    @staticmethod
    def guard(
        request: ProtectedRelation, status: str, reason: str, *, possible_send: bool = False,
    ) -> GuardOutcome:
        return GuardOutcome.model_validate({
            "status": status, "operation": "work_relate", "work_id": request.work_id,
            "operation_id": request.operation_id, "reason": reason,
            "effect": "unknown" if possible_send else "not_sent",
            "retry": "reconcile" if possible_send else "refresh" if status == "stale" else "none",
            "next_action": (
                "Retry only this OperationId to reconcile; do not send a new relation effect."
                if possible_send else "Refresh work/admission or ask the trusted issuer."
            ),
        })

    async def _resolved(
        self, grant: WorkGrant, provider_name: str, request: ProtectedRelation,
    ) -> ProviderRelation | None:
        patch = request.patch
        target_gid: str | None = None
        if patch.target_work_id is not None:
            if not grant.can_read(patch.target_work_id, explicit_target=True):
                return None
            handle = await self.state.get(patch.target_work_id)
            if handle is None or handle.provider != provider_name:
                return None
            target_gid = handle.provider_work_id
        return ProviderRelation(
            kind=patch.kind, action=patch.action, target_gid=target_gid,
            assignee_gid=patch.assignee_gid, project_gid=patch.project_gid,
            section_gid=patch.section_gid,
        )

    async def update(
        self, principal: PrincipalContext, request: ProtectedRelation,
    ) -> GuardOutcome:
        return await run_update_or_relation(
            self.grants, principal, request, self.fingerprint(principal, request),
            "work_relate", "relation_qualification",
            "relation_not_qualified_for_this_surface",
            lambda status, reason, possible: self.guard(
                request, status, reason, possible_send=possible
            ),
            lambda grant, qualification: self._prepare(
                principal, request, grant, qualification
            ),
            lambda record: self._reconcile(principal, request, record),
        )

    async def _prepare(
        self, principal: PrincipalContext, request: ProtectedRelation,
        grant: WorkGrant, qualification: str,
    ) -> PreparedMutation | GuardOutcome:
        handle = await self.state.get(request.work_id)
        if handle is None:
            return self.guard(request, "denied", "work_not_bound")
        provider = self.providers.get(handle.provider)
        index = getattr(self.state, "work_index", None)
        postgres_dependency = (
            request.patch.kind == "dependency" and index is not None
            and await authority_generation(index.engine) is not None
        )
        if (
            provider is None
            or (not postgres_dependency and (
                not hasattr(provider, "update_relation")
                or not hasattr(provider, "relation_matches")
            ))
        ):
            return self.guard(request, "denied", "provider_relation_not_supported")
        current = await provider.get(handle.provider_work_id)
        if current is None or not current.canonical:
            return self.guard(request, "not_applied", "source_read_unavailable")
        if not await observed_revision_matches(
            self.state, request.work_id, request.observed_revision, current.revision,
            provider_notes=current.notes, provider_context=current.context,
        ):
            return self.guard(request, "stale", "source_revision_changed")
        resolved = await self._resolved(grant, handle.provider, request)
        if resolved is None:
            return self.guard(request, "denied", "relation_target_not_granted")
        if postgres_dependency:
            if request.patch.target_work_id is None or request.patch.action not in {"add", "remove"}:
                return self.guard(request, "denied", "invalid_database_dependency")
            return PreparedMutation(
                intent={
                    "request": request.model_dump(mode="json"),
                    "authority": "postgres",
                    "provider": handle.provider,
                    "task_gid": handle.provider_work_id,
                    "qualification": qualification,
                },
                send=lambda: self._send_database_dependency(
                    principal, request, grant, handle.provider,
                    handle.provider_work_id, qualification, current,
                ),
            )
        return PreparedMutation(
            intent={
                "request": request.model_dump(mode="json"),
                "provider": handle.provider,
                "task_gid": handle.provider_work_id,
                "qualification": qualification,
                "resolved": {
                    "kind": resolved.kind, "action": resolved.action,
                    "target_gid": resolved.target_gid,
                    "assignee_gid": resolved.assignee_gid,
                    "project_gid": resolved.project_gid,
                    "section_gid": resolved.section_gid,
                },
            },
            send=lambda: self._send(
                principal, request, grant, handle.provider,
                handle.provider_work_id, qualification, resolved,
                cast(RelationProvider, provider),
            ),
        )

    async def _send_database_dependency(
        self, principal: PrincipalContext, request: ProtectedRelation, grant: WorkGrant,
        provider_name: str, task_gid: str, qualification: str, current: ProviderWork,
    ) -> GuardOutcome:
        if request.patch.target_work_id is None:
            return self.guard(request, "denied", "invalid_database_dependency")
        index = cast(Stage2State, self.state).work_index
        _revision, applied = await index.update_dependency(
            request.work_id, request.patch.target_work_id,
            request.observed_revision, current, add=request.patch.action == "add",
        )
        if not applied:
            return self.guard(request, "stale", "source_revision_changed")
        return self._applied(
            principal, request, grant.id, grant.version,
            provider_name, task_gid, qualification,
        )

    async def _send(
        self, principal: PrincipalContext, request: ProtectedRelation, grant: WorkGrant,
        provider_name: str, task_gid: str, qualification: str, resolved: ProviderRelation,
        provider: RelationProvider,
    ) -> GuardOutcome:
        try:
            await provider.update_relation(task_gid, resolved)
        except UnknownEffect:
            pass
        except ProviderError as error:
            return self.guard(request, "not_applied", provider_rejection_reason(error))
        if not await provider.relation_matches(task_gid, resolved):
            return self.guard(
                request, "unknown", "effect_readback_unconfirmed", possible_send=True
            )
        return self._applied(
            principal, request, grant.id, grant.version,
            provider_name, task_gid, qualification,
        )

    async def _reconcile(
        self, principal: PrincipalContext, request: ProtectedRelation, record: EffectRecord,
    ) -> GuardOutcome:
        provider_name = record.intent.get("provider")
        task_gid = record.intent.get("task_gid")
        qualification = record.intent.get("qualification")
        raw = record.intent.get("resolved")
        if record.intent.get("authority") == "postgres":
            if request.patch.target_work_id is None:
                raise TypeError("durable database dependency intent invalid")
            index = getattr(self.state, "work_index", None)
            if index is None or not await index.dependency_matches(
                request.work_id, request.patch.target_work_id,
                add=request.patch.action == "add",
            ):
                return self.guard(
                    request, "unknown", "effect_readback_unconfirmed", possible_send=True
                )
            if not isinstance(provider_name, str) or not isinstance(task_gid, str) \
                    or not isinstance(qualification, str):
                raise TypeError("durable database dependency intent invalid")
            outcome = self._applied(
                principal, request, record.grant_id, record.grant_version,
                provider_name, task_gid, qualification,
            )
            await self.grants.finish(outcome)
            return outcome
        if (
            not isinstance(provider_name, str) or not isinstance(task_gid, str)
            or not isinstance(qualification, str) or not isinstance(raw, dict)
        ):
            raise TypeError("durable relation intent invalid")
        values = cast(dict[object, object], raw)
        kind, action = values.get("kind"), values.get("action")
        optional = {
            name: values.get(name)
            for name in ("target_gid", "assignee_gid", "project_gid", "section_gid")
        }
        if (
            not isinstance(kind, str) or not isinstance(action, str)
            or any(value is not None and not isinstance(value, str) for value in optional.values())
        ):
            raise TypeError("durable relation intent invalid")
        resolved = ProviderRelation(
            kind=kind, action=action,
            target_gid=cast(str | None, optional["target_gid"]),
            assignee_gid=cast(str | None, optional["assignee_gid"]),
            project_gid=cast(str | None, optional["project_gid"]),
            section_gid=cast(str | None, optional["section_gid"]),
        )
        provider = self.providers.get(provider_name)
        if (
            provider is None
            or not hasattr(provider, "update_relation")
            or not hasattr(provider, "relation_matches")
        ):
            return self.guard(
                request, "unknown", "relation_recovery_unavailable", possible_send=True
            )
        relation_provider = cast(RelationProvider, provider)
        matches = await relation_provider.relation_matches(task_gid, resolved)
        if not matches and resolved.kind == "placement" and resolved.action == "move":
            try:
                await relation_provider.update_relation(task_gid, resolved)
            except (UnknownEffect, ProviderError):
                pass
            matches = await relation_provider.relation_matches(task_gid, resolved)
        if not matches:
            return self.guard(
                request, "unknown", "effect_readback_unconfirmed", possible_send=True
            )
        outcome = self._applied(
            principal, request, record.grant_id, record.grant_version,
            provider_name, task_gid, qualification,
        )
        await self.grants.finish(outcome)
        return outcome

    @staticmethod
    def _applied(
        principal: PrincipalContext, request: ProtectedRelation,
        grant_id: UUID, grant_version: int, provider_name: str, task_gid: str,
        qualification: str,
    ) -> GuardOutcome:
        receipt = RelationReceipt(
            operation_id=request.operation_id, principal=principal, grant_id=grant_id,
            grant_version=grant_version, work_id=request.work_id, provider=provider_name,
            task_gid=task_gid, observed_revision=request.observed_revision,
            patch=request.patch, qualification=qualification,
        )
        return GuardOutcome(
            status="ok", operation="work_relate", work_id=request.work_id,
            operation_id=request.operation_id, reason="relation_state_converged",
            effect="applied", retry="none", next_action="Use the recorded receipt.",
            receipt=receipt,
        )
