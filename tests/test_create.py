from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from chatgpt_fixture import ACTIVE, PRINCIPAL, MemoryGrants, grant
from sqlalchemy.exc import SQLAlchemyError

from switchstand.chatgpt import ChatGPTService
from switchstand.contracts import Routing, WorkContext
from switchstand.core import Handle, ProviderError, ProviderSourceTask, ProviderWork, UnknownEffect
from switchstand.creates import CreateGateway
from switchstand.grants import PrincipalContext, ProtectedCreate


class State:
    def __init__(self):
        self.handles = {ACTIVE: Handle(ACTIVE, "asana", "123")}

    async def get(self, work_id):
        return self.handles.get(work_id)

    async def bind_reserved(self, work_id, provider, provider_work_id):
        existing = self.handles.get(work_id)
        if existing is not None and (existing.provider, existing.provider_work_id) != (provider, provider_work_id):
            raise ValueError("binding conflict")
        handle = Handle(work_id, provider, provider_work_id)
        self.handles[work_id] = handle
        return handle


class Provider:
    def __init__(self):
        self.created = {}
        self.creates = 0
        self.parent_ids = {"123"}
        self.lose_response = False
        self.visible = True
        self.binding = "fixture-recovery-v1"
        self.projects = {"999"}

    def recovery_identity(self):
        return self.binding

    async def source_task(self, task_gid):
        if task_gid in self.parent_ids:
            return ProviderSourceTask("Parent", "", False, "r1", WorkContext(), True)
        if task_gid in self.created.values():
            return ProviderSourceTask("Created", "notes", False, "r2", WorkContext(), True)
        return None

    async def get(self, task_gid):
        task = await self.source_task(task_gid)
        if task is None:
            return None
        return ProviderWork(
            task.title, task.notes, task.completed, task.revision,
            Routing(priority="P0"), WorkContext(), task.canonical,
        )

    async def create_work(
        self, title, notes, operation_id, *, parent_task_gid=None, project_gid=None
    ):
        if parent_task_gid is None:
            if project_gid not in self.projects:
                raise ProviderError("project denied")
            return await self.create_child(None, title, notes, operation_id)
        if project_gid is not None:
            raise ProviderError("create target invalid")
        return await self.create_child(parent_task_gid, title, notes, operation_id)

    async def create_child(self, parent_task_gid, title, notes, operation_id):
        self.creates += 1
        task_gid = str(9000 + self.creates)
        self.created[operation_id] = task_gid
        if self.lose_response:
            raise UnknownEffect("lost response")
        return task_gid

    async def recover_created(self, parent_task_gid, operation_id, *, project_gid=None):
        if parent_task_gid is None and project_gid not in self.projects:
            return None
        return self.created.get(operation_id) if self.visible else None


def subject(
    principal=PRINCIPAL, qualification="test:create", scope="launch",
):
    selected = grant(
        principal=principal, scope=scope,
        operations=frozenset({"work_get", "work_create"}),
        append_qualification=None, create_qualification=qualification,
    )
    state, provider = State(), Provider()
    grants = MemoryGrants(selected)

    async def resolve_principal():
        return principal

    return ChatGPTService(resolve_principal, state, grants, {"asana": provider}), selected, state, provider


def request(selected, operation_id=None, **changes):
    values = {
        "api_version": "1", "operation_id": operation_id or uuid4(),
        "parent_work_id": selected.authority.active_work_id,
        "grant_version": selected.version, "title": "Created", "notes": "notes",
    }
    return ProtectedCreate(**(values | changes))


async def test_create_binds_reserved_work_and_normal_work_readback():
    service, selected, state, provider = subject()
    req = request(selected)
    result = await service.create(req)
    assert result.status == "ok" and result.effect == "applied"
    assert result.receipt.task_gid == "9001" and result.receipt.parent_task_gid == "123"
    assert result.work_id == CreateGateway.work_id(req.operation_id)
    assert (await state.get(result.work_id)).provider_work_id == "9001"
    readback = await service.get(result.work_id)
    assert readback.status == "ok" and readback.item.id == result.work_id
    assert readback.item.title == "Created" and provider.creates == 1


@pytest.mark.parametrize("invalid_title", [" \t ", "before\0after"])
async def test_unindexable_title_is_rejected_before_create_preparation_or_send(
    invalid_title,
):
    service, selected, _state, provider = subject()
    req = request(selected, title=invalid_title)

    result = await service.create(req)

    assert result.status == "denied" and result.reason == "title_not_indexable"
    assert provider.creates == 0
    assert service.grants.effects == {}


async def test_lost_create_response_recovers_after_restart_without_second_send():
    service, selected, state, provider = subject()
    req = request(selected)
    provider.lose_response, provider.visible = True, False
    first = await service.create(req)
    assert first.status == "unknown" and first.effect == "unknown"
    assert provider.creates == 1

    provider.visible = True
    restarted = ChatGPTService(service.principal, state, service.grants, service.providers)
    recovered = await restarted.create(req)
    assert recovered.status == "ok" and recovered.effect == "applied"
    assert recovered.receipt.task_gid == "9001"
    assert provider.creates == 1
    assert await restarted.create(req) == recovered
    assert (await restarted.get(recovered.work_id)).status == "ok"
    assert provider.creates == 1


async def test_unreadable_intent_history_preserves_unknown_and_never_resends(monkeypatch):
    service, selected, _state, provider = subject()
    req = request(selected)
    provider.lose_response, provider.visible = True, False
    assert (await service.create(req)).effect == "unknown"

    async def unavailable(_operation_id):
        raise SQLAlchemyError("intent journal unavailable")

    monkeypatch.setattr(service.grants, "exact", unavailable)
    replay = await service.create(req)
    assert replay.status == "unknown" and replay.effect == "unknown"
    assert replay.retry == "reconcile" and provider.creates == 1


