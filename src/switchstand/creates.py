"""Durable work-create gateway using the existing effect-intent journal."""

import hashlib
import json
from typing import Protocol, cast
from uuid import UUID, uuid5

from sqlalchemy.exc import SQLAlchemyError

from .core import (
    Provider,
    ProviderError,
    ProviderWork,
    State,
    UnknownEffect,
    content_authorization_token,
    provider_rejection_reason,
)
from .grant_state import EffectRecord, GrantState
from .grants import CreateReceipt, GuardOutcome, PrincipalContext, ProtectedCreate
from .work_index import normalize_title

CREATE_NAMESPACE = UUID("238ea5f0-fb67-4f46-81b1-560fa39375ce")


class CreateProvider(Protocol):
    def recovery_identity(self) -> str: ...
    async def create_work(
        self, title: str, notes: str, operation_id: UUID, *,
        parent_task_gid: str | None = None, project_gid: str | None = None,
        require_canonical: bool = True,
    ) -> str: ...
    async def recover_created(
        self, parent_task_gid: str | None, operation_id: UUID, *,
        project_gid: str | None = None,
    ) -> str | None: ...


class CreateState(Protocol):
    async def bind_reserved(self, work_id: UUID, provider: str, provider_work_id: str) -> object: ...


class CreateWorksets(Protocol):
    async def validate_create_parent(self, work_id: UUID) -> None: ...
    async def admit_created_under_parent(
        self, work_id: UUID, parent_id: UUID, title: str, provider: ProviderWork,
    ) -> None: ...


class Stage3CreateState(CreateState, Protocol):
    worksets: CreateWorksets


