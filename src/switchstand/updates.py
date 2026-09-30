import hashlib
import json
from uuid import UUID

from .contracts import WorkPatch
from .core import (
    Provider,
    ProviderError,
    ProviderWork,
    State,
    UnknownEffect,
    apply_scalar,
    provider_rejection_reason,
)
from .grant_state import EffectRecord, GrantState
from .grants import (
    GuardOutcome,
    PrincipalContext,
    ProtectedUpdate,
    UpdateReceipt,
    WorkGrant,
)
from .mutation_effect import PreparedMutation, run_update_or_relation


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
        return await run_update_or_relation(
            self.grants, principal, request, self.fingerprint(principal, request),
            "work_update", "update_qualification", "update_not_qualified_for_this_surface",
            lambda status, reason, possible: self.guard(
                request, status, reason, possible_send=possible
            ),
            lambda grant, qualification: self._prepare(
                principal, request, grant, qualification
            ),
            lambda record: self._reconcile(principal, request, record),
        )

    async def _prepare(
        self, principal: PrincipalContext, request: ProtectedUpdate,
        grant: WorkGrant, qualification: str,
    ) -> PreparedMutation | GuardOutcome:
        handle = await self.state.get(request.work_id)
        if handle is None:
            return self.guard(request, "denied", "work_not_bound")
        provider = self.providers[handle.provider]
        current = await provider.get(handle.provider_work_id)
        if current is None or not current.canonical:
            return self.guard(request, "not_applied", "source_read_unavailable")
        if current.revision != request.observed_revision:
            return self.guard(request, "stale", "source_revision_changed")
        return PreparedMutation(
            intent={
                "request": request.model_dump(mode="json"), "provider": handle.provider,
                "task_gid": handle.provider_work_id, "qualification": qualification,
            },
            send=lambda: self._send(
                principal, request, grant, handle.provider,
                handle.provider_work_id, qualification,
            ),
        )
    async def _send(self, principal: PrincipalContext, request: ProtectedUpdate, grant: WorkGrant,
                    provider_name: str, task_gid: str, qualification: str) -> GuardOutcome:
        patch = WorkPatch.model_validate(request.patch.model_dump(exclude_unset=True))
        try:
            work = await apply_scalar(self.providers[provider_name], task_gid, patch)
        except UnknownEffect:
            work = await apply_scalar(self.providers[provider_name], task_gid, patch, send=False)
        except ProviderError as error:
            return self.guard(request, "not_applied", provider_rejection_reason(error))
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

    async def reconcile_record(
        self, principal: PrincipalContext, request: ProtectedUpdate, record: EffectRecord,
    ) -> GuardOutcome:
        """Read back one already-prepared exact intent; never send the scalar update."""
        return await self._reconcile(principal, request, record)

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
