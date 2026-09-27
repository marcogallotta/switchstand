"""Journaled ordinary relation mutations over existing provider and grant truth."""

import hashlib
import json
from typing import Protocol, cast
from uuid import UUID

from sqlalchemy.exc import SQLAlchemyError

from .core import Provider, ProviderError, State, UnknownEffect
from .grant_state import EffectRecord, GrantState
from .grants import (
    GuardOutcome,
    PrincipalContext,
    ProtectedRelation,
    RelationReceipt,
    WorkGrant,
)
from .provider import RelationMutation


class RelationProvider(Protocol):
    async def update_relation(
        self, provider_work_id: str, mutation: RelationMutation,
    ) -> None: ...
    async def relation_matches(
        self, provider_work_id: str, mutation: RelationMutation,
    ) -> bool: ...


class RelationGateway:
    def __init__(self, state: State, grants: GrantState, providers: dict[str, Provider]):
        self.state, self.grants, self.providers = state, grants, providers

    @staticmethod
    def fingerprint(principal: PrincipalContext, request: ProtectedRelation) -> str:
        payload = [
            principal.key,
            str(request.work_id),
            request.grant_version,
            request.observed_revision,
            request.change.model_dump(mode="json"),
        ]
        return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()

    @staticmethod
    def guard(
        request: ProtectedRelation, status: str, reason: str, *, possible_send: bool = False,
    ) -> GuardOutcome:
        return GuardOutcome.model_validate({
            "status": status,
            "operation": "work_relate",
            "work_id": request.work_id,
            "operation_id": request.operation_id,
            "reason": reason,
            "effect": "unknown" if possible_send else "not_sent",
            "retry": "reconcile" if possible_send else "refresh" if status == "stale" else "none",
            "next_action": (
                "Retry only this OperationId to reconcile; do not send a new effect."
                if possible_send else "Refresh work/grant or ask the trusted issuer."
            ),
        })

    @staticmethod
    def mutation(request: ProtectedRelation) -> RelationMutation:
        return RelationMutation(**request.change.model_dump())

    async def relate(
        self, principal: PrincipalContext, request: ProtectedRelation,
    ) -> GuardOutcome:
        possible_send = False
        history_known = False
        try:
            async with self.grants.locked(principal.key, request.work_id) as grant:
                fingerprint = self.fingerprint(principal, request)
                record = await self.grants.exact(request.operation_id)
                history_known = True
                if record is not None:
                    possible_send = record.outcome.effect != "not_sent"
                    if record.principal_key != principal.key or record.fingerprint != fingerprint:
                        return self.guard(request, "denied", "operation_identity_conflict")
                    if record.outcome.effect != "unknown":
                        return record.outcome
                    return await self._reconcile(principal, request, record)

                blocked = await self.grants.previous(request.operation_id, request.work_id)
                if blocked is not None:
                    return self.guard(
                        request, "unknown", "target_has_unresolved_effect", possible_send=True
                    )
                if grant is None or grant.principal != principal or not grant.current():
                    return self.guard(request, "denied", "no_current_grant")
                if not grant.can_write(request.work_id) or "work_update" not in grant.operations:
                    return self.guard(request, "denied", "operation_or_work_not_granted")
                if request.grant_version != grant.version:
                    return self.guard(request, "stale", "grant_version_changed")
                qualification = grant.update_qualification
                if (qualification is None
                        or (principal.assurance == "test") != qualification.startswith("test:")):
                    return self.guard(
                        request, "denied", "update_not_qualified_for_this_surface"
                    )
                handle = await self.state.get(request.work_id)
                if handle is None:
                    return self.guard(request, "denied", "work_not_bound")
                provider = self.providers.get(handle.provider)
                if (provider is None or not hasattr(provider, "update_relation")
                        or not hasattr(provider, "relation_matches")):
                    return self.guard(request, "denied", "provider_relation_not_supported")
                current = await provider.get(handle.provider_work_id)
                if current is None or not current.canonical:
                    return self.guard(request, "not_applied", "source_read_unavailable")
                if current.revision != request.observed_revision:
                    return self.guard(request, "stale", "source_revision_changed")

                unknown = self.guard(
                    request, "unknown", "prepared_or_unconfirmed_send", possible_send=True
                )
                await self.grants.prepare({
                    "request": request.model_dump(mode="json"),
                    "provider": handle.provider,
                    "task_gid": handle.provider_work_id,
                    "qualification": qualification,
                }, grant, fingerprint, unknown)
                possible_send = True
                if not grant.current():
                    outcome = self.guard(
                        request, "not_applied", "grant_expired_before_send"
                    )
                else:
                    relation_provider = cast(RelationProvider, provider)
                    try:
                        await relation_provider.update_relation(
                            handle.provider_work_id, self.mutation(request)
                        )
                    except UnknownEffect:
                        outcome = await self._readback(
                            principal, request, grant.id, grant.version,
                            handle.provider, handle.provider_work_id, qualification,
                        )
                    except ProviderError:
                        outcome = self.guard(
                            request, "not_applied", "provider_rejected_send"
                        )
                    else:
                        outcome = await self._readback(
                            principal, request, grant.id, grant.version,
                            handle.provider, handle.provider_work_id, qualification,
                        )
                await self.grants.finish(outcome)
                return outcome
        except (SQLAlchemyError, ProviderError, TypeError, ValueError, KeyError):
            return self.guard(
                request, "unknown", "state_or_effect_unavailable",
                possible_send=possible_send or not history_known,
            )

    async def _reconcile(
        self, principal: PrincipalContext, request: ProtectedRelation, record: EffectRecord,
    ) -> GuardOutcome:
        provider_name = record.intent.get("provider")
        task_gid = record.intent.get("task_gid")
        qualification = record.intent.get("qualification")
        if not all(isinstance(value, str) and value for value in (
            provider_name, task_gid, qualification,
        )):
            raise ValueError("relation durable intent invalid")
        outcome = await self._readback(
            principal, request, record.grant_id, record.grant_version,
            cast(str, provider_name), cast(str, task_gid), cast(str, qualification),
        )
        if outcome.effect == "applied":
            await self.grants.finish(outcome)
        return outcome

    async def _readback(
        self, principal: PrincipalContext, request: ProtectedRelation,
        grant_id: UUID, grant_version: int, provider_name: str, task_gid: str,
        qualification: str,
    ) -> GuardOutcome:

        provider = self.providers.get(provider_name)
        if (provider is None or not hasattr(provider, "relation_matches")):
            return self.guard(
                request, "unknown", "relation_readback_unavailable", possible_send=True
            )
        relation_provider = cast(RelationProvider, provider)
        if not await relation_provider.relation_matches(task_gid, self.mutation(request)):
            return self.guard(
                request, "unknown", "effect_readback_unconfirmed", possible_send=True
            )
        work = await provider.get(task_gid)
        if work is None or not work.canonical:
            return self.guard(
                request, "unknown", "effect_readback_unconfirmed", possible_send=True
            )
        receipt = RelationReceipt(
            operation_id=request.operation_id,
            principal=principal,
            grant_id=grant_id,
            grant_version=grant_version,
            work_id=request.work_id,
            provider=provider_name,
            task_gid=task_gid,
            observed_revision=request.observed_revision,
            resulting_revision=work.revision,
            change=request.change,
            qualification=qualification,
        )
        return GuardOutcome(
            status="ok",
            operation="work_relate",
            work_id=request.work_id,
            operation_id=request.operation_id,
            reason="relation_state_converged",
            effect="applied",
            retry="none",
            next_action="Use the recorded receipt.",
            receipt=receipt,
        )