class CreateGateway:
    def __init__(self, state: State, grants: GrantState, providers: dict[str, Provider]):
        self.state, self.grants, self.providers = state, grants, providers

    @staticmethod
    def work_id(operation_id: UUID) -> UUID:
        return uuid5(CREATE_NAMESPACE, str(operation_id))

    @staticmethod
    def fingerprint(principal: PrincipalContext, request: ProtectedCreate) -> str:
        payload = [
            principal.key,
            None if request.parent_work_id is None else str(request.parent_work_id),
            request.project_gid,
            request.grant_version,
            request.title,
            request.notes,
        ]
        return hashlib.sha256(json.dumps(payload, ensure_ascii=False).encode()).hexdigest()

    @classmethod
    def guard(
        cls, request: ProtectedCreate, status: str, reason: str, *, possible_send: bool = False,
    ) -> GuardOutcome:
        return GuardOutcome.model_validate({
            "status": status, "operation": "work_create", "work_id": cls.work_id(request.operation_id),
            "operation_id": request.operation_id, "reason": reason,
            "effect": "unknown" if possible_send else "not_sent",
            "retry": "reconcile" if possible_send else "refresh" if status == "stale" else "none",
            "next_action": "Retry only this OperationId so Switchstand can reconcile it; do not create again."
            if possible_send else "Refresh admission or ask the trusted issuer.",
        })

    @staticmethod
    def qualification(record: EffectRecord) -> str:
        value = record.intent.get("qualification")
        if not isinstance(value, str) or not value:
            raise ValueError("create qualification missing from durable intent")
        return value

    @staticmethod
    def recovery_identity(record: EffectRecord) -> str:
        value = record.intent.get("recovery_identity")
        if not isinstance(value, str) or not value:
            raise ValueError("create recovery identity missing from durable intent")
        return value

    async def create(self, principal: PrincipalContext, request: ProtectedCreate) -> GuardOutcome:
        try:
            normalize_title(request.title)
        except (TypeError, ValueError):
            return self.guard(request, "denied", "title_not_indexable")
        possible_send = False
        history_known = False
        try:
            async with self.grants.locked(principal.key, request.parent_work_id) as grant:
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

                if request.parent_work_id is not None and await self.grants.unresolved_create(
                    request.parent_work_id
                ):
                    return self.guard(request, "unknown", "parent_has_unresolved_create")

                if grant is None or grant.principal != principal or not grant.current():
                    return self.guard(request, "denied", "no_current_grant")
                if "work_create" not in grant.operations:
                    return self.guard(request, "denied", "operation_not_granted")
                if request.parent_work_id is not None and not grant.can_write(request.parent_work_id):
                    return self.guard(request, "denied", "parent_not_granted")
                if request.parent_work_id is None and grant.scope != "workspace":
                    return self.guard(request, "denied", "workspace_create_required")
                if request.grant_version != grant.version:
                    return self.guard(request, "stale", "grant_version_changed")
                qualification = grant.create_qualification
                expected_qualification = (
                    "test:" if principal.assurance == "test" else "real:"
                )
                if (
                    qualification is None
                    or not qualification.startswith(expected_qualification)
                ):
                    return self.guard(request, "denied", "create_not_qualified_for_this_surface")

                parent_task_gid: str | None = None
                database_parent = False
                provider_name = "asana"
                if request.parent_work_id is not None:
                    parent = await self.state.get(request.parent_work_id)
                    if parent is None:
                        return self.guard(request, "denied", "parent_not_bound")
                    provider_name, parent_task_gid = parent.provider, parent.provider_work_id
                    try:
                        database_parent = (
                            await content_authorization_token(self.state, request.parent_work_id)
                            is not None
                        )
                        if database_parent:
                            await cast(Stage3CreateState, self.state).worksets\
                                .validate_create_parent(request.parent_work_id)
                    except PermissionError:
                        return self.guard(request, "denied", "parent_not_admitted")
                provider = self.providers.get(provider_name)
                if (
                    provider is None
                    or not hasattr(provider, "create_work")
                    or not hasattr(provider, "recover_created")
                    or not hasattr(provider, "recovery_identity")
                ):
                    return self.guard(request, "denied", "provider_create_not_supported")
                create_provider = cast(CreateProvider, provider)
                recovery_identity = create_provider.recovery_identity()
                if not recovery_identity:
                    return self.guard(request, "denied", "provider_create_not_supported")
                if parent_task_gid is not None:
                    current = await provider.source_task(parent_task_gid)
                    if current is None:
                        return self.guard(request, "not_applied", "parent_read_unavailable")
                    indexed = getattr(self.state, "work_index", None)
                    completed = current.completed
                    if database_parent and indexed is not None:
                        row = await indexed.get(request.parent_work_id)
                        if row is None:
                            return self.guard(request, "denied", "parent_not_admitted")
                        completed = row.completed
                    if (not current.canonical and not database_parent) or completed:
                        return self.guard(request, "denied", "parent_not_writable")

                unknown = self.guard(
                    request, "unknown", "prepared_or_unconfirmed_send", possible_send=True
                )
                await self.grants.prepare({
                    "request": request.model_dump(mode="json"),
                    "provider": provider_name,
                    "parent_task_gid": parent_task_gid,
                    "project_gid": request.project_gid,
                    "qualification": qualification,
                    "recovery_identity": recovery_identity,
                    "database_parent": database_parent,
                }, grant, fingerprint, unknown)
                possible_send = True
                if not grant.current():
                    outcome = self.guard(request, "not_applied", "grant_expired_before_send")
                else:
                    outcome = await self._send(
                        principal, request, grant.id, grant.version, qualification,
                        recovery_identity, provider_name, parent_task_gid,
                        request.project_gid, database_parent, create_provider,
                    )
                await self.grants.finish(outcome)
                return outcome
        except (SQLAlchemyError, ProviderError, TypeError, ValueError, KeyError):
            return self.guard(
                request, "unknown", "state_or_effect_unavailable",
                possible_send=possible_send or not history_known,
            )

    async def _send(
        self, principal: PrincipalContext, request: ProtectedCreate,
        grant_id: UUID, grant_version: int, qualification: str, recovery_identity: str,
        provider_name: str, parent_task_gid: str | None, project_gid: str | None,
        database_parent: bool,
        provider: CreateProvider,
    ) -> GuardOutcome:
        try:
            index = getattr(self.state, "work_index", None)
            provider_title = (
                "Switchstand work"
                if index is not None and await index.active()
                else request.title
            )
            task_gid = await provider.create_work(
                provider_title, request.notes, request.operation_id,
                parent_task_gid=parent_task_gid, project_gid=project_gid,
                require_canonical=not database_parent,
            )
        except UnknownEffect:
            return await self._reconcile_values(
                principal, request, grant_id, grant_version, qualification, recovery_identity,
                provider_name, parent_task_gid, project_gid, database_parent,
            )
        except ProviderError as error:
            return self.guard(request, "not_applied", provider_rejection_reason(error))
        return await self._applied(
            principal, request, grant_id, grant_version, qualification,
            provider_name, parent_task_gid, project_gid, task_gid,
            database_parent=database_parent,
        )

    async def _reconcile(
        self, principal: PrincipalContext, request: ProtectedCreate, record: EffectRecord,
    ) -> GuardOutcome:
        intent = record.intent
        provider_name = intent.get("provider")
        parent_task_gid = intent.get("parent_task_gid")
        project_gid = intent.get("project_gid")
        database_parent = intent.get("database_parent", False)
        if not isinstance(provider_name, str) or not provider_name:
            raise ValueError("durable create provider invalid")
        if parent_task_gid is not None and not isinstance(parent_task_gid, str):
            raise ValueError("durable create parent invalid")
        if project_gid is not None and not isinstance(project_gid, str):
            raise ValueError("durable create project invalid")
        if not isinstance(database_parent, bool):
            raise TypeError("durable create authority invalid")
        return await self._reconcile_values(
            principal, request, record.grant_id, record.grant_version,
            self.qualification(record), self.recovery_identity(record),
            provider_name, parent_task_gid, project_gid,
            database_parent,
        )

    async def _reconcile_values(
        self, principal: PrincipalContext, request: ProtectedCreate,
        grant_id: UUID, grant_version: int, qualification: str, recovery_identity: str,
        provider_name: str, parent_task_gid: str | None, project_gid: str | None,
        database_parent: bool,
    ) -> GuardOutcome:
        provider = self.providers.get(provider_name)
        if (
            provider is None
            or not hasattr(provider, "recover_created")
            or not hasattr(provider, "recovery_identity")
        ):
            return self.guard(
                request, "unknown", "create_recovery_unavailable", possible_send=True
            )
        create_provider = cast(CreateProvider, provider)
        if create_provider.recovery_identity() != recovery_identity:
            return self.guard(
                request, "unknown", "create_recovery_binding_changed", possible_send=True
            )
        bound = await self.state.get(self.work_id(request.operation_id))
        task_gid = (
            bound.provider_work_id
            if bound is not None and bound.provider == provider_name
            else await create_provider.recover_created(
                parent_task_gid, request.operation_id, project_gid=project_gid
            )
        )
        if task_gid is None:
            return self.guard(
                request, "unknown", "create_not_yet_reconciled", possible_send=True
            )
        outcome = await self._applied(
            principal, request, grant_id, grant_version, qualification,
            provider_name, parent_task_gid, project_gid, task_gid,
            database_parent=database_parent,
        )
        await self.grants.finish(outcome)
        return outcome

    async def _applied(
        self, principal: PrincipalContext, request: ProtectedCreate,
        grant_id: UUID, grant_version: int, qualification: str,
        provider_name: str, parent_task_gid: str | None, project_gid: str | None,
        task_gid: str,
        *, database_parent: bool = False,
    ) -> GuardOutcome:
        provider = self.providers[provider_name]
        work_id = self.work_id(request.operation_id)
        if database_parent:
            # The binding is recovery-only until the atomic Stage 3 admission below succeeds.
            await cast(CreateState, self.state).bind_reserved(work_id, provider_name, task_gid)
        task = await provider.source_task(task_gid)
        if task is None or (not task.canonical and not database_parent):
            return self.guard(
                request, "unknown", "created_task_readback_unconfirmed", possible_send=True
            )
        if not database_parent:
            await cast(CreateState, self.state).bind_reserved(work_id, provider_name, task_gid)
        index = getattr(self.state, "work_index", None)
        if index is not None and await index.active():
            work = await provider.get(task_gid)
            if work is None or (not work.canonical and not database_parent):
                return self.guard(
                    request, "unknown", "created_task_readback_unconfirmed", possible_send=True
                )
            if database_parent:
                if request.parent_work_id is None:
                    raise ValueError("database parent create lacks parent")
                await cast(Stage3CreateState, self.state).worksets.admit_created_under_parent(
                    work_id, request.parent_work_id, request.title, work
                )
            else:
                await index.admit_created(work_id, request.title, work)
        receipt = CreateReceipt(
            operation_id=request.operation_id, principal=principal, grant_id=grant_id,
            grant_version=grant_version, work_id=work_id, provider=provider_name,
            task_gid=task_gid, parent_task_gid=parent_task_gid, project_gid=project_gid,
            title=request.title, qualification=qualification,
        )
        return GuardOutcome(
            status="ok", operation="work_create", work_id=work_id,
            operation_id=request.operation_id, reason="exact_create_verified", effect="applied",
            retry="none", next_action="Use the recorded create receipt.", receipt=receipt,
        )
