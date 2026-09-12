"""One durable append gateway, shared independently of the calling MCP surface."""

import hashlib
import json

from sqlalchemy.exc import SQLAlchemyError

from .core import Provider, ProviderError, State, UnknownEffect
from .grant_state import GrantState
from .grants import EffectReceipt, GuardOutcome, PrincipalContext, ProtectedAppend, WorkGrant


class AppendGateway:
    def __init__(self, state: State, grants: GrantState, providers: dict[str, Provider]):
        self.state, self.grants, self.providers = state, grants, providers

    @staticmethod
    def fingerprint(principal: PrincipalContext, request: ProtectedAppend) -> str:
        payload = [principal.key, str(request.work_id), request.text]
        return hashlib.sha256(json.dumps(payload, ensure_ascii=False).encode()).hexdigest()

    @staticmethod
    def guard(
        request: ProtectedAppend, status: str, reason: str, *, possible_send: bool = False,
    ) -> GuardOutcome:
        return GuardOutcome.model_validate({'status': status, 'operation': "work_append", 'work_id': request.work_id, 'operation_id': request.operation_id, 'reason': reason, 'effect': "unknown" if possible_send else "not_sent", 'retry': "reconcile" if possible_send else "refresh" if status == "stale" else "none", 'next_action': "Reconcile the recorded effect; do not send a new operation."
                         if possible_send else "Refresh work/grant or ask the trusted issuer."})

    @staticmethod
    def admitted(principal: PrincipalContext, grant: WorkGrant | None) -> bool:
        return grant is not None and grant.principal == principal and grant.current()

    async def append(self, principal: PrincipalContext, request: ProtectedAppend) -> GuardOutcome:
        possible_send = False
        try:
            async with self.grants.locked(principal.key, request.work_id) as grant:
                if not self.admitted(principal, grant) or grant is None:
                    return self.guard(request, "denied", "no_current_grant")
                if (request.work_id != grant.authority.active_work_id
                        or "work_append" not in grant.operations):
                    return self.guard(request, "denied", "operation_or_work_not_granted")
                if request.grant_version != grant.version:
                    return self.guard(request, "stale", "grant_version_changed")
                qualification = grant.append_qualification
                if (qualification is None
                        or (principal.assurance == "test") != qualification.startswith("test:")):
                    return self.guard(request, "denied", "append_not_qualified_for_this_surface")
                fingerprint = self.fingerprint(principal, request)
                previous = await self.grants.previous(
                    request.operation_id, fingerprint, request.work_id,
                )
                if previous is not None:
                    owner, previous_fingerprint, outcome = previous
                    if owner == principal.key and previous_fingerprint == fingerprint:
                        return outcome
                    if outcome.operation_id == request.operation_id:
                        return self.guard(request, "denied", "operation_identity_conflict")
                    return self.guard(request, "unknown", "target_has_unresolved_effect",
                                      possible_send=True)
                handle = await self.state.get(request.work_id)
                if handle is None:
                    return self.guard(request, "denied", "work_not_bound")
                provider = self.providers[handle.provider]
                current = await provider.source_task(handle.provider_work_id)
                if current is None:
                    return self.guard(request, "not_applied", "source_read_unavailable")
                if not current.canonical:
                    return self.guard(request, "denied", "source_not_canonical")
                if current.completed:
                    return self.guard(request, "denied", "work_is_terminal")
                if current.revision != request.observed_revision:
                    return self.guard(request, "stale", "source_revision_changed")
                unknown = self.guard(request, "unknown", "prepared_or_unconfirmed_send",
                                     possible_send=True)
                await self.grants.prepare({
                    "request": request.model_dump(mode="json"),
                    "provider": handle.provider, "task_gid": handle.provider_work_id,
                    "qualification": qualification,
                }, grant, fingerprint, unknown)
                possible_send = True
                if not grant.current():
                    outcome = self.guard(request, "not_applied", "grant_expired_before_send")
                else:
                    outcome = await self._send(principal, grant, request, handle.provider,
                                               handle.provider_work_id, provider, qualification)
                await self.grants.finish(outcome)
                return outcome
        except (SQLAlchemyError, ProviderError, ValueError, KeyError):
            # A prepared intent survives cancellations/crashes too. Its UNKNOWN
            # remains the durable barrier until an exact trusted reconciliation.
            return self.guard(request, "unknown", "state_or_effect_unavailable",
                              possible_send=possible_send)

    async def _send(
        self, principal: PrincipalContext, grant: WorkGrant, request: ProtectedAppend,
        provider_name: str, task_gid: str, provider: Provider, qualification: str,
    ) -> GuardOutcome:
        try:
            story_gid = await provider.append(task_gid, request.text)
        except UnknownEffect:
            return self.guard(request, "unknown", "ambiguous_provider_send", possible_send=True)
        except ProviderError:
            return self.guard(request, "not_applied", "provider_rejected_send")
        if story_gid is not None:
            story = await provider.source_story(task_gid, story_gid)
            task = await provider.source_task(task_gid)
            if (story is not None and story.story_gid == story_gid and story.task_gid == task_gid
                    and story.text == request.text and task is not None and task.canonical):
                receipt = EffectReceipt(
                    operation_id=request.operation_id, principal=principal,
                    grant_id=grant.id, grant_version=grant.version, work_id=request.work_id,
                    provider=provider_name, task_gid=task_gid, story_gid=story_gid,
                    text=request.text, qualification=qualification,
                )
                return GuardOutcome(
                    status="ok", operation="work_append", work_id=request.work_id,
                    operation_id=request.operation_id, reason="exact_effect_verified",
                    effect="applied", retry="none", next_action="Use the recorded receipt.",
                    receipt=receipt,
                )
        return self.guard(request, "unknown", "effect_readback_unconfirmed", possible_send=True)