async def test_unresolved_create_recovers_after_grant_renewal_with_original_receipt():
    service, selected, _state, provider = subject()
    req = request(selected)
    provider.lose_response, provider.visible = True, False
    assert (await service.create(req)).effect == "unknown"

    renewed = selected.model_copy(update={"id": uuid4(), "version": 2})
    service.grants.grant = renewed
    provider.visible = True
    recovered = await service.create(req)
    assert recovered.status == "ok" and recovered.receipt.grant_id == selected.id
    assert recovered.receipt.grant_version == selected.version and provider.creates == 1
    assert (await service.get(recovered.work_id)).status == "ok"


async def test_unresolved_create_recovers_after_original_grant_expiry_without_send():
    service, selected, _state, provider = subject()
    req = request(selected)
    provider.lose_response, provider.visible = True, False
    assert (await service.create(req)).effect == "unknown"

    service.grants.grant = selected.model_copy(update={
        "expires_at": datetime.now(UTC) - timedelta(seconds=1),
    })
    provider.visible = True
    recovered = await service.create(req)
    assert recovered.status == "ok" and recovered.receipt.grant_id == selected.id
    assert recovered.receipt.grant_version == 1 and provider.creates == 1


async def test_recovery_binding_change_blocks_until_original_binding_returns():
    service, selected, _state, provider = subject()
    req = request(selected)
    provider.lose_response, provider.visible = True, False
    first = await service.create(req)
    assert first.effect == "unknown" and provider.creates == 1

    provider.visible = True
    provider.binding = "different-recovery-v2"
    blocked = await service.create(req)
    assert blocked.status == "unknown" and blocked.effect == "unknown"
    assert blocked.reason == "create_recovery_binding_changed" and provider.creates == 1

    provider.binding = "fixture-recovery-v1"
    recovered = await service.create(req)
    assert recovered.status == "ok" and recovered.receipt.task_gid == "9001"
    assert provider.creates == 1


async def test_operation_identity_and_qualification_block_unsafe_create():
    service, selected, _state, provider = subject()
    req = request(selected)
    provider.lose_response, provider.visible = True, False
    assert (await service.create(req)).status == "unknown"
    conflict = await service.create(req.model_copy(update={"title": "Different"}))
    assert conflict.status == "denied" and conflict.reason == "operation_identity_conflict"

    unqualified = selected.model_copy(update={"version": 2, "create_qualification": None})
    service.grants.grant = unqualified
    denied = await service.create(request(unqualified))
    assert denied.status == "denied" and denied.effect == "not_sent"
    assert provider.creates == 1


async def test_workspace_scope_can_create_under_explicit_bound_canonical_parent():
    service, selected, state, provider = subject()
    foreign = uuid4()
    state.handles[foreign] = Handle(foreign, "asana", "124")
    provider.parent_ids.add("124")
    workspace = selected.model_copy(update={"scope": "workspace"})
    service.grants.grant = workspace

    result = await service.create(request(workspace, parent_work_id=foreign))
    assert result.status == "ok" and result.receipt is not None
    assert result.receipt.parent_task_gid == "124"

    launch = workspace.model_copy(update={"scope": "launch"})
    service.grants.grant = launch
    denied = await service.create(request(launch, parent_work_id=foreign))
    assert denied.status == "denied" and denied.effect == "not_sent"


async def test_create_qualification_is_explicit_for_test_and_authenticated_surfaces():
    service, selected, _state, _provider = subject()
    assert (await service.create(request(selected))).status == "ok"

    authenticated = PrincipalContext(
        issuer="fixture", subject="real", client_id="client", assurance="authenticated"
    )
    real_service, real_grant, _state, _provider = subject(
        authenticated, "real:create"
    )
    assert (await real_service.create(request(real_grant))).status == "ok"

    for principal, qualification in [
        (PRINCIPAL, "real:create"),
        (authenticated, "test:create"),
        (authenticated, "legacy:create"),
        (authenticated, "garbage"),
    ]:
        denied_service, denied_grant, _state, denied_provider = subject(
            principal, qualification
        )
        denied = await denied_service.create(request(denied_grant))
        assert denied.status == "denied"
        assert denied.reason == "create_not_qualified_for_this_surface"
        assert denied_provider.creates == 0


async def test_workspace_project_create_binds_and_ambiguous_send_never_resends():
    service, selected, state, provider = subject(scope="workspace")
    req = request(
        selected, parent_work_id=None, project_gid="999",
    )
    applied = await service.create(req)
    assert applied.status == "ok" and applied.effect == "applied"
    assert applied.receipt.project_gid == "999"
    assert applied.receipt.parent_task_gid is None
    assert (await state.get(applied.work_id)).provider_work_id == "9001"

    denied = await service.create(request(
        selected, parent_work_id=None, project_gid="998",
    ))
    assert denied.status == "not_applied"
    assert denied.effect == "not_sent"

    uncertain = request(
        selected, parent_work_id=None, project_gid="999", operation_id=uuid4(),
    )
    provider.lose_response, provider.visible = True, False
    first = await service.create(uncertain)
    assert (first.status, first.effect, provider.creates) == ("unknown", "unknown", 2)
    replay = await service.create(uncertain)
    assert (replay.status, replay.effect, provider.creates) == ("unknown", "unknown", 2)
