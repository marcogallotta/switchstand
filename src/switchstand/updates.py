import hashlib
import json
from typing import Protocol, cast
from uuid import UUID

from .contracts import WorkPatch
from .core import (
    Provider,
    ProviderError,
    ProviderWork,
    State,
    UnknownEffect,
    apply_scalar,
    authoritative_work,
    observed_revision_matches,
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
from .work_index import WorkIndex, normalize_title
from .work_metadata import MUTABLE_FIELDS, authority_generation


class Stage1State(Protocol):
    work_index: WorkIndex


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
        fields = request.patch.model_fields_set
        if fields & {"horizon", "stage3_gate"}:
            return self.guard(request, "denied", "legacy_metadata_is_read_only")
        if "title" in fields:
            try:
                normalize_title(cast(str, request.patch.title))
            except (TypeError, ValueError):
                return self.guard(request, "denied", "title_not_indexable")
        index = getattr(self.state, "work_index", None)
        metadata_active = (
            index is not None and await authority_generation(index.engine) is not None
        )
        if "review_next_action" in fields and "notes" not in fields and not metadata_active:
            return self.guard(request, "denied", "review_next_action_requires_notes")
        new_metadata_fields = set(MUTABLE_FIELDS) - {
            "priority", "work_type", "review_next_action"
        }
        if fields & new_metadata_fields and not metadata_active:
            return self.guard(request, "denied", "metadata_authority_not_active")
        database_fields = fields & ({"title", "completed"} | (
            set(MUTABLE_FIELDS) if metadata_active else set()
        ))
        if (
            database_fields and fields - database_fields and index is not None
            and await index.active()
        ):
            return self.guard(request, "denied", "patch_spans_authority_domains")
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
        if not await observed_revision_matches(
            self.state, request.work_id, request.observed_revision, current.revision,
            provider_notes=current.notes, provider_context=current.context,
        ):
            return self.guard(request, "stale", "source_revision_changed")
        index = getattr(self.state, "work_index", None)
        database_fields = {"title", "completed"}
        if index is not None and await authority_generation(index.engine) is not None:
            database_fields |= set(MUTABLE_FIELDS)
        if (
            request.patch.model_fields_set <= database_fields
            and index is not None and await index.active()
        ):
            try:
                await index.validate_update_fields(
                    request.work_id, request.patch.model_dump(exclude_unset=True)
                )
            except (PermissionError, TypeError, ValueError):
                return self.guard(request, "denied", "invalid_database_metadata")
            return PreparedMutation(
                intent={
                    "request": request.model_dump(mode="json"), "authority": "postgres",
                    "provider": handle.provider, "task_gid": handle.provider_work_id,
                    "qualification": qualification,
                },
                send=lambda: self._send_database_fields(
                    principal, request, grant, handle.provider,
                    handle.provider_work_id, qualification, current,
                ),
            )
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

    async def _send_database_fields(
        self, principal: PrincipalContext, request: ProtectedUpdate, grant: WorkGrant,
        provider_name: str, task_gid: str, qualification: str, current: ProviderWork,
    ) -> GuardOutcome:
        index = cast(Stage1State, self.state).work_index
        fields = request.patch.model_dump(exclude_unset=True)
        revision, applied = await index.update_fields(
            request.work_id, request.observed_revision, current, fields
        )
        if not applied:
            return self.guard(request, "stale", "source_revision_changed")
        after = await self.providers[provider_name].get(task_gid)
        metadata_active = await authority_generation(index.engine) is not None
        unchanged = (
            after is not None and after.canonical
            and (
                (after.notes, after.context) == (current.notes, current.context)
                if metadata_active else after.revision == current.revision
            )
        )
        if not unchanged:
            return self.guard(
                request, "unknown", "effect_readback_unconfirmed", possible_send=True
            )
        return self._applied(
            principal, request, grant.id, grant.version, provider_name,
            task_gid, qualification, current, resulting_revision=revision,
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
        if work is not None:
            work = await authoritative_work(self.state, request.work_id, work)
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
        if intent.get("authority") == "postgres":
            provider = self.providers[provider_name]
            current = await provider.get(task_gid)
            if current is None or not current.canonical:
                return self.guard(
                    request, "unknown", "effect_readback_unconfirmed", possible_send=True
                )
            index = cast(Stage1State, self.state).work_index
            indexed = await index.get(request.work_id)
            requested = request.patch.model_dump(exclude_unset=True)
            scalar = {"title", "completed"}
            if indexed is None:
                return self.guard(
                    request, "unknown", "effect_readback_unconfirmed", possible_send=True
                )
            matches = not any(
                (
                    getattr(indexed, field) if field in scalar
                    else getattr(indexed.routing, field)
                ) != value
                for field, value in requested.items()
            )
            if matches and await authority_generation(index.engine) is not None:
                if "lifecycle_state" in requested:
                    matches = indexed.completed == (
                        requested["lifecycle_state"] == "TERMINAL"
                    )
                if matches and "completed" in requested:
                    matches = indexed.routing.lifecycle_state == (
                        "TERMINAL" if requested["completed"] else "CURRENT"
                    )
            if not matches:
                return self.guard(
                    request, "unknown", "effect_readback_unconfirmed", possible_send=True
                )
            revision = await index.revision(
                request.work_id, current.revision,
                provider_notes=current.notes, provider_context=current.context,
            )
            outcome = self._applied(
                principal, request, record.grant_id, record.grant_version,
                provider_name, task_gid, qualification, current,
                resulting_revision=revision,
            )
            await self.grants.finish(outcome)
            return outcome
        patch = WorkPatch.model_validate(request.patch.model_dump(exclude_unset=True))
        work = await apply_scalar(self.providers[provider_name], task_gid, patch, send=False)
        if work is not None:
            work = await authoritative_work(self.state, request.work_id, work)
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
                 work: ProviderWork | None, *, resulting_revision: str | None = None,
    ) -> GuardOutcome:
        if work is None:
            return self.guard(request, "unknown", "effect_readback_unconfirmed", possible_send=True)
        receipt = UpdateReceipt(
            operation_id=request.operation_id, principal=principal, grant_id=grant_id,
            grant_version=grant_version, work_id=request.work_id, provider=provider_name,
            task_gid=task_gid, observed_revision=request.observed_revision,
            resulting_revision=resulting_revision or work.revision,
            patch=request.patch, qualification=qualification,
        )
        return GuardOutcome(status="ok", operation="work_update", work_id=request.work_id,
                            operation_id=request.operation_id, reason="scalar_state_converged",
                            effect="applied", retry="none", next_action="Use the recorded receipt.",
                            receipt=receipt)
