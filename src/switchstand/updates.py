import hashlib
import json
from uuid import UUID

from sqlalchemy.exc import SQLAlchemyError

from .contracts import WorkPatch
from .core import Provider, ProviderError, ProviderWork, State, UnknownEffect, apply_scalar
from .grant_state import EffectRecord, GrantState
from .grants import (
    GuardOutcome,
    PrincipalContext,
    ProtectedUpdate,
    UpdateReceipt,
    WorkGrant,
)


class UpdateGateway:
    def __init__(self, state: State, grants: GrantState, providers: dict[str, Provider]):
        self.state, self.grants, self.providers = state, grants, providers

    @staticmethod
    def fingerprint(principal: PrincipalContext, request: ProtectedUpdate) -> str:
        payload = [principal.key, str(request.work_id), request.grant_version,
                   request.observed_revision, request.patch.model_dump(mode="json", exclude_unset=True)]
        return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
    @staticmethod
    def guard(request: ProtectedUpdate, status: str, reason: str, *, possible_send: bool = False) -> GuardOutcome:
        return GuardOutcome.model_validate({
            "status": status, "operation": "work_update", "work_id": request.work_id,
            "operation_id": request.operation_id, "reason": reason,
            "effect": "unknown" if possible_send else "not_sent",
            "retry": "reconcile" if possible_send else "refresh" if status == "stale" else "none",
            "next_action": "Retry only this OperationId to reconcile; do not send a new effect."
            if possible_send else "Refresh work/grant or ask the trusted issuer.",
        })
    async def update(self, principal: PrincipalContext, request: ProtectedUpdate) -> GuardOutcome:
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
                    return self.guard(request, "unknown", "target_has_unresolved_effect", possible_send=True)
                if (grant is None or grant.principal != principal or not grant.current()):
                    return self.guard(request, "denied", "no_current_grant")
                if not grant.can_write(request.work_id) or "work_update" not in grant.operations:
                    return self.guard(request, "denied", "operation_or_work_not_granted")
                if request.grant_version != grant.version:
                    return self.guard(request, "stale", "grant_version_changed")
                qualification = grant.update_qualification
                if (qualification is None
                        or (principal.assurance == "test") != qualification.startswith("test:")):
                    return self.guard(request, "denied", "update_not_qualified_for_this_surface")
                handle = await self.state.get(request.work_id)
                if handle is None:
                    return self.guard(request, "denied", "work_not_bound")
                provider = self.providers[handle.provider]
                current = await provider.get(handle.provider_work_id)
                if current is None or not current.canonical:
                    return self.guard(request, "not_applied", "source_read_unavailable")
                if current.revision != request.observed_revision:
                    return self.guard(request, "stale", "source_revision_changed")
                unknown = self.guard(request, "unknown", "prepared_or_unconfirmed_send", possible_send=True)
                await self.grants.prepare({
                    "request": request.model_dump(mode="json"), "provider": handle.provider,
                    "task_gid": handle.provider_work_id, "qualification": qualification,
                }, grant, fingerprint, unknown)
                possible_send = True
                if not grant.current():
                    outcome = self.guard(request, "not_applied", "grant_expired_before_send")
                else:
                    outcome = await self._send(principal, request, grant, handle.provider,
                                               handle.provider_work_id, qualification)
                await self.grants.finish(outcome)
                return outcome
        except (SQLAlchemyError, ProviderError, TypeError, ValueError, KeyError):
            return self.guard(request, "unknown", "state_or_effect_unavailable",
                              possible_send=possible_send or not history_known)
    async def _send(self, principal: PrincipalContext, request: ProtectedUpdate, grant: WorkGrant,
                    provider_name: str, task_gid: str, qualification: str) -> GuardOutcome:
        patch = WorkPatch.model_validate(request.patch.model_dump(exclude_unset=True))
        try:
            work = await apply_scalar(self.providers[provider_name], task_gid, patch)
        except UnknownEffect:
            work = await apply_scalar(self.providers[provider_name], task_gid, patch, send=False)
        except ProviderError:
            return self.guard(request, "not_applied", "provider_rejected_send")
        return self._applied(principal, request, grant.id, grant.version, provider_name,
                             task_gid, qualification, work)
    async def _reconcile(self, principal: PrincipalContext, request: ProtectedUpdate,
                         record: EffectRecord) -> GuardOutcome:
        intent = record.intent
        provider_name, task_gid, qualification = (intent.get("provider"), intent.get("task_gid"),
                                                   intent.get("qualification"))
        if not isinstance(provider_name, str) or not provider_name:
            raise ValueError("durable update provider invalid")
        if not isinstance(task_gid, str) or not task_gid:
            raise ValueError("durable update target invalid")
        if not isinstance(qualification, str) or not qualification:
            raise ValueError("durable update intent invalid")
        patch = WorkPatch.model_validate(request.patch.model_dump(exclude_unset=True))
        work = await apply_scalar(self.providers[provider_name], task_gid, patch, send=False)
        outcome = self._applied(principal, request, record.grant_id, record.grant_version,
                                provider_name, task_gid, qualification, work)
        if outcome.effect == "applied":
            await self.grants.finish(outcome)
        return outcome

    def _applied(self, principal: PrincipalContext, request: ProtectedUpdate, grant_id: UUID,
                 grant_version: int, provider_name: str, task_gid: str, qualification: str,
                 work: ProviderWork | None) -> GuardOutcome:
        if work is None:
            return self.guard(request, "unknown", "effect_readback_unconfirmed", possible_send=True)
        receipt = UpdateReceipt(
            operation_id=request.operation_id, principal=principal, grant_id=grant_id,
            grant_version=grant_version, work_id=request.work_id, provider=provider_name,
            task_gid=task_gid, observed_revision=request.observed_revision,
            resulting_revision=work.revision, patch=request.patch, qualification=qualification,
        )
        return GuardOutcome(status="ok", operation="work_update", work_id=request.work_id,
                            operation_id=request.operation_id, reason="scalar_state_converged",
                            effect="applied", retry="none", next_action="Use the recorded receipt.",
                            receipt=receipt)
